"""Trusted route-aware actions executed by the persistent workbench queue."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .assemble import assemble
from .digital_workflow import _default_process_runner
from .model_profiles import get_task_profile_binding_for_usage
from .planning_finalize import finalize_digital_storyboard
from .preview import generate_asset_previews
from .provenance import latest_gate
from .rough_media import resolve_rough_media
from .render_paths import reserve_candidate_paths
from .scanner import scan_library
from .snapshots import file_content_signature, latest_review_candidate, latest_retryable_render_snapshot
from .task_sources import require_bound_source_readiness, require_source_readiness, sync_task_sources
from .tasks import get_task
from .tools import default_ffprobe_search_roots, find_ffmpeg
from .transcript_reconciliation import reconcile_transcript, project_transcript_state
from .workflow import complete_stage, project_gates


def _canonical_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _task_context(db, job, project_root) -> tuple[Path, dict]:
    root = Path(project_root).resolve()
    task_id = job.task_id or job.payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("TASK_ID_REQUIRED: 后台动作缺少任务编号")
    if job.task_id is not None and job.payload.get("task_id", task_id) != task_id:
        raise ValueError("TASK_ID_MISMATCH: 后台任务编号不一致")
    if any(part in task_id for part in ("/", "\\")) or task_id in {".", ".."}:
        raise ValueError("INVALID_TASK_ID: 任务编号包含非法路径字符")
    task = get_task(db, task_id)
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    if task.get("task_type") not in {"digital_human", "ad_mix"}:
        raise ValueError("UNSUPPORTED_ROUTE: 当前动作不支持该视频类型")
    task_root = (
        root / "runtime" / "workbench" / "tasks" / task_id
    ).resolve()
    trusted_tasks = (root / "runtime" / "workbench" / "tasks").resolve()
    if not _contained(task_root, trusted_tasks):
        raise ValueError("INVALID_TASK_ID: 任务目录越界")
    return task_root, task


def _locked_inputs(db, job, project_root) -> tuple[Path, dict, Path, Path, str]:
    task_root, task = _task_context(db, job, project_root)
    if latest_gate(db, task["task_id"], "brief") is None:
        raise RuntimeError("BRIEF_GATE_MISSING: 必须先锁定最终文案")
    brief = task.get("brief")
    if not isinstance(brief, dict):
        raise ValueError("BRIEF_INVALID: 任务文案记录无效")
    script_record = brief.get("final_script")
    source_record = brief.get("digital_human_source")
    if not isinstance(script_record, dict) or not isinstance(source_record, dict):
        raise ValueError("BRIEF_INVALID: 任务缺少锁定文案或数字人来源")
    relative_script = script_record.get("relative_path")
    if not isinstance(relative_script, str):
        raise ValueError("BRIEF_INVALID: 锁定文案路径无效")
    script_path = (task_root / relative_script).resolve()
    if not _contained(script_path, task_root) or not script_path.is_file():
        raise ValueError("LOCKED_SCRIPT_MISSING: 锁定文案文件不存在")
    script_sha256 = _file_sha256(script_path)
    if script_sha256 != script_record.get("sha256"):
        raise RuntimeError("LOCKED_SCRIPT_CHANGED: 锁定文案内容已经变化")
    source_path = Path(str(source_record.get("path") or "")).resolve()
    if not source_path.is_file():
        raise ValueError("DIGITAL_HUMAN_SOURCE_MISSING: 数字人口播源文件不存在")
    return task_root, brief, script_path, source_path, script_sha256


def _action_result(cursor: str, phase_label: str, result: dict) -> dict:
    return {
        "cursor": cursor,
        "progress": {"current": 1, "total": 1},
        "phase_label": phase_label,
        "result": result,
    }


def _run_checked(
    runner, command: list[str], *, cwd: Path, timeout_seconds: int,
    max_output_bytes: int = 64 * 1024, env=None,
):
    options = {
        "cwd": str(cwd),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
    }
    if env is not None:
        options["env"] = env
    outcome = runner(command, **options)
    if (
        isinstance(outcome, dict)
        and outcome.get("started") is not True
        and outcome.get("error_code") == "runtime.internal_error"
    ):
        # ATR can occasionally return a permanent runtime.internal_error before
        # the target process is created.  That is an ATR transport failure, not
        # a render failure.  Keep the same bounded timeout and fall back to the
        # Windows process-tree runner already used by the production renderer.
        from .render_mix_adapter import _run_process_tree

        try:
            completed = _run_process_tree(
                command,
                cwd=str(Path(cwd).resolve()),
                env=env,
                timeout=timeout_seconds,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            outcome = {
                "started": True,
                "timed_out": True,
                "returncode": None,
                "stdout": "",
                "stderr": "",
            }
        else:
            outcome = {
                "started": True,
                "timed_out": False,
                "returncode": completed.returncode,
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
            }
    if not isinstance(outcome, dict) or not outcome.get("started", True):
        raise RuntimeError("PROCESS_START_FAILED: 本机处理进程未能启动")
    if outcome.get("timed_out"):
        raise RuntimeError("PROCESS_TIMEOUT: 本机处理超时，可从当前步骤恢复")
    if outcome.get("returncode") != 0:
        detail = str(outcome.get("stderr") or outcome.get("stdout") or "执行失败")
        raise RuntimeError(f"PROCESS_FAILED: {detail[:500]}")
    return outcome


def _as_subprocess_runner(runner):
    """Adapt the bounded workbench runner to subprocess.run's result contract."""

    def run(command, **options):
        timeout = int(options.get("timeout", 1800))
        outcome = runner(
            command,
            cwd=Path(options.get("cwd", Path.cwd())),
            timeout_seconds=timeout,
            max_output_bytes=1024 * 1024,
            env=options.get("env"),
        )
        if (
            isinstance(outcome, dict)
            and outcome.get("started") is not True
            and outcome.get("error_code") == "runtime.internal_error"
        ):
            from .render_mix_adapter import _run_process_tree

            fallback_options = dict(options)
            if fallback_options.pop("capture_output", False):
                fallback_options.setdefault("stdout", subprocess.PIPE)
                fallback_options.setdefault("stderr", subprocess.PIPE)
            return _run_process_tree(command, **fallback_options)
        if not isinstance(outcome, dict) or outcome.get("started") is not True:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout=str((outcome or {}).get("stdout") or ""),
                stderr=str((outcome or {}).get("error") or "进程未能启动"),
            )
        if outcome.get("timed_out"):
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output=outcome.get("stdout"),
                stderr=outcome.get("stderr"),
            )
        return subprocess.CompletedProcess(
            command,
            int(outcome.get("returncode", 1)),
            stdout=str(outcome.get("stdout") or ""),
            stderr=str(outcome.get("stderr") or ""),
        )

    return run


def _python_executable(root: Path) -> Path:
    windows = root / ".venv" / "Scripts" / "python.exe"
    if windows.is_file():
        return windows
    return root / ".venv" / "bin" / "python"


