"""Fail-closed preparation of isolated v2 digital-human review candidates."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

from .fingerprint import full_sha256
from .libraries import register_library
from .packaging_plan import canonical_packaging_gate_payload
from .provenance import canonical_digest, record_gate, selection_digest
from .scanner import scan_library
from .subtitles import SubtitleSemanticsError, validate_semantics_file


REQUIRED_KEYS = (
    "xinjiang-wind-farm",
    "modern-data-center",
    "xinjiang-computing-park",
)
SUBTITLE_PRESET = "sub-modern-clean-v1"


class V2CandidateError(RuntimeError):
    """Stable blocker for an unsafe v2 candidate preparation request."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _blocked(code: str, message: str) -> None:
    raise V2CandidateError(code, message)


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_generated_file(value: str, library_root: Path, *, resolver=None) -> Path:
    """Resolve a manifest filename beneath the generated library, including symlinks."""
    resolve_path = Path.resolve if resolver is None else resolver
    root = resolve_path(Path(library_root))
    candidate = Path(value)
    resolved = resolve_path(root / candidate) if not candidate.is_absolute() else resolve_path(candidate)
    if not _contained(resolved, root):
        _blocked("GENERATED_FILE_OUTSIDE_LIBRARY", f"生成文件越出素材库：{resolved}")
    return resolved


_CARD_KEY_MARKERS = {
    "xinjiang-wind-farm": ("风电", "风机", "绿电"),
    "modern-data-center": ("数据中心", "机房", "服务器"),
    "xinjiang-computing-park": ("信息产业园", "产业园", "园区", "昌吉"),
}


def _source_card_key(card: dict) -> str:
    text = " ".join(
        str(card.get(field) or "")
        for field in ("narrative_text", "system_suggestion", "final_decision")
    )
    matches = [
        key for key, markers in _CARD_KEY_MARKERS.items()
        if any(marker in text for marker in markers)
    ]
    if not matches:
        _blocked(
            "SOURCE_SCENE_CARD_UNCLASSIFIED",
            f"场景卡无法唯一识别 B-roll 语义：{card['card_id']}",
        )
    if len(matches) != 1:
        _blocked(
            "SOURCE_SCENE_CARD_AMBIGUOUS",
            f"场景卡命中多个 B-roll 语义：{card['card_id']}",
        )
    return matches[0]


def _map_source_cards(source_cards: list[dict]) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for card in source_cards:
        key = _source_card_key(card)
        if key in mapping:
            _blocked("SOURCE_SCENE_CARD_MAPPING_INVALID", f"重复 B-roll 叙事窗口：{key}")
        mapping[key] = card
    if set(mapping) != set(REQUIRED_KEYS):
        _blocked("SOURCE_SCENE_CARD_MAPPING_INVALID", "源任务缺少指定 B-roll 叙事窗口")
    return mapping

def _source_state(db, task_id: str) -> dict:
    queries = {
        "task": "SELECT * FROM tasks WHERE task_id=?",
        "assets": "SELECT * FROM task_assets WHERE task_id=? ORDER BY asset_id",
        "cards": "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id",
        "locks": "SELECT * FROM picture_lock WHERE task_id=?",
        "plans": "SELECT * FROM packaging_plans WHERE task_id=? ORDER BY plan_id",
        "plan_items": (
            "SELECT i.* FROM packaging_plan_items i JOIN packaging_plans p USING(plan_id) "
            "WHERE p.task_id=? ORDER BY i.plan_id,i.item_id"
        ),
        "gates": "SELECT * FROM task_gate_events WHERE task_id=? ORDER BY rowid",
        "snapshots": "SELECT * FROM render_snapshots WHERE task_id=? ORDER BY rowid",
    }
    return {
        name: [tuple(row) for row in db.connection.execute(query, (task_id,)).fetchall()]
        for name, query in queries.items()
    }


