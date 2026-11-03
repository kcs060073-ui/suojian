"""Read-only inspection of managed inputs and known frozen outputs.

This phase never restores, migrates, starts a worker, or authorizes switching.
Nested external dependencies still need the following snapshot/recovery phase.
"""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
from urllib.parse import quote

from .database import CatalogDatabase
from .path_utils import filesystem_path
from .task_script import read_task_script
from .tasks import get_task


class RecoveryPlanError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise RecoveryPlanError(code, message)


def _lexical_filesystem_path(path):
    # lstat must inspect the supplied entry before resolve follows a junction.
    value = Path(path).absolute()
    if os.name != "nt":
        return value
    text = str(value)
    if text.startswith("\\\\?\\"):
        return value
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text.lstrip("\\"))
    return Path("\\\\?\\" + text)


def _plain_path(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        native = _lexical_filesystem_path(component)
        try:
            info = native.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            _fail("PATH_REDIRECTED", "接续路径含链接或重定向，请先核对实际目录归属。")
    return path.resolve()


def _logical_digest(connection):
    digest = hashlib.sha256()
    for line in connection.iterdump():
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _managed_files(root, cache_root):
    folders = {root / "runtime/workbench/tasks", root / "runtime/voice",
               root / "runtime/workbench/cache", root / "assets-local", cache_root}
    rows = {}
    for folder in sorted(folders):
        native = filesystem_path(folder)
        if not native.exists():
            continue
        _plain_path(folder)
        if not native.is_dir():
            _fail("MANAGED_DIRECTORY_INVALID", "托管目录不是可读文件夹。")
        pending = [native]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    info = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                        _fail("PATH_REDIRECTED", "托管文件树含链接或重定向，不能自动接续。")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        _fail("MANAGED_FILE_UNSUPPORTED", "托管文件树含不支持的文件类型。")
                    if entry.name.lower() in {".env", "session-signing.key"}:
                        _fail("SENSITIVE_MANAGED_FILE", "托管文件树内含凭据文件，需维护人员处理。")
                    relative = path.relative_to(filesystem_path(root)).as_posix()
                    before = path.lstat()
                    if stat.S_ISLNK(before.st_mode) or getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                        _fail("PATH_REDIRECTED", "托管文件在读取前发生重定向。")
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                            _fail("SOURCE_CHANGED", "源文件在打开时被替换，请停止原服务后重试。")
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        _fail("SOURCE_CHANGED", "源文件在盘点时变化，请停止原服务后重试。")
                    rows[relative] = {"relativePath": relative, "bytes": before.st_size, "sha256": digest.hexdigest()}
    return [rows[key] for key in sorted(rows)]


def _voice_checks(db, root, tables):
    from .voice_bundle_service import read_registered_voice
    from .voice_task_context import resolve_voice_task_context
    checks = []
    seen = set()
    for table in ("voice_edit_heads", "voice_edit_versions"):
        if table not in tables:
            continue
        for row in db.connection.execute(f"SELECT task_id,context_digest,config_json,audio_json FROM {table}"):
            if not row["audio_json"]:
                continue
            try:
                audio = json.loads(row["audio_json"])
                if audio is None:
                    continue
                config = json.loads(row["config_json"])
                if not isinstance(audio, dict) or not isinstance(config, dict):
                    _fail("VOICE_UNAVAILABLE", "托管声音记录格式不完整。")
                key = (row["task_id"], audio.get("bundle_id"), row["context_digest"], config.get("fingerprint"))
                if table == "voice_edit_heads" and resolve_voice_task_context(db, root, row["task_id"]).context_digest != row["context_digest"]:
                    _fail("VOICE_CONTEXT_CHANGED", "当前声音与原稿上下文不一致，需先恢复原依据。")
                if key in seen:
                    continue
                checked = read_registered_voice(db, root, row["task_id"], audio, config, row["context_digest"])
                seen.add(key)
                checks.append({"taskId": row["task_id"], "bundleId": checked["bundle_id"],
                               "contextDigest": row["context_digest"], "audioSha256": audio["sha256"],
                               "durationUs": checked["timeline"]["duration_us"]})
            except RecoveryPlanError:
                raise
            except (ValueError, OSError, KeyError, TypeError):
                _fail("VOICE_UNAVAILABLE", "托管声音或祖先依据缺失/不一致，请恢复完整声音树。")
    return checks



def _legacy_voice_checks(db, root):
    """Verify existing legacy inputs without granting registered provenance."""
    from .ad_voice_preview import current_ad_voice_source
    from .voice_bundle_service import resolve_ad_render_inputs
    from .voice_ownership import read_voice_ownership
    checks = []
    bindings = {}
    for row in db.connection.execute("SELECT task_id FROM tasks WHERE task_type='ad_mix' ORDER BY task_id"):
        task_id = row[0]
        if not isinstance(task_id, str) or task_id in ("", ".", "..") or "/" in task_id or "\\" in task_id:
            _fail("TASK_RECORD_INVALID", "任务标识无法安全定位旧配音目录。")
        ad = root / "runtime/workbench/tasks" / task_id / "artifacts/ad"
        paths = [ad / name for name in ("adplan.json", "adtiming.json", "voice.mp3", "recommendation.json")]
        if not any(filesystem_path(path).exists() for path in paths):
            continue  # No generated voice in a new task; never create one.
        ownership = read_voice_ownership(db, task_id)
        if ownership.branch != "legacy":
            if ownership.branch == "managed" or ownership.reason == "CURRENT_CONTEXT_INVALID":
                continue  # Registered current/history were checked above.
            _fail("LEGACY_VOICE_UNAVAILABLE", "旧配音归属账本缺失或冲突，不能自动接续。")
        try:
            for path in paths:
                _plain_path(path)
                if not filesystem_path(path).is_file():
                    _fail("LEGACY_VOICE_UNAVAILABLE", "旧配音及时间表材料不完整。")
            for path in paths:
                bindings[path.relative_to(root).as_posix()] = hashlib.sha256(filesystem_path(path).read_bytes()).hexdigest()
            task = get_task(db, task_id)
            current = current_ad_voice_source(db, root, task)
            if current.source_kind != "legacy" or current.status != "available":
                _fail("LEGACY_VOICE_UNAVAILABLE", "旧配音与锁定原稿或已记录产物不一致。")
            script = read_task_script(root, task)
            inputs = resolve_ad_render_inputs(db, root, task_id, script, task["brief"].get("aspect_ratio"))
            metadata = json.loads(filesystem_path(paths[-1]).read_text(encoding="utf8"))
            duration = inputs["timing"]["total"]
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
                _fail("LEGACY_VOICE_UNAVAILABLE", "旧配音时间表总时长无效。")
            for segment in inputs["timing"]["segments"]:
                if any(type(segment.get(key)) not in (int, float) or not math.isfinite(segment[key]) for key in ("start", "end", "dur")):
                    _fail("LEGACY_VOICE_UNAVAILABLE", "旧配音逐句时间必须是有限数值。")
            checks.append({"taskId": task_id, "sourceKind": "legacy", "ownershipReason": ownership.reason,
                           "evidenceGrade": "recorded_artifact_hashes" if metadata.get("artifact_sha256") is not None else "script_binding_only",
                           "originalTtsInput": "unverified", "audioDecode": "unverified",
                           "timingStructure": "verified", "durationUs": round(duration * 1000000),
                           "audioSha256": bindings[paths[2].relative_to(root).as_posix()]})
        except RecoveryPlanError:
            raise
        except (ValueError, OSError, KeyError, TypeError, RuntimeError):
            _fail("LEGACY_VOICE_UNAVAILABLE", "旧配音、分镜或时间表无法按原读取合同核验。")
    return checks, bindings


def build_recovery_plan(source_root: Path, db_path: Path, destination_root: Path, *, _original_reference_root=None) -> dict:
    source = _plain_path(source_root)
    destination = _plain_path(destination_root)
    database = _plain_path(db_path)
    if not filesystem_path(source).is_dir():
        _fail("SOURCE_ROOT_UNAVAILABLE", "找不到旧安装目录。")
    reference_root = source
    receipt_version = None
    restored_receipt = _plain_path(source / 'runtime/upgrade-recovery-complete.json')
    if _original_reference_root is not None:
        reference_root = _plain_path(_original_reference_root)
    elif filesystem_path(restored_receipt).is_file():
        try:
            receipt = json.loads(filesystem_path(restored_receipt).read_text(encoding='utf8'))
            if (not isinstance(receipt, dict) or receipt.get('formatVersion') != 1
                    or receipt.get('restoreComplete') is not True or receipt.get('oldRootMustBePreserved') is not True):
                _fail('RESTORE_RECEIPT_INVALID', '恢复完成记录格式不受支持。')
            if (_plain_path(receipt['destinationRoot']) != source
                    or receipt.get('canSwitchInstallation') is not False
                    or type(receipt.get('schemaVersion')) is not int or receipt['schemaVersion'] <= 0
                    or not isinstance(receipt.get('databaseLogicalSha256'), str)
                    or len(receipt['databaseLogicalSha256']) != 64
                    or any(c not in '0123456789abcdef' for c in receipt['databaseLogicalSha256'])):
                _fail('RESTORE_RECEIPT_INVALID', '恢复记录不属于当前安装或缺少原账本身份。')
            receipt_version = receipt['schemaVersion']
            reference_root = _plain_path(receipt['originalReferenceRoot'])
            if source == reference_root or source.is_relative_to(reference_root) or reference_root.is_relative_to(source):
                _fail('RESTORE_RECEIPT_INVALID', '恢复记录的历史引用目录必须与当前安装独立。')
        except (OSError, ValueError, KeyError, TypeError):
            _fail('RESTORE_RECEIPT_INVALID', '恢复完成记录缺少明确历史原引用目录。')
    if not filesystem_path(reference_root).is_dir():
        _fail('ORIGINAL_REFERENCE_ROOT_UNAVAILABLE', '历史原引用目录不可读；保持原地址，不改绑定。')
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        _fail("DESTINATION_OVERLAP", "新旧目录必须独立，不能相同或互相嵌套。")
    if not database.is_relative_to(source) or not filesystem_path(database).is_file():
        _fail("SOURCE_DB_UNAVAILABLE", "源账本必须是旧安装目录内已存在的可读库。")
    uri = "file:" + quote(str(filesystem_path(database)), safe="/\\:") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                _fail("DATABASE_INVALID", "账本完整性或外键检查失败。")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")} if "schema_migrations" in tables else set()
            supported = {int(p.stem.split("_", 1)[0]) for p in (Path(__file__).parent / "migrations").glob("0*.sql")}
            if not applied or applied - supported or applied != {v for v in supported if v <= max(applied)}:
                _fail("SCHEMA_UNSUPPORTED", "账本版本未知或不完整，只能先原样备份，不能自动接续。")
            if receipt_version is not None and receipt_version not in applied:
                _fail('RESTORE_RECEIPT_INVALID', '恢复回执库版本不是当前账本已知历史版本。')
            if "jobs" in tables and connection.execute("SELECT 1 FROM jobs WHERE status IN ('queued','pending','running') LIMIT 1").fetchone():
                _fail("SOURCE_BUSY", "源安装仍有排队或运行作业，需维护人员停止并核对。")
            db = CatalogDatabase(database, connection)
            tasks = []
            if "tasks" in tables:
                for row in connection.execute("SELECT task_id FROM tasks ORDER BY task_id").fetchall():
                    task = get_task(db, row[0])
                    if task.get("brief_json") and not isinstance(task.get("brief"), dict):
                        _fail("TASK_RECORD_INVALID", "旧任务文案记录格式损坏，不能按空任务接续。")
                    brief = task.get("brief") or {}
                    expected = (brief.get("final_script") or {}).get("sha256") or brief.get("script_draft_sha256")
                    if expected is None:
                        tasks.append({"taskId": row[0], "scriptStatus": "no_registered_script"})
                        continue
                    text = read_task_script(source, task)
                    if text is None:
                        _fail("SCRIPT_UNAVAILABLE", "旧任务原稿缺失，单独账本不能完成接续。")
                    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    if actual != expected:
                        _fail("SCRIPT_CHANGED", "旧任务原稿与账本摘要不一致，不能重新绑定。")
                    tasks.append({"taskId": row[0], "scriptStatus": "verified" if text else "empty_draft", "scriptSha256": actual})
            voices = _voice_checks(db, source, tables)
            legacy, legacy_bindings = _legacy_voice_checks(db, source) if "tasks" in tables else ([], {})
            files = _managed_files(source, database.parent / "cache")
            by_path = {Path(item["relativePath"]): item for item in files}
            for relative, expected in legacy_bindings.items():
                actual = by_path.get(Path(relative))
                if actual is None or actual["sha256"] != expected:
                    _fail("SOURCE_CHANGED", "旧配音材料在核验与盘点之间变化，请保留源安装。")
            for task in tasks:
                if task["scriptStatus"] != "verified":
                    continue
                script_file = by_path.get(Path("runtime/workbench/tasks") / task["taskId"] / "inputs/final-script.txt")
                if script_file is None or script_file["sha256"] != task["scriptSha256"]:
                    _fail("SOURCE_CHANGED", "原稿在核验与文件盘点之间变化，请保留源安装。")
            from .upgrade_frozen_references import inspect_frozen_references
            frozen = inspect_frozen_references(db, source, tables, original_reference_root=reference_root)
            libraries = [dict(row) for row in connection.execute("SELECT library_id,mount_path,online FROM libraries ORDER BY library_id")] if "libraries" in tables else []
            return {"formatVersion": 1, "phase": "managed-input-inspection", "sourceRoot": str(source), "originalReferenceRoot": str(reference_root),
                    "databaseRelativePath": database.relative_to(source).as_posix(), "destinationRoot": str(destination),
                    "schemaVersion": max(applied), "databaseLogicalSha256": _logical_digest(connection),
                    "managedFiles": files, "tasks": tasks, "registeredVoices": voices, "legacyVoices": legacy, "libraryReferences": libraries, "frozenReferences": frozen,
                    "oldRootMustBePreserved": True, "externalReferencesAuditComplete": frozen["auditComplete"],
                    "snapshotInputsComplete": frozen["auditComplete"],
                    "readyForSnapshot": False, "canSwitchInstallation": False,
                    "remainingChecks": (["未解决引用：" + item["code"] for item in frozen["unresolved"]] + ["原服务归属与停机窗口", "完整快照及不覆盖恢复", "旧任务回读及新任务验证"])}
    except sqlite3.Error:
        _fail("DATABASE_UNAVAILABLE", "账本无法只读检查，请保留原目录交维护人员处理。")