def _validate_words(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 词级时间戳文件无效") from error
    if not isinstance(value, list) or not value:
        raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 没有识别到词级时间戳")
    previous = -1.0
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("w"), str):
            raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 词级时间戳结构无效")
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 词级时间戳结构无效") from error
        if start < previous or end < start:
            raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 词级时间戳顺序无效")
        previous = end
    return value


def _promote_transcription(receipt_path: Path, binding: dict, source_path: Path) -> None:
    """Finish only a validated, input-bound publication owned by this action."""
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (receipt["input_digest"] != binding["input_digest"]
                or receipt["source_sha256"] != _file_sha256(source_path)):
            raise RuntimeError("TRANSCRIPTION_STALE: 转写发布期间输入已变化，不能混用结果")
        files = receipt["files"]
        if set(files) != {"transcript.srt", "transcript.words.json", "transcription.json"}:
            raise ValueError("invalid publication files")
        pending = []
        # Validate the entire set before publishing any remaining file.
        for name in ("transcript.srt", "transcript.words.json", "transcription.json"):
            entry = files[name]
            staged_name = entry["staged"]
            if not isinstance(staged_name, str) or Path(staged_name).name != staged_name:
                raise ValueError("invalid staged path")
            target = receipt_path.parent / name
            staged = receipt_path.parent / staged_name
            actual = target if target.exists() else staged
            if not actual.is_file() or _file_sha256(actual) != entry["sha256"]:
                raise RuntimeError("TRANSCRIPTION_PARTIAL: 转写发布文件缺失或已变更，请人工恢复")
            if not target.exists():
                pending.append((staged, target))
        for staged, target in pending:
            os.replace(staged, target)
        receipt_path.unlink()
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("TRANSCRIPTION_PARTIAL: 转写发布记录无效，请人工恢复") from error