def _load_generated_manifest(manifest_path: Path, library_root: Path) -> tuple[str, list[dict]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _blocked("GENERATED_MANIFEST_INVALID", f"manifest 无法读取：{error}")
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        _blocked("GENERATED_MANIFEST_SCHEMA", "manifest schemaVersion 必须为 1")
    run_id = payload.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        _blocked("GENERATED_RUN_ID_MISSING", "manifest 缺少非空 runId")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != len(REQUIRED_KEYS):
        _blocked("MANIFEST_ITEM_COUNT", "manifest 必须恰好包含三条生成素材")

    observed_keys = [item.get("key") if isinstance(item, dict) else None for item in items]
    if set(observed_keys) != set(REQUIRED_KEYS) or len(set(observed_keys)) != len(REQUIRED_KEYS):
        _blocked("MANIFEST_KEYS_INVALID", "manifest 必须且只能包含三条指定的唯一 key")

    validated: list[dict] = []
    hashes: set[str] = set()
    for item in items:
        for field in ("key", "prompt", "model", "taskId", "file", "createdAt", "sha256"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                _blocked("MANIFEST_PROVENANCE_MISSING", f"生成素材缺少 {field}")
        expected_hash = item["sha256"].lower()
        if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
            _blocked("MANIFEST_PROVENANCE_MISSING", "生成素材 sha256 必须是 64 位十六进制")
        file_path = _resolve_generated_file(item["file"], library_root)

        if not file_path.is_file():
            _blocked("GENERATED_FILE_MISSING", f"生成文件不存在：{file_path}")
        actual_hash = full_sha256(file_path)
        if actual_hash != expected_hash:
            _blocked("GENERATED_HASH_MISMATCH", f"生成文件哈希不匹配：{file_path}")
        if actual_hash in hashes:
            _blocked("GENERATED_HASH_DUPLICATE", "三条生成素材不得复用同一哈希")
        hashes.add(actual_hash)
        validated.append({
            "key": item["key"],
            "prompt": item["prompt"],
            "model": item["model"],
            "task_id": item["taskId"],
            "file": file_path,
            "created_at": item["createdAt"],
            "sha256": actual_hash,
        })
    return run_id.strip(), sorted(validated, key=lambda item: REQUIRED_KEYS.index(item["key"]))


def _source_broll_hashes(db, source_task_id: str) -> tuple[set[str], set[str]]:
    rows = db.connection.execute(
        """
        SELECT ta.asset_id, a.full_sha256, l.mount_path, al.relative_path
        FROM task_assets ta
        JOIN assets a USING(asset_id)
        LEFT JOIN asset_locations al ON al.asset_id=ta.asset_id AND al.state='available'
        LEFT JOIN libraries l ON l.library_id=al.library_id AND l.online=1
        WHERE ta.task_id=? AND ta.role='broll'
        ORDER BY ta.asset_id, al.location_id
        """,
        (source_task_id,),
    ).fetchall()
    asset_ids = {row["asset_id"] for row in rows}
    hashes = {row["full_sha256"].lower() for row in rows if row["full_sha256"]}
    for row in rows:
        if row["mount_path"] is None or row["relative_path"] is None:
            continue
        root = Path(row["mount_path"]).resolve()
        relative = Path(row["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            _blocked("SOURCE_BROLL_PATH_INVALID", f"源 B-roll 路径不安全：{row['asset_id']}")
        path = (root / relative).resolve()
        if not _contained(path, root) or not path.is_file():
            _blocked("SOURCE_BROLL_FILE_MISSING", f"源 B-roll 文件不可用：{row['asset_id']}")
        hashes.add(full_sha256(path))
    if len(asset_ids) != len(REQUIRED_KEYS):
        _blocked("SOURCE_BROLL_COUNT_INVALID", "源任务必须恰好绑定三条旧 B-roll")
    return hashes, asset_ids


def _scan_generated_assets(db, run_id: str, library_root: Path, generated: list[dict]) -> dict[str, str]:
    library_id = f"generated-v2-{run_id}"
    register_library(db, library_id, f"Seedance v2 {run_id}", "local", str(library_root))
    scan_library(db, library_id)
    result: dict[str, str] = {}
    with db.transaction() as conn:
        for item in generated:
            relative = str(item["file"].relative_to(library_root))
            row = conn.execute(
                """
                SELECT al.asset_id
                FROM asset_locations al
                JOIN assets a USING(asset_id)
                WHERE al.library_id=? AND al.relative_path=? AND al.state='available'
                """,
                (library_id, relative),
            ).fetchone()
            if row is None:
                _blocked("GENERATED_ASSET_UNREGISTERED", f"生成素材未登记：{relative}")
            conn.execute(
                "UPDATE assets SET full_sha256=? WHERE asset_id=?",
                (item["sha256"], row["asset_id"]),
            )
            result[item["key"]] = row["asset_id"]
    if len(set(result.values())) != len(REQUIRED_KEYS):
        _blocked("GENERATED_ASSET_DUPLICATE", "三条生成素材必须登记为独立资产")
    return result


def _confirmed_source_packaging(db, source_task_id: str) -> tuple[dict, list[dict]]:
    plans = db.connection.execute(
        "SELECT * FROM packaging_plans WHERE task_id=? AND status='confirmed' ORDER BY rowid",
        (source_task_id,),
    ).fetchall()
    if len(plans) != 1:
        _blocked("SOURCE_PACKAGING_INVALID", "源任务必须有且仅有一个已确认包装方案")
    plan = dict(plans[0])
    items = [
        dict(row) for row in db.connection.execute(
            "SELECT * FROM packaging_plan_items WHERE plan_id=? ORDER BY item_id",
            (plan["plan_id"],),
        ).fetchall()
    ]
    return plan, items


def _confirm_target_packaging(conn, task_id: str, plan_id: str) -> None:
    digest, state = selection_digest(_ConnectionDatabase(conn), task_id)
    lock = conn.execute(
        "SELECT changed_since_lock,selection_digest FROM picture_lock WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if lock is None or lock["changed_since_lock"] or lock["selection_digest"] != digest:
        _blocked("PICTURE_LOCK_INVALID", "目标画面锁定无效")
    items = [
        dict(row) for row in conn.execute(
            "SELECT slot,asset_ref,asset_file,asset_version,is_backup,rank "
            "FROM packaging_plan_items WHERE plan_id=? ORDER BY slot,rank,item_id",
            (plan_id,),
        ).fetchall()
    ]
    subtitles = [item for item in items if item["slot"] == "subtitle"]
    if subtitles != [{
        "slot": "subtitle", "asset_ref": SUBTITLE_PRESET, "asset_file": None,
        "asset_version": None, "is_backup": 0, "rank": 0,
    }]:
        _blocked("TARGET_SUBTITLE_INVALID", "目标包装必须唯一确认现代字幕 preset")
    plan = conn.execute(
        "SELECT plan_id,route,plan_name FROM packaging_plans WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    if plan is None:
        _blocked("TARGET_PACKAGING_INVALID", "目标包装方案不存在")
    payload = canonical_packaging_gate_payload(dict(plan), items)
    conn.execute(
        "UPDATE packaging_plans SET status='confirmed',confirmed_at=datetime('now') WHERE plan_id=?",
        (plan_id,),
    )
    conn.execute(
        "UPDATE tasks SET status='packaging_confirmed',updated_at=datetime('now') WHERE task_id=?",
        (task_id,),
    )
    record_gate(conn, task_id, "packaging", canonical_digest(payload), payload)


class _ConnectionDatabase:
    """Minimal read-only adapter so selection_digest sees uncommitted transaction state."""

    def __init__(self, connection):
        self.connection = connection


def prepare_v2_candidate(
    db, source_task_id: str, target_task_id: str,
    generated_manifest: Path, generated_library: Path, *,
    script_path: Path, words_path: Path, semantics_path: Path,
) -> dict:
    """Create a fail-closed v2 task without mutating the source task."""
    library_root = Path(generated_library).resolve()
    if not library_root.is_dir():
        _blocked("GENERATED_LIBRARY_MISSING", f"生成素材库不存在：{library_root}")
    script_path = Path(script_path).resolve()
    words_path = Path(words_path).resolve()
    if not script_path.is_file() or not words_path.is_file():
        _blocked("SUBTITLE_SOURCES_MISSING", "v2 任务必须绑定真实 script_path 与 words_path")
    try:
        semantics_path = validate_semantics_file(semantics_path)
    except SubtitleSemanticsError as error:
        _blocked("SUBTITLE_SEMANTICS_INVALID", str(error))
    if source_task_id == target_task_id:
        _blocked("TARGET_TASK_INVALID", "源任务与目标任务不能相同")
    source_task = db.connection.execute(
        "SELECT * FROM tasks WHERE task_id=?", (source_task_id,)
    ).fetchone()
    if source_task is None:
        _blocked("SOURCE_TASK_MISSING", f"源任务不存在：{source_task_id}")
    if db.connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (target_task_id,)).fetchone():
        _blocked("TARGET_TASK_EXISTS", f"目标任务已存在：{target_task_id}")

    source_before = _source_state(db, source_task_id)
    run_id, generated = _load_generated_manifest(Path(generated_manifest), library_root)
    old_hashes, source_old_broll_ids = _source_broll_hashes(db, source_task_id)
    if any(item["sha256"] in old_hashes for item in generated):
        _blocked("OLD_BROLL_HASH_REUSED", "新 B-roll 哈希不得复用源任务旧素材")
    source_assets = [
        dict(row) for row in db.connection.execute(
            "SELECT asset_id,role FROM task_assets WHERE task_id=? ORDER BY asset_id",
            (source_task_id,),
        ).fetchall()
    ]
    digital_assets = [item["asset_id"] for item in source_assets if item["role"] == "digital_human"]
    if len(digital_assets) != 1:
        _blocked("SOURCE_DIGITAL_HUMAN_INVALID", "源任务必须恰好绑定一条数字人口播")
    source_cards = [
        dict(row) for row in db.connection.execute(
            "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id",
            (source_task_id,),
        ).fetchall()
    ]
    if len(source_cards) != len(REQUIRED_KEYS) or any(
        card["asset_id"] not in source_old_broll_ids for card in source_cards
    ):
        _blocked("SOURCE_SCENE_CARDS_INVALID", "源任务必须有三段绑定旧 B-roll 的叙事窗口")
    source_card_mapping = _map_source_cards(source_cards)
    source_plan, source_plan_items = _confirmed_source_packaging(db, source_task_id)

    generated_asset_ids = _scan_generated_assets(db, run_id, library_root, generated)
    if set(generated_asset_ids.values()) & source_old_broll_ids:
        _blocked("OLD_BROLL_ASSET_REUSED", "新任务不得复用旧 B-roll asset_id")

    try:
        source_brief = json.loads(source_task["brief_json"] or "{}")
    except json.JSONDecodeError:
        _blocked("SOURCE_BRIEF_INVALID", "源任务 brief 无法读取")
    if not isinstance(source_brief, dict):
        _blocked("SOURCE_BRIEF_INVALID", "源任务 brief 必须是 object")
    target_brief = dict(source_brief)
    target_brief.update({
        "source_task_id": source_task_id,
        "generated_run_id": run_id,
        "subtitle_sources": {
            "script_path": str(script_path),
            "words_path": str(words_path),
            "semantics_path": str(semantics_path),
        },
    })
    target_plan_id = f"pp-v2-{uuid.uuid4().hex[:12]}"

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO tasks(task_id,title,task_type,status,brief_json,rule_version)
            VALUES(?,?,?,?,?,?)
            """,
            (
                target_task_id, f"{source_task['title']} v2", source_task["task_type"], "brief_locked",
                json.dumps(target_brief, ensure_ascii=False), source_task["rule_version"],
            ),
        )
        record_gate(conn, target_task_id, "brief", canonical_digest(target_brief), target_brief)
        conn.execute(
            "INSERT INTO task_assets(task_id,asset_id,role) VALUES(?,?, 'digital_human')",
            (target_task_id, digital_assets[0]),
        )
        for item in generated:
            conn.execute(
                "INSERT INTO task_assets(task_id,asset_id,role) VALUES(?,?, 'broll')",
                (target_task_id, generated_asset_ids[item["key"]]),
            )
        generated_by_key = {item["key"]: item for item in generated}
        for key in REQUIRED_KEYS:
            card = source_card_mapping[key]
            item = generated_by_key[key]
            conn.execute(
                """
                INSERT INTO scene_cards(
                  card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                  asset_id,segment_id,mix_form,enter_tc,exit_tc,audio_policy_draft,
                  source_type,system_suggestion,final_decision,confirm_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"{target_task_id}#{card['idx']}", target_task_id, card["idx"],
                    card["narrative_text"], card["narrative_start"], card["narrative_end"],
                    generated_asset_ids[item["key"]], None, card["mix_form"],
                    card["enter_tc"], card["exit_tc"], card["audio_policy_draft"],
                    card["source_type"], card["system_suggestion"], card["final_decision"], 1,
                ),
            )
        digest, state = selection_digest(_ConnectionDatabase(conn), target_task_id)
        conn.execute(
            """
            INSERT INTO picture_lock(task_id,lock_version,changed_since_lock,selection_digest)
            VALUES(?, 'picture-lock-v1', 0, ?)
            """,
            (target_task_id, digest),
        )
        record_gate(conn, target_task_id, "picture_lock", digest, state)
        conn.execute(
            "UPDATE tasks SET status='selection_locked',updated_at=datetime('now') WHERE task_id=?",
            (target_task_id,),
        )
        conn.execute(
            "INSERT INTO packaging_plans(plan_id,task_id,route,plan_name,status) VALUES(?,?,?,?, 'draft')",
            (target_plan_id, target_task_id, source_plan["route"], f"{source_plan['plan_name']} · v2"),
        )
        for item in source_plan_items:
            if item["slot"] == "subtitle":
                continue
            conn.execute(
                """
                INSERT INTO packaging_plan_items(
                  plan_id,slot,asset_ref,asset_file,asset_version,is_backup,rank,notes
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    target_plan_id, item["slot"], item["asset_ref"], item["asset_file"],
                    item["asset_version"], item["is_backup"], item["rank"], item["notes"],
                ),
            )
        conn.execute(
            """
            INSERT INTO packaging_plan_items(plan_id,slot,asset_ref,is_backup,rank)
            VALUES(?, 'subtitle', ?, 0, 0)
            """,
            (target_plan_id, SUBTITLE_PRESET),
        )
        _confirm_target_packaging(conn, target_task_id, target_plan_id)
        if _source_state(db, source_task_id) != source_before:
            _blocked("SOURCE_TASK_MUTATED", "源任务在 v2 准备过程中被修改")

    target = db.connection.execute(
        "SELECT status FROM tasks WHERE task_id=?", (target_task_id,)
    ).fetchone()
    assets = [
        {
            "key": item["key"],
            "asset_id": generated_asset_ids[item["key"]],
            "sha256": item["sha256"],
            "task_id": item["task_id"],
            "model": item["model"],
            "prompt": item["prompt"],
            "created_at": item["created_at"],
        }
        for item in generated
    ]
    return {
        "source_task_id": source_task_id,
        "target_task_id": target_task_id,
        "run_id": run_id,
        "assets": assets,
        "subtitle_preset": SUBTITLE_PRESET,
        "status": target["status"],
        "brief_locked": True,
        "picture_locked": True,
        "packaging_plan_id": target_plan_id,
    }