def transcribe(db, job, project_root, runner):
    """Create task-scoped Whisper timing artifacts without overwriting them."""
    root = Path(project_root).resolve()
    from .task_script import require_transcription_inputs, ScriptDraftError
    def check_inputs():
        try:
            return require_transcription_inputs(
                db, job.task_id, job.payload.get("input_digest"),
                script_sha256=job.payload.get("script_sha256"), project_root=root,
            )
        except ScriptDraftError as error:
            raise RuntimeError(f"{error.code}: {error.message}") from error
    brief_gate_id = check_inputs()
    task_root, _brief, script_path, source_path, script_sha256 = _locked_inputs(
        db, job, root
    )
    artifacts = task_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    srt_path = artifacts / "transcript.srt"
    words_path = artifacts / "transcript.words.json"
    metadata_path = artifacts / "transcription.json"
    if job.payload.get('recovery'):
        if job.payload['recovery'].get('brief_event_id') != brief_gate_id:
            raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 文案已重新确认，请重新发起恢复')
        from .transcript_recovery import archive_for_recovery, prepare_legacy_storyboard_recovery
        job = prepare_legacy_storyboard_recovery(db, root, job)
        archive_for_recovery(root, job, source_path)
    source_stat = source_path.stat()
    binding = {
        "task_id": job.task_id,
        "script_sha256": script_sha256,
        "source_path": str(source_path),
        "source_size_bytes": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "model": str(job.payload.get("model") or "small"),
    }
    binding["input_digest"] = _canonical_digest(binding)
    receipt_path = artifacts / ".transcription-promotion.json"
    if receipt_path.exists():
        _promote_transcription(receipt_path, binding, source_path)

    if metadata_path.is_file() and srt_path.is_file() and words_path.is_file():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("TRANSCRIPTION_STALE: 已有转写绑定记录无效") from error
        if existing.get("input_digest") != binding["input_digest"]:
            raise RuntimeError("TRANSCRIPTION_STALE: 锁定输入已变化，不能覆盖旧转写")
        if (not srt_path.read_bytes().strip()
                or existing.get("srt_sha256") not in (None, _file_sha256(srt_path))):
            raise RuntimeError("TRANSCRIPTION_STALE: 字幕文件已变化，原文件已保留，请恢复后重试")
        words = _validate_words(words_path)
        if existing.get("words_sha256") not in (None, _file_sha256(words_path)):
            raise RuntimeError("TRANSCRIPTION_STALE: 词级时间戳已变化")
        reconciliation = reconcile_transcript(
            script_path.read_text(encoding="utf-8"), words
        )
        if existing.get("reconciliation") != reconciliation:
            existing["words_sha256"] = _file_sha256(words_path)
            existing["reconciliation"] = reconciliation
            temporary = artifacts / f".transcription.{uuid.uuid4().hex}.upgrade.json"
            temporary.write_text(
                json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, metadata_path)
        return _action_result(
            "transcribe:complete",
            "语音识别完成",
            {
                "reused": True,
                "srt_path": str(srt_path.resolve()),
                "words_path": str(words_path.resolve()),
                "reconciliation": reconciliation,
            },
        )
    if any(path.exists() for path in (metadata_path, srt_path, words_path)):
        raise RuntimeError("TRANSCRIPTION_PARTIAL: 检测到不完整转写结果，请人工恢复")

    expected_source_sha256 = _file_sha256(source_path)
    if job.payload.get('recovery') and expected_source_sha256 != job.payload['recovery']['source_sha256']:
        raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 恢复期间原口播视频发生变化')

    # Job IDs can exceed Windows path limits; task/input binding stays in metadata.
    token = uuid.uuid4().hex
    part_srt = artifacts / f".transcript.{token}.part.srt"
    part_words = artifacts / f".transcript.{token}.part.words.json"
    part_metadata = artifacts / f".transcription.{token}.part.json"
    for part in (part_srt, part_words, part_metadata):
        if part.exists():
            part.unlink()
    command = [
        str(_python_executable(root)),
        "-X",
        "utf8",
        str(root / "scripts" / "transcribe.py"),
        str(source_path),
        binding["model"],
        "--srt-output",
        str(part_srt),
        "--words-output",
        str(part_words),
    ]
    try:
        _run_checked(runner, command, cwd=root, timeout_seconds=3600)
        if not part_srt.is_file() or not part_srt.read_text(encoding="utf-8").strip():
            raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: SRT 输出为空")
        words = _validate_words(part_words)
        current_stat = source_path.stat()
        if (current_stat.st_size != source_stat.st_size
                or current_stat.st_mtime_ns != source_stat.st_mtime_ns):
            raise RuntimeError("TRANSCRIPTION_STALE: 识别期间口播底片发生变化")
        source_sha256 = _file_sha256(source_path)
        if source_sha256 != expected_source_sha256:
            raise RuntimeError("TRANSCRIPTION_STALE: 识别期间口播底片内容发生变化")
        if _file_sha256(script_path) != script_sha256:
            raise RuntimeError("TRANSCRIPTION_STALE: 识别期间锁定文案发生变化")
        if check_inputs() != brief_gate_id:
            raise RuntimeError("TRANSCRIPTION_INPUT_CHANGED: 识别期间文案确认版本已变化")
        reconciliation = reconcile_transcript(
            script_path.read_text(encoding="utf-8"), words
        )
        binding.update(
            {
                "source_sha256": source_sha256,
                "srt_sha256": _file_sha256(part_srt),
                "words_sha256": _file_sha256(part_words),
                "word_count": len(words),
                "reconciliation": reconciliation,
            }
        )
        part_metadata.write_text(
            json.dumps(binding, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _atomic_json(receipt_path, {
            "input_digest": binding["input_digest"],
            "source_sha256": source_sha256,
            "files": {
                target.name: {"staged": staged.name, "sha256": _file_sha256(staged)}
                for staged, target in ((part_srt, srt_path), (part_words, words_path),
                                       (part_metadata, metadata_path))
            },
        })
        _promote_transcription(receipt_path, binding, source_path)
    except BaseException:
        # A durable receipt owns the staged files until a safe retry finishes.
        if receipt_path.exists():
            raise
        for part in (part_srt, part_words, part_metadata):
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return _action_result(
        "transcribe:complete",
        "语音识别完成",
        {
            "reused": False,
            "srt_path": str(srt_path.resolve()),
            "words_path": str(words_path.resolve()),
            "reconciliation": binding["reconciliation"],
        },
    )


def sync_sources(db, job, project_root, runner):
    """Synchronize the task's frozen source scope and record the stage."""
    _ = runner
    _task_root, task = _task_context(db, job, project_root)
    if latest_gate(db, task["task_id"], "brief") is None:
        raise RuntimeError("BRIEF_GATE_MISSING: 必须先锁定最终文案")
    impact_token = job.payload.get("impact_token")
    if impact_token is not None and not isinstance(impact_token, str):
        raise ValueError("IMPACT_TOKEN_INVALID: 素材同步确认令牌无效")
    result = sync_task_sources(
        db,
        task["task_id"],
        impact_token=impact_token,
    )
    stage_payload = {
        "changed": bool(result["changed"]),
        "configuration_changed": bool(result["configuration_changed"]),
        "health_changed": bool(result["health_changed"]),
        "snapshot_ids": list(result["snapshot_ids"]),
        "sources": result["sources"],
    }
    complete_stage(
        db,
        task["task_id"],
        "material_sync",
        input_digest=_canonical_digest(stage_payload),
        details=stage_payload,
    )
    return _action_result(
        "sync_sources:complete",
        "素材增量同步完成",
        result,
    )


def scan_catalog(db, job, project_root, runner):
    """Catalog one registered local material library for the workbench."""
    _ = project_root, runner
    library_id = job.payload.get("library_id")
    if not isinstance(library_id, str) or not library_id.strip():
        raise ValueError("LIBRARY_ID_REQUIRED: 素材检查缺少目录编号")
    summary = scan_library(db, library_id)
    from .local_bgm import index_scanned_folder
    index_scanned_folder(db, library_id)
    return _action_result(
        "scan:complete",
        "素材目录检查完成",
        {
            "library_id": library_id,
            "catalog": {
                "discovered": summary.discovered,
                "completed": summary.completed,
                "failed": summary.failed,
                "remaining": summary.remaining,
            },
        },
    )


def generate_preview(db, job, project_root, runner):
    """Generate a browser-safe task-scoped proxy without modifying the source."""
    task_root, task = _task_context(db, job, project_root)
    _ = task_root
    asset_id = job.payload.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError("ASSET_ID_REQUIRED: 代理预览任务缺少素材编号")
    resolve_rough_media(db, task["task_id"], asset_id, "preview")
    ffmpeg = find_ffmpeg(
        os.environ,
        search_roots=default_ffprobe_search_roots(),
    )
    produced = generate_asset_previews(
        db,
        asset_id,
        ffmpeg,
        {"thumbnail", "proxy"},
        runner=_as_subprocess_runner(runner),
    )
    return _action_result(
        "generate_preview:complete",
        "代理预览生成完成",
        {"asset_id": asset_id, **produced},
    )


def _storyboard_recommendation_context(db, job, project_root):
    """Compute the current bindings shared by generation and local recovery."""
    root = Path(project_root).resolve()
    task_root, brief, script_path, source_path, script_sha256 = _locked_inputs(
        db, job, root
    )
    task_id = job.task_id
    require_source_readiness(db, task_id)
    artifacts = task_root / "artifacts"
    words_path = artifacts / "transcript.words.json"
    transcription_path = artifacts / "transcription.json"
    if not words_path.is_file() or not transcription_path.is_file():
        raise RuntimeError("TRANSCRIPTION_REQUIRED: 必须先完成语音识别和词级时间戳")
    words = _validate_words(words_path)
    try:
        transcription = json.loads(transcription_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("TRANSCRIPTION_REQUIRED: 转写绑定记录无效") from error
    if not isinstance(transcription, dict):
        raise RuntimeError("TRANSCRIPTION_REQUIRED: 转写绑定记录必须是对象")
    if transcription.get("script_sha256") != script_sha256:
        raise RuntimeError("TRANSCRIPTION_STALE: 转写未绑定当前锁定文案")
    source_stat = source_path.stat()
    if (transcription.get("source_path") != str(source_path)
            or transcription.get("source_size_bytes") != source_stat.st_size
            or transcription.get("source_mtime_ns") != source_stat.st_mtime_ns):
        raise RuntimeError("TRANSCRIPTION_STALE: 数字人口播底片已变化")
    if transcription.get("words_sha256") not in (None, _file_sha256(words_path)):
        raise RuntimeError("TRANSCRIPTION_STALE: 词级时间戳已变化")
    transcript_state = project_transcript_state(
        root, get_task(db, task_id), verify_source_content=True
    )
    if transcript_state["status"] in {"pending", "stale"}:
        raise RuntimeError("TRANSCRIPTION_STALE: 口播核验依据缺失或已变化")
    if transcript_state["status"] == "needs_review":
        raise RuntimeError("TRANSCRIPT_REVIEW_REQUIRED: 口播与锁定文案存在关键疑点，请回听核对")

    source_rows = db.connection.execute(
        "SELECT snapshot_id FROM task_source_snapshots WHERE task_id=? ORDER BY rowid",
        (task_id,),
    ).fetchall()
    source_snapshot_ids = [row["snapshot_id"] for row in source_rows]
    if not source_snapshot_ids:
        raise RuntimeError("SOURCE_SYNC_REQUIRED: 必须先完成本次素材增量同步")
    input_binding = {
        "task_id": task_id,
        "script_sha256": script_sha256,
        "transcription_digest": transcription.get("input_digest"),
        "words_sha256": _file_sha256(words_path),
        "source_snapshot_ids": source_snapshot_ids,
        "profile": str(brief.get("subtype") or "zhaoshang"),
    }
    model_capability = "storyboard"
    model_binding = get_task_profile_binding_for_usage(
        db, task_id, model_capability
    )
    if model_binding is None:
        model_capability = "general_text"
        model_binding = get_task_profile_binding_for_usage(
            db, task_id, model_capability
        )
    packaging_binding = (
        get_task_profile_binding_for_usage(db, task_id, "packaging")
        if model_capability == "storyboard"
        else None
    )
    if model_capability == "storyboard" and packaging_binding is None:
        raise RuntimeError(
            "MODEL_BINDING_INCOMPLETE: 数字人规划必须同时冻结分镜模型和包装模型"
        )
    if model_binding is not None:
        input_binding["model_profile_digest"] = model_binding.get("configDigest")
    if packaging_binding is not None:
        input_binding["packaging_profile_digest"] = packaging_binding.get(
            "configDigest"
        )
    input_digest = _canonical_digest(input_binding)
    return {"root": root, "task_id": task_id, "artifacts": artifacts,
            "script_path": script_path, "source_path": source_path,
            "transcription": transcription, "input_binding": input_binding,
            "input_digest": input_digest, "model_binding": model_binding,
            "packaging_binding": packaging_binding, "model_capability": model_capability}


def _validated_storyboard_recovery(db, job, project_root):
    _task_root, task = _task_context(db, job, project_root)
    if task.get("task_type") != "digital_human" or task.get("orchestration_mode") != "standalone":
        raise RuntimeError("STORYBOARD_RECOVERY_UNSUPPORTED: 仅支持独立数字人任务的已生成分镜恢复")
    if not project_gates(db, task["task_id"])["brief_locked"]:
        raise RuntimeError("BRIEF_GATE_MISSING: 当前文案锁定已失效，必须重新确认最终文案")
    context = _storyboard_recommendation_context(db, job, project_root)
    binding = context["input_binding"]
    transcription = context["transcription"]
    if (transcription.get("task_id") != context["task_id"]
            or not isinstance(transcription.get("input_digest"), str)
            or not transcription["input_digest"]
            or transcription.get("words_sha256") != binding["words_sha256"]
            or not transcription.get("source_sha256")
            or not binding.get("model_profile_digest")
            or not binding.get("packaging_profile_digest")):
        raise RuntimeError("STORYBOARD_RECOVERY_BINDING_REQUIRED: 转写、底片或冻结规划模型缺少完整绑定，不能仅本地恢复")
    directory = context["artifacts"] / "storyboard"
    paths = {"brief": directory / "brief.json", "plan": directory / "plan.json"}
    metadata_path = directory / "recommendation.json"
    if not metadata_path.is_file() or any(not path.is_file() for path in paths.values()):
        raise RuntimeError("STORYBOARD_RECOVERY_REQUIRED: 没有完整的已生成分镜缓存；本入口不会调用模型")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError("STORYBOARD_RECOVERY_INVALID: 已生成分镜绑定记录无效；本入口不会调用模型") from error
    if not isinstance(metadata.get("state"), str) or metadata["state"] not in {"generated", "complete"}:
        raise RuntimeError("STORYBOARD_RECOVERY_REQUIRED: 缓存尚未生成完成；本入口不会调用模型")
    if (metadata.get("source") != "standalone_model"
            or metadata.get("input_digest") != context["input_digest"]
            or any(metadata.get(key) != value for key, value in binding.items())
            or any(metadata.get(key + "_sha256") != _file_sha256(path)
                   for key, path in paths.items())):
        raise RuntimeError("STORYBOARD_STALE: 当前任务、输入绑定或分镜文件已变化，拒绝仅本地恢复")
    try:
        content = {key: json.loads(path.read_text(encoding="utf-8")) for key, path in paths.items()}
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError("STORYBOARD_RECOVERY_INVALID: 已生成分镜文件无效；本入口不会调用模型") from error
    from .planning_finalize import validate_digital_storyboard_content
    validate_digital_storyboard_content(content, context["script_path"].read_text(encoding="utf-8"))
    return context, content


def storyboard_recovery_availability(db, task_id, project_root):
    """Read-only, on-demand availability; never claim a cache is recoverable by name alone."""
    from types import SimpleNamespace
    job = SimpleNamespace(task_id=task_id, payload={"task_id": task_id})
    task_root, task = _task_context(db, job, project_root)
    if task.get("task_type") != "digital_human" or task.get("orchestration_mode") != "standalone":
        return {"available": False, "reason": "unsupported", "message": "当前任务不使用数字人分镜缓存恢复。"}
    directory = task_root / "artifacts" / "storyboard"
    if not all((directory / name).is_file() for name in ("brief.json", "plan.json", "recommendation.json")):
        return {"available": False, "reason": "no_cache", "message": "尚无完整的已生成分镜缓存。"}
    try:
        validate_storyboard_recovery(db, task_id, project_root)
    except (RuntimeError, ValueError, OSError, KeyError, TypeError):
        # Detailed validation errors belong to the explicit recovery action;
        # availability must not expose local paths or suggest stale cache reuse.
        return {"available": False, "reason": "cache_not_current",
                "message": "已生成缓存与当前任务不匹配或未通过完整性检查。"}
    return {"available": True, "reason": "ready", "message": "可恢复当前任务已生成的分镜，无需再次调用模型。"}


def validate_storyboard_recovery(db, task_id, project_root):
    """Read-only preflight; no cards, files, processes, or model calls."""
    from types import SimpleNamespace
    job = SimpleNamespace(task_id=task_id, payload={"task_id": task_id})
    context, _content = _validated_storyboard_recovery(db, job, project_root)
    return {"task_id": task_id, "input_digest": context["input_digest"]}


def recover_storyboard(db, job, project_root, runner):
    """Finalize verified cached content; generation is not a reachable branch."""
    if (job.job_type != "recover_storyboard"
            or job.payload.get("action") != "recover_storyboard"
            or job.payload.get("local_recovery_only") is not True
            or not isinstance(job.payload.get("expected_input_digest"), str)):
        raise RuntimeError("STORYBOARD_RECOVERY_REQUEST_INVALID: 本地恢复请求缺少服务端约束")
    context, content = _validated_storyboard_recovery(db, job, project_root)
    if job.payload["expected_input_digest"] != context["input_digest"]:
        raise RuntimeError("STORYBOARD_STALE: 排队后的分镜恢复输入已变化，请刷新后重新核对")
    finalized = finalize_digital_storyboard(
        db, task_id=context["task_id"], project_root=context["root"], content=content,
        input_binding=context["input_binding"], input_digest=context["input_digest"],
        source="standalone_model")
    return _action_result("recover_storyboard:complete", "已生成分镜已仅本地恢复", finalized)


def recommend_storyboard(db, job, project_root, runner):
    """Generate one paid AI brief, then bind candidates to scoped local assets."""
    context = _storyboard_recommendation_context(db, job, project_root)
    root, task_id, artifacts = context["root"], context["task_id"], context["artifacts"]
    script_path, input_binding = context["script_path"], context["input_binding"]
    input_digest, model_binding = context["input_digest"], context["model_binding"]
    packaging_binding, model_capability = context["packaging_binding"], context["model_capability"]
    storyboard_dir = artifacts / "storyboard"
    storyboard_dir.mkdir(parents=True, exist_ok=True)
    brief_path = storyboard_dir / "brief.json"
    plan_path = storyboard_dir / "plan.json"
    metadata_path = storyboard_dir / "recommendation.json"

    metadata = None
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("STORYBOARD_STALE: 分镜绑定记录无效") from error
        if metadata.get("input_digest") != input_digest:
            raise RuntimeError("STORYBOARD_STALE: 锁定文案、时间或素材范围已变化")
        if metadata.get("state") == "complete":
            try:
                completed_content = {
                    "brief": json.loads(brief_path.read_text(encoding="utf-8")),
                    "plan": json.loads(plan_path.read_text(encoding="utf-8")),
                }
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    "STORYBOARD_PARTIAL: 已完成分镜文件无效"
                ) from error
            finalized = finalize_digital_storyboard(
                db,
                task_id=task_id,
                project_root=root,
                content=completed_content,
                input_binding=input_binding,
                input_digest=input_digest,
                source="standalone_model",
            )
            return _action_result(
                "recommend_storyboard:complete",
                "AI 分镜候选已生成",
                finalized,
            )

    if metadata is None:
        if brief_path.exists() or plan_path.exists():
            raise RuntimeError("STORYBOARD_PARTIAL: 检测到无绑定的分镜输出")
        node = shutil.which("node") or "node"
        ts_node = root / "node_modules" / "ts-node" / "dist" / "bin.js"
        token = re.sub(r"[^A-Za-z0-9_.-]", "_", job.job_id)
        part_brief = storyboard_dir / f".brief.{token}.part.json"
        part_plan = storyboard_dir / f".plan.{token}.part.json"
        for part in (part_brief, part_plan):
            part.unlink(missing_ok=True)
        command = [
            str(_python_executable(root)),
            "-X",
            "utf8",
            str(root / "scripts" / "plan_brief_runner.py"),
            "--project-root", str(root),
            "--node", str(Path(node).resolve()),
            "--ts-node", str(ts_node.resolve()),
            "--script",
            str(script_path),
            "--brief-output",
            str(part_brief),
            "--plan-output",
            str(part_plan),
            "--speed",
            "1.25",
            "--profile",
            input_binding["profile"],
        ]
        if model_binding is not None:
            command.extend([
                "--catalog-db", str(Path(db.path).resolve()),
                "--task-id", task_id,
                "--capability", model_capability,
            ])
            if packaging_binding is not None:
                command.extend(["--packaging-capability", "packaging"])
        else:
            setting = db.connection.execute(
                "SELECT model FROM capability_settings WHERE capability='general_text'"
            ).fetchone()
            model = setting["model"] if setting is not None else None
            if not model or model == "未选择模型":
                raise RuntimeError(
                    "GENERAL_TEXT_NOT_CONFIGURED: 请先在设置中配置文案与分镜模型"
                )
            command.extend(["--model", str(model)])
        try:
            _run_checked(
                runner,
                command,
                cwd=root,
                timeout_seconds=300,
            )
            ai_brief = _validate_storyboard_brief(part_brief, script_path)
            try:
                ai_plan = json.loads(part_plan.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件无效") from error
            if not isinstance(ai_plan, dict):
                raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件必须是对象")
            os.replace(part_brief, brief_path)
            os.replace(part_plan, plan_path)
        except BaseException:
            part_brief.unlink(missing_ok=True)
            part_plan.unlink(missing_ok=True)
            raise
        metadata = {
            **input_binding,
            "input_digest": input_digest,
            "source": "standalone_model",
            "state": "generated",
            "brief_sha256": _file_sha256(brief_path),
            "plan_sha256": _file_sha256(plan_path),
            "missing": [],
        }
        _atomic_json(metadata_path, metadata)
    else:
        ai_brief = _validate_storyboard_brief(brief_path, script_path)
        try:
            ai_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件无效") from error
        if not isinstance(ai_plan, dict):
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件必须是对象")

    finalized = finalize_digital_storyboard(
        db,
        task_id=task_id,
        project_root=root,
        content={"brief": ai_brief, "plan": ai_plan},
        input_binding=input_binding,
        input_digest=input_digest,
        source="standalone_model",
    )
    return _action_result(
        "recommend_storyboard:complete",
        "AI 分镜候选已生成",
        finalized,
    )


def _render_input_digest(db, task_id: str, *, rework_override: dict | None = None) -> str:
    from .snapshots import render_epoch
    gates = {}
    for gate_type in ("brief", "picture_lock", "packaging"):
        row = latest_gate(db, task_id, gate_type)
        gates[gate_type] = None if row is None else dict(row)
    cards = [
        dict(row)
        for row in db.connection.execute(
            "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id",
            (task_id,),
        ).fetchall()
    ]
    assets = [
        dict(row)
        for row in db.connection.execute(
            "SELECT task_id,asset_id,role FROM task_assets "
            "WHERE task_id=? ORDER BY role,asset_id",
            (task_id,),
        ).fetchall()
    ]
    rework = db.connection.execute(
        """SELECT rework_id,execution_key FROM rework_requests
        WHERE task_id=? AND status='applied'
        ORDER BY updated_at DESC,rowid DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    voice=db.connection.execute('SELECT revision,context_digest,config_json,audio_json FROM voice_edit_heads WHERE task_id=?',(task_id,)).fetchone()
    managed=voice is not None and voice['revision']>0 and voice['audio_json'] is not None
    return _canonical_digest({
        **({'voice_version':dict(voice)} if managed else {}),
        "gates": gates,
        "render_epoch": render_epoch(db.connection, task_id),
        "cards": cards,
        "assets": assets,
        "rework": (None if rework is None else dict(rework)) if rework_override is None else (rework_override or None),
    })


def _qa_rework_input_matches(db, task_id: str, recorded_digest: str) -> bool:
    # Older mixes store only a digest, not its request component. Compare that
    # trusted digest against known historical requests while holding ALL current
    # render inputs fixed; candidate lineage is not a render-input identity.
    known_requests = [{}] + [dict(row) for row in db.connection.execute(
        "SELECT rework_id,execution_key FROM rework_requests WHERE task_id=? AND status='applied'",
        (task_id,),
    ).fetchall()]
    return any(_render_input_digest(db, task_id, rework_override=request) == recorded_digest
               for request in known_requests)


def _action_state_path(artifacts: Path, action: str, input_digest: str) -> Path:
    """Keep each input generation, including the previous fixed-name record, immutable."""
    versioned = artifacts / f"{action}.{input_digest}.json"
    if versioned.exists():
        return versioned
    legacy = artifacts / f"{action}.json"
    if legacy.is_file():
        try:
            previous = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("ACTION_STATE_INVALID: 旧动作绑定记录无效") from error
        if previous.get("input_digest") == input_digest:
            return legacy
    return versioned


def _write_render_completion(path: Path, state: dict) -> None:
    """Publish a complete receipt atomically, without replacing any existing one."""
    temporary = path.parent / f".render-{uuid.uuid4().hex}.part"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        try:
            if os.name == "nt":
                # Windows rename refuses an existing destination (unlike replace).
                temporary.rename(path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise RuntimeError("ACTION_STATE_INVALID: 已有完成记录无法核验") from error
            if existing != state:
                raise RuntimeError("ACTION_STATE_COLLISION: 已有完成记录属于另一绑定")
    finally:
        if temporary.exists():
            temporary.unlink()


def _bound_render_state_path(artifacts: Path, input_digest: str, qa_snapshot_id: str) -> Path:
    binding = hashlib.sha256(f"{input_digest}:{qa_snapshot_id}".encode("utf-8")).hexdigest()
    return artifacts / f"render-action.bound-{binding}.json"


def _render_snapshot_current(db, task_id: str, snapshot: dict,
                             *, refresh_authorization: bool = False) -> bool:
    from .render_mix_adapter import verify_catalog_snapshot, verify_dependency_snapshot, RenderMixJobError
    if refresh_authorization:
        from .qa_authorization import refresh_qa_authorization
        snapshot = refresh_qa_authorization(db, task_id, snapshot)
    try:
        return verify_dependency_snapshot(snapshot.get("dependency_snapshot") or {}) and verify_catalog_snapshot(
            db, task_id, Path(snapshot["output_path"]), snapshot.get("dependency_snapshot") or {},
        )
    except (OSError, RenderMixJobError):
        return False


def _validate_completed_render(db, task_id: str, state: dict) -> None:
    candidate = latest_review_candidate(db, task_id)
    bound = state.get("result", {}).get("snapshots", {})
    if candidate is None or candidate["qa_snapshot_id"] != bound.get("qa_snapshot_id"):
        raise RuntimeError("ACTION_CANDIDATE_CHANGED: 已完成动作不再绑定当前审片候选")
    if not _render_snapshot_current(db, task_id, candidate):
        raise RuntimeError("ACTION_DEPENDENCY_CHANGED: 已完成动作的门禁或依赖已变化")
    if file_content_signature(candidate["output_path"]) != candidate["output_signature"]:
        raise RuntimeError("ACTION_OUTPUT_CHANGED: 候选文件与快照内容不一致")


def _completed_action(path: Path, input_digest: str, output_path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("ACTION_STATE_INVALID: 后台动作绑定记录无效") from error
    if metadata.get("input_digest") != input_digest:
        raise RuntimeError("ACTION_STATE_STALE: 已完成动作与当前锁定输入不一致")
    if not output_path.is_file() or metadata.get("output_sha256") != _file_sha256(output_path):
        raise RuntimeError("ACTION_OUTPUT_CHANGED: 已完成动作的输出文件已经变化")
    return metadata


def render_and_qa(db, job, project_root, runner):
    """Render one immutable candidate and run QA, reusing trusted output on reentry."""
    require_bound_source_readiness(db, job.task_id)
    root = Path(project_root).resolve()
    task_root, _brief, script_path, _source_path, _script_sha256 = _locked_inputs(
        db, job, root
    )
    task_id = job.task_id
    artifacts = task_root / "artifacts"
    # Use exactly the same locked sources as the preview. A restored task may
    # retain validated semantics and scene cards without the legacy AI brief.
    from .packaging_preview import _digital_subtitle_sources
    from .subtitles import SubtitleSemanticsError, validate_semantics_file

    subtitle_sources = _digital_subtitle_sources(db, task_id, root)
    if Path(subtitle_sources["script_path"]).resolve() != script_path.resolve():
        raise RuntimeError("ASS_SOURCE_MISMATCH: 字幕文案与当前锁定文案不一致")
    if not Path(subtitle_sources["words_path"]).is_file():
        raise RuntimeError("TRANSCRIPT_TIMING_MISSING: 字幕时间轴缺失，请先恢复口播识别结果")
    if not Path(subtitle_sources["semantics_path"]).is_file():
        raise RuntimeError("SUBTITLE_SEMANTICS_MISSING: 字幕重点词记录缺失，请先恢复字幕数据")
    try:
        validate_semantics_file(Path(subtitle_sources["semantics_path"]))
    except SubtitleSemanticsError as error:
        raise RuntimeError("ASS_SOURCE_INVALID: 字幕重点词记录损坏，请先恢复字幕数据") from error

    input_digest = _render_input_digest(db, task_id)
    from .performance import identity
    identity(input_digest, "render_action")
    candidate = latest_review_candidate(db, task_id)
    state_path = _action_state_path(artifacts, "render-action", input_digest)
    if candidate is not None:
        bound_path = _bound_render_state_path(artifacts, input_digest, candidate["qa_snapshot_id"])
        if bound_path.is_file():
            state_path = bound_path
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            recorded_output = Path(state["output_path"]).resolve()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("ACTION_STATE_INVALID: 渲染动作绑定记录无效") from error
        completed = _completed_action(state_path, input_digest, recorded_output)
        result = completed.get("result")
        bound = result.get("snapshots") if isinstance(result, dict) else None
        if not isinstance(bound, dict) or not bound.get("qa_snapshot_id"):
            raise RuntimeError("ACTION_STATE_INVALID: 渲染动作缺少质检快照绑定")
        if candidate is not None and candidate["qa_snapshot_id"] == bound["qa_snapshot_id"]:
            _validate_completed_render(db, task_id, completed)
            return _action_result(
                "render_and_qa:complete",
                "精剪成片与自动质检完成",
                {**result, "reused": True},
            )
        # A restored DB is authoritative. Preserve a later unbound completion
        # receipt and output, then use only current DB candidates or a new render.

    candidate = latest_review_candidate(db, task_id)
    retry = latest_retryable_render_snapshot(db, task_id)
    if candidate is not None and _render_snapshot_current(db, task_id, candidate):
        output_path = Path(candidate["output_path"]).resolve()
        if not output_path.is_file():
            raise RuntimeError("RENDER_OUTPUT_MISSING: 已审片候选文件不存在")
        if file_content_signature(output_path) != candidate["output_signature"]:
            raise RuntimeError("ACTION_OUTPUT_CHANGED: 候选文件与快照内容不一致")
        result = {
            "status": "awaiting_human_review",
            "output_path": str(output_path),
            "reused": True,
            "snapshots": candidate,
        }
    else:
        retry_current = retry is not None and _render_snapshot_current(
            db, task_id, retry, refresh_authorization=True,
        )
        if retry is not None and retry.get("qa_rework_id") and not retry_current:
            raise RuntimeError("QA_RETRY_INPUT_CHANGED: 原成片依赖已变化，不能仅重做质检")
        if retry_current:
            output_path = Path(retry["output_path"]).resolve()
        else:
            version_no = db.connection.execute(
                "SELECT COUNT(*) + 1 FROM render_snapshots "
                "WHERE task_id=? AND stage='mix'",
                (task_id,),
            ).fetchone()[0]
            output_path, work_dir = reserve_candidate_paths(root, task_root, task_id, version_no)
        if retry_current:
            work_dir = task_root / "render" / output_path.parent.name
            work_dir.mkdir(parents=True, exist_ok=True)
        subprocess_runner = _as_subprocess_runner(runner)
        assembled = assemble(
            db,
            task_id,
            render=True,
            output_path=output_path,
            work_dir=work_dir,
            runner=subprocess_runner,
            subtitle_runner=subprocess_runner,
            subtitle_sources=subtitle_sources,
        )
        if assembled.get("status") not in {"awaiting_human_review", "qa_blocked"}:
            raise RuntimeError(
                f"RENDER_WORKFLOW_BLOCKED: {assembled.get('message') or assembled.get('status')}"
            )
        result = dict(assembled)
        result["reused"] = bool(retry_current)

    if result["status"] == "awaiting_human_review":
        output_path = Path(result["output_path"]).resolve()
        state = {
            "input_digest": input_digest,
            "output_path": str(output_path),
            "output_sha256": _file_sha256(output_path),
            "result": result,
        }
        bound_path = _bound_render_state_path(
            artifacts, input_digest, result["snapshots"]["qa_snapshot_id"],
        )
        target = bound_path if state_path.exists() else state_path
        try:
            _write_render_completion(target, state)
        except RuntimeError as error:
            if target == bound_path or not str(error).startswith("ACTION_STATE_COLLISION:"):
                raise
            # Another DB may complete the same inputs at the same physical root.
            # Preserve its receipt and publish this DB's own bound completion.
            _write_render_completion(bound_path, state)
        return _action_result(
            "render_and_qa:complete",
            "精剪成片与自动质检完成",
            result,
        )
    return _action_result(
        "render_and_qa:qa_blocked",
        "自动质检发现阻断项",
        result,
    )


def build_publish_package(db, job, project_root, runner):
    """Build a release package from the exact human-approved review candidate."""
    require_bound_source_readiness(db, job.task_id)
    _ = runner
    root = Path(project_root).resolve()
    task_root, _task = _task_context(db, job, root)
    task_id = job.task_id
    candidate = latest_review_candidate(db, task_id)
    if candidate is None:
        raise RuntimeError("REVIEW_CANDIDATE_MISSING: 没有可发布的完整审片候选")
    expected_candidate_id = str(
        job.payload.get("candidate_id") or candidate.get("candidate_id") or ""
    )
    if candidate.get("candidate_id") != expected_candidate_id:
        raise RuntimeError("PUBLISH_CANDIDATE_STALE: 发布任务绑定的候选已不是当前版本")
    output_path = Path(candidate["output_path"]).resolve()
    if not output_path.is_file():
        raise RuntimeError("RENDER_OUTPUT_MISSING: 完整审片对应的候选成片不存在")
    payload = {
        **({"platform": job.payload["platform"]} if job.payload.get("platform") is not None else {}),
        "title": job.payload.get("title"),
        "description": job.payload.get("description"),
        "tags": list(job.payload.get("tags") or []),
        "cover_choice": str(job.payload.get("cover_choice") or "candidate-1"),
        "external_cover_path": job.payload.get("external_cover_path"),
        "expected_external_cover_signature": job.payload.get("expected_external_cover_signature"),
        "delivery_type": str(job.payload.get("delivery_type") or "standard"),
        "output_root": job.payload.get("output_root"),
    }
    input_digest = _canonical_digest(
        {
            "candidate": {
                "qa_snapshot_id": candidate.get("qa_snapshot_id"),
                "qa_report_id": candidate.get("qa_report_id"),
                "output_path": str(output_path),
                "output_sha256": _file_sha256(output_path),
            },
            "publish": payload,
        }
    )
    artifacts = task_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    state_path = _action_state_path(artifacts, "publish-action", input_digest)
    # Re-enter the trusted publish path on every retry to revalidate current approval
    # and package integrity. The package builder owns content idempotency.
    previous_state = _completed_action(state_path, input_digest, output_path)
    assembled = assemble(
        db,
        task_id,
        render=True,
        output_path=output_path,
        platform=payload.get("platform"),
        title=payload["title"],
        description=payload["description"],
        tags=payload["tags"],
        cover_choice=payload["cover_choice"],
        external_cover_path=payload["external_cover_path"],
        expected_external_cover_signature=payload["expected_external_cover_signature"],
        delivery_type=payload["delivery_type"],
        output_root=payload["output_root"],
        candidate_id=expected_candidate_id,
        delivery_retry_key=job.job_id,
    )
    if assembled.get("status") not in {"published", "publish_blocked"}:
        raise RuntimeError(
            f"PUBLISH_WORKFLOW_BLOCKED: {assembled.get('message') or assembled.get('status')}"
        )
    result = {**assembled, "reused": bool(previous_state) or bool(assembled.get("publish", {}).get("reused"))}
    _atomic_json(
        state_path,
        {
            "input_digest": input_digest,
            "output_path": str(output_path),
            "output_sha256": _file_sha256(output_path),
            "result": result,
        },
    )
    return _action_result(
        "build_publish_package:complete",
        "发布包生成完成",
        result,
    )


def run_packaging_preview_action(db, job, project_root, runner=None) -> dict:
    """Execute one durable packaging-preview job without changing task gates."""
    require_bound_source_readiness(db, job.task_id or job.payload.get("task_id"))
    from .packaging_preview import (
        _receipt_assets_current,
        current_preview_matches,
        get_preview_artifact,
        render_packaging_preview,
        resolve_packaging_preview_media,
        set_preview_status,
    )

    preview_id = job.payload.get("preview_id")
    if not isinstance(preview_id, str) or not preview_id:
        raise ValueError("PACKAGING_PREVIEW_ID_REQUIRED: 预览任务缺少记录编号")
    artifact = get_preview_artifact(db, preview_id)
    if artifact is None:
        raise KeyError(f"找不到包装预览：{preview_id}")
    task_id = job.task_id or job.payload.get("task_id")
    if artifact["task_id"] != task_id:
        raise PermissionError("PACKAGING_PREVIEW_NOT_OWNED: 预览不属于当前任务")
    root = Path(project_root).resolve()
    if artifact["status"] == "ready":
        # A worker can stop after publishing the artifact but before finishing
        # its durable job. Reuse only the still-current, signed output.
        if not current_preview_matches(db, artifact) or not _receipt_assets_current(artifact):
            result = set_preview_status(
                db, preview_id, "stale", error_code="PACKAGING_PREVIEW_STALE"
            )
        else:
            try:
                resolve_packaging_preview_media(db, preview_id, root)
            except RuntimeError as error:
                if str(error).partition(":")[0] == "PACKAGING_PREVIEW_MEDIA_CHANGED":
                    set_preview_status(
                        db, preview_id, "stale",
                        error_code="PACKAGING_PREVIEW_MEDIA_CHANGED",
                    )
                raise
            result = artifact
    else:
        result = render_packaging_preview(db, preview_id, root, runner)
    message = (
        "包装预览已生成"
        if result["status"] == "ready"
        else "包装输入已变化，预览已过期"
    )
    return _action_result("packaging_preview:complete", message, result)


def run_packaging_catalog_check_action(
    db, job, project_root, runner=None
) -> dict:
    from .packaging_catalog_check import run_packaging_catalog_check

    check_id = job.payload.get("check_id")
    if not isinstance(check_id, str) or not check_id:
        raise ValueError("PACKAGING_CATALOG_CHECK_ID_REQUIRED: 检查任务缺少记录编号")
    result = run_packaging_catalog_check(
        db, Path(project_root).resolve(), check_id=check_id
    )
    return _action_result(
        "packaging_catalog_check:complete", "包装素材检查完成", result
    )


def build_action_handlers(db, project_root, runner=None) -> dict:
    """Bind route-aware trusted actions to one worker-owned database connection."""
    from .ad_publish_actions import build_ad_publish_package
    from .ad_actions import (
        recommend_ad_storyboard,
        render_ad_and_qa,
    )
    from .initial_source_check import run_initial_source_check
    from .visual_batch_execution import run_visual_batch_action
    from .delivery_copy_jobs import run_delivery_copy_suggestion
    from .edit_intent import run_edit_intent
    from .creative_visual_advice import run_creative_advice
    from .content_review_jobs import run_content_review
    from .audio_review_jobs import run_audio_review
    from .packaging_suggestion_jobs import run_packaging_suggestion
    from .packaging_audio_jobs import run_packaging_audio
    from .scene_window_jobs import run_scene_window_job

    process_runner = runner or _default_process_runner
    root = Path(project_root).resolve()

    def bind(digital_action, ad_action=None):
        def handle(job):
            if job.payload.get("qa_rework_id"):
                from .snapshots import latest_qa_only_rework
                active = latest_qa_only_rework(db, job.task_id)
                if active is None or active["rework_id"] != job.payload["qa_rework_id"]:
                    raise RuntimeError("QA_REWORK_STALE: 这次仅重做质检的请求已过期，请刷新后重新确认。")
            task = get_task(db, job.task_id or job.payload.get("task_id"))
            if task is None:
                raise KeyError("任务不存在")
            action = (
                ad_action
                if task.get("task_type") == "ad_mix" and ad_action is not None
                else digital_action
            )
            from .performance import metric
            result = action(db, job, root, process_runner)
            detail = result.get("result", {}) if isinstance(result, dict) else {}
            if isinstance(detail, dict) and type(detail.get("reused")) is bool:
                metric("cache_hits", int(detail["reused"]))
            if digital_action is render_and_qa:
                from .intelligent_review import after_render
                result = after_render(db, root, job, result)
            return result
        return handle

    from .voice_jobs import run_voice_editing_job
    from .source_continue import run_source_continue

    return {
        "tts": lambda job: run_voice_editing_job(db,job,root,process_runner),
        "scan": lambda job: scan_catalog(db, job, root, process_runner),
        "generate_preview": bind(generate_preview),
        "transcribe": bind(transcribe),
        "sync_sources": bind(sync_sources),
        "source_continue": lambda job: run_source_continue(db, job, root, process_runner),
        "initial_source_check": lambda job: run_initial_source_check(db, job, root, process_runner),
        "recommend_storyboard": bind(recommend_storyboard, recommend_ad_storyboard),
        "recover_storyboard": bind(recover_storyboard),
        "render_and_qa": bind(render_and_qa, render_ad_and_qa),
        "build_publish_package": bind(
            build_publish_package, build_ad_publish_package
        ),
        "packaging_preview": lambda job: run_packaging_preview_action(
            db, job, root, process_runner
        ),
        "packaging_catalog_check": lambda job: run_packaging_catalog_check_action(
            db, job, root, process_runner
        ),
        "visual_analysis": lambda job: run_visual_batch_action(db, job, root, max_concurrency=2),
        "delivery_copy_suggestion": lambda job: run_delivery_copy_suggestion(db, job, root),
        "edit_intent": lambda job: run_edit_intent(db, job, root),
        "creative_visual_advice": lambda job: run_creative_advice(db, job, root),
        "content_review": lambda job: run_content_review(db, job, root),
        "audio_review": lambda job: run_audio_review(db, job, root),
        "packaging_suggestion": lambda job: run_packaging_suggestion(db, job, root),
        "packaging_audio_analysis": lambda job: run_packaging_audio(db, job, root),
        "scene_detection": lambda job: run_scene_window_job(db, job, root),
    }


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _subtitle_semantics(storyboard_dir: Path, ai_brief: dict) -> Path:
    semantics = {
        "keywords": ai_brief.get("keywords"),
        "punchLines": ai_brief.get("punchLines"),
    }
    for key, values in semantics.items():
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise RuntimeError(
                f"STORYBOARD_OUTPUT_INVALID: AI 分镜 {key} 必须是非空字符串数组"
            )
    path = Path(storyboard_dir) / "semantics.json"
    expected = json.dumps(
        semantics, ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n"
    if path.is_file():
        if path.read_text(encoding="utf-8") != expected:
            raise RuntimeError("STORYBOARD_STALE: 字幕语义派生产物与 AI 分镜不一致")
    else:
        _atomic_json(path, semantics)
    return path


def _validate_storyboard_brief(path: Path, script_path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜输出不是有效 JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("broll"), list):
        raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜输出结构无效")
    script = script_path.read_text(encoding="utf-8")
    for item in value["broll"]:
        if not isinstance(item, dict):
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜项结构无效")
        anchor = item.get("anchorText")
        if not isinstance(anchor, str) or not anchor or anchor not in script:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 锚点不属于锁定原文")
        if item.get("kind") not in {"broll", "image"}:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜素材类型无效")
        if not isinstance(item.get("query"), str):
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜缺少素材查询词")
    return value


def _anchor_time(
    script: str,
    words: list[dict],
    anchor: str,
) -> tuple[float, float]:
    """Mirror src/timeline.ts: map locked-script positions onto word timing.

    Whisper may return traditional characters, verbalized numbers, or imperfect
    proper nouns. The locked script is authoritative, so exact matching against
    ASR text would incorrectly reject valid anchors.
    """
    anchor_offset = script.find(anchor)
    if anchor_offset < 0:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: AI 锚点不属于锁定文案")

    timeline = []
    for item in words:
        characters = [char for char in item["w"] if not char.isspace()]
        if not characters:
            continue
        start = float(item["start"])
        end = float(item["end"])
        span = (end - start) / len(characters)
        timeline.extend(
            {
                "start": start + span * index,
                "end": start + span * (index + 1),
            }
            for index in range(len(characters))
        )
    if not timeline:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: 没有可用词级时间")

    spoken_rank = {}
    rank = 0
    for index, character in enumerate(script):
        if re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff%]", character):
            spoken_rank[index] = rank
            rank += 1
    if rank == 0:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: 锁定文案没有可定位文字")

    anchor_ranks = [
        spoken_rank[index]
        for index in range(anchor_offset, anchor_offset + len(anchor))
        if index in spoken_rank
    ]
    if not anchor_ranks:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: AI 锚点没有可定位文字")

    def timing_at(position: int, edge: str) -> float:
        projected = (position / rank) * len(timeline)
        timeline_index = int(projected + 0.5)
        timeline_index = min(max(timeline_index, 0), len(timeline) - 1)
        return float(timeline[timeline_index][edge])

    start = max(0.0, timing_at(anchor_ranks[0], "start"))
    end = max(start + 0.5, timing_at(anchor_ranks[-1] + 1, "end"))
    return start, end
