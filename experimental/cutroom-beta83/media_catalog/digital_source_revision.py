"""Early digital source replacement with preflight, history and fresh approval."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid

from .digital_workflow import (
    _default_file_snapshot, _register_source_plan_connection,
    _source_catalog_plan, _source_snapshot_identity, preflight_digital_task,
)
from .provenance import canonical_digest
from .task_script import ScriptDraftError, read_task_script
from .tasks import get_task
from .workflow import rollback_to_stage_connection


def _current_task(db, project_root: Path, task_id: str) -> tuple[dict, Path]:
    task = get_task(db, task_id)
    if task is None:
        raise ScriptDraftError(404, "NOT_FOUND", "任务不存在。")
    if task.get("task_type") != "digital_human":
        raise ScriptDraftError(409, "ROUTE_INVALID", "仅数字人口播任务可更换底片。")
    root = (Path(project_root) / "runtime" / "workbench" / "tasks").resolve()
    task_root = (root / task_id).resolve()
    if task_root == root or not task_root.is_relative_to(root) or not task_root.is_dir():
        raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务目录不可用。")
    if not read_task_script(project_root, task):
        raise ScriptDraftError(409, "SCRIPT_UNAVAILABLE", "当前文案不可用。")
    return task, task_root


def _replacement_plan(db, snapshot: dict, old_asset_id: str) -> dict:
    try:
        plan = _source_catalog_plan(db, snapshot)
    except RuntimeError as error:
        same_old_location = db.connection.execute(
            """SELECT 1 FROM asset_locations al JOIN libraries l USING(library_id)
            WHERE al.asset_id=? AND al.relative_path=? AND l.mount_path=? LIMIT 1""",
            (old_asset_id, Path(snapshot["path"]).name, str(Path(snapshot["path"]).parent)),
        ).fetchone()
        if str(error) != "已登记数字人来源与当前文件身份不一致" or same_old_location is None:
            raise
    else:
        if plan["asset_id"] != old_asset_id:
            return plan
    # An in-place re-export may keep the catalog's quick fingerprint while its
    # file version changes. Preserve the old asset row instead of mutating it.
    seed = canonical_digest({
        "path": snapshot["path"], "size": snapshot["size_bytes"],
        "mtime_ns": snapshot["mtime_ns"],
        "quick_fingerprint": snapshot["quick_fingerprint"],
        "file_identity": snapshot.get("file_identity", ""),
    })
    library_id = "lib_digital_rev_" + seed[:24]
    location = db.connection.execute(
        """SELECT al.location_id,al.asset_id,a.size_bytes,a.quick_fingerprint,al.mtime_ns
        FROM asset_locations al JOIN assets a USING(asset_id)
        WHERE al.library_id=? AND al.relative_path=?""",
        (library_id, Path(snapshot["path"]).name),
    ).fetchone()
    if location is not None:
        if (location["size_bytes"] != snapshot["size_bytes"]
                or location["quick_fingerprint"] != snapshot["quick_fingerprint"]
                or location["mtime_ns"] != snapshot["mtime_ns"]):
            raise ScriptDraftError(409, "SOURCE_IDENTITY_CONFLICT", "新底片台账身份冲突。")
        asset_id = location["asset_id"]
    else:
        asset_id = "ast_" + uuid.uuid4().hex
    library = db.connection.execute(
        "SELECT mount_path,purpose FROM libraries WHERE library_id=?", (library_id,)
    ).fetchone()
    if library is not None and (
        Path(library["mount_path"]).resolve() != Path(snapshot["path"]).resolve().parent
        or library["purpose"] != "task_specific"
    ):
        raise ScriptDraftError(409, "SOURCE_IDENTITY_CONFLICT", "新底片素材库身份冲突。")
    return {
        "library_id": library_id, "asset_id": asset_id,
        "mount_path": str(Path(snapshot["path"]).parent),
        "relative_path": Path(snapshot["path"]).name,
        "library_created": library is None, "asset_created": location is None,
        "location_id": None if location is None else location["location_id"],
        "location_created": location is None,
    }


def preview_source_replacement(
    db, project_root: Path, task_id: str, source_path: str,
) -> dict:
    task, task_root = _current_task(db, project_root, task_id)
    if not isinstance(source_path, str) or not source_path.strip():
        raise ScriptDraftError(400, "SOURCE_REQUIRED", "请先选择新的数字人口播 MP4。")
    brief = task.get("brief") or {}
    from .task_sources import get_task_sources
    current_sources = get_task_sources(db, task_id)
    materials = ({
        "primary_library_id": current_sources["primary"]["library_id"],
        "selected_generic_ids": [item["library_id"] for item in current_sources["selected_generic"]],
        "include_default_generic": current_sources["include_default_generic"],
    } if current_sources["primary"] else brief.get("materials") or {})
    request = {
        "route": "digital_human", "subtype": brief.get("subtype"),
        "title": task.get("title"),
        "final_script": read_task_script(project_root, task),
        "digital_human_mp4": source_path,
        "primary_library_id": materials.get("primary_library_id"),
        "selected_generic_ids": materials.get("selected_generic_ids", []),
        "include_default_generic": materials.get("include_default_generic", False),
        "orchestration_mode": task.get("orchestration_mode") or "standalone",
    }
    preflight = preflight_digital_task(db, request, project_root)
    snapshot = preflight.get("source")
    old = brief.get("digital_human_source") or {}
    try:
        old_identity = _source_snapshot_identity(old)
    except (KeyError, TypeError, ValueError) as error:
        raise ScriptDraftError(409, "SOURCE_BINDING_INVALID", "当前主底片身份记录不完整。") from error
    if snapshot and _source_snapshot_identity(snapshot) == old_identity:
        raise ScriptDraftError(409, "SOURCE_UNCHANGED", "所选底片与当前版本相同。")
    artifacts = task_root / "artifacts"
    affected = sorted(path.name for path in artifacts.iterdir()) if artifacts.is_dir() else []
    active = db.connection.execute(
        "SELECT job_id,status FROM jobs WHERE task_id=? AND status IN ('queued','running','pending') ORDER BY rowid",
        (task_id,),
    ).fetchall()
    blockers = list(preflight.get("blocking") or [])
    if active:
        blockers.append({"code": "TASK_BUSY", "status": "blocking", "message": "任务正在处理"})
    for table in ("scene_cards", "candidate_versions", "render_snapshots"):
        if db.connection.execute(f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
            blockers.append({"code": "REWORK_REQUIRED", "status": "blocking", "message": "已有分镜或候选，须走正式返工"})
            break
    digest = canonical_digest({
        "task_id": task_id, "brief": brief,
        "replacement": None if snapshot is None else _source_snapshot_identity(snapshot),
        "affected": affected,
    })
    return {
        "previewDigest": digest,
        "ready": bool(snapshot) and not blockers,
        "blocking": blockers,
        "warnings": preflight.get("warnings") or [],
        "source": None if snapshot is None else {
            "name": Path(str(snapshot["path"])).name,
            "sizeBytes": snapshot["size_bytes"],
            "durationSeconds": (snapshot.get("media") or {}).get("duration_seconds"),
        },
        "affectedArtifacts": affected,
        "runningJobs": [dict(row) for row in active],
    }


def replace_digital_source(
    db, project_root: Path, task_id: str, source_path: str,
    expected_preview_digest: str,
) -> dict:
    preview = preview_source_replacement(db, project_root, task_id, source_path)
    if not preview["ready"]:
        raise ScriptDraftError(409, "SOURCE_PREFLIGHT_BLOCKED", "新底片未通过预检或任务已有下游产物。")
    if preview["previewDigest"] != expected_preview_digest:
        raise ScriptDraftError(409, "SOURCE_PREVIEW_STALE", "底片预检依据已变化，请重新预览。")
    connection = db.connection
    if connection.in_transaction:
        raise RuntimeError("Source replacement requires its own transaction")
    connection.execute("BEGIN IMMEDIATE")
    task_root = None
    archive = None
    artifacts = None
    moved = created_empty = False
    try:
        task, task_root = _current_task(db, project_root, task_id)
        brief = dict(task["brief"])
        old_source = brief["digital_human_source"]
        snapshot = _default_file_snapshot(Path(source_path))
        if canonical_digest({
            "task_id": task_id, "brief": brief,
            "replacement": _source_snapshot_identity(snapshot),
            "affected": sorted(path.name for path in (task_root / "artifacts").iterdir())
            if (task_root / "artifacts").is_dir() else [],
        }) != expected_preview_digest:
            raise ScriptDraftError(409, "SOURCE_PREVIEW_STALE", "底片或任务内容已变化，请重新预览。")
        if connection.execute(
            "SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running','pending') LIMIT 1", (task_id,)
        ).fetchone():
            raise ScriptDraftError(409, "TASK_BUSY", "任务正在处理，请等待作业结束。")
        for table in ("scene_cards", "candidate_versions", "render_snapshots"):
            if connection.execute(f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
                raise ScriptDraftError(409, "REWORK_REQUIRED", "已有分镜或候选，须走正式返工。")
        old_assets = connection.execute(
            "SELECT asset_id FROM task_assets WHERE task_id=? AND role='digital_human'", (task_id,)
        ).fetchall()
        if len(old_assets) != 1 or old_assets[0]["asset_id"] != old_source.get("asset_id"):
            raise ScriptDraftError(409, "SOURCE_BINDING_INVALID", "当前主底片台账绑定不一致。")
        plan = _replacement_plan(db, snapshot, old_assets[0]["asset_id"])
        existing_use = connection.execute(
            "SELECT role FROM task_assets WHERE task_id=? AND asset_id=?", (task_id, plan["asset_id"])
        ).fetchone()
        if existing_use is not None:
            raise ScriptDraftError(409, "SOURCE_ASSET_ALREADY_USED", "新底片已作为任务其他素材使用。")
        artifacts = task_root / "artifacts"
        if artifacts.exists() and (not artifacts.is_dir() or artifacts.is_symlink()):
            raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务产物目录不可用。")
        history_root = task_root / "history" / "source-revisions"
        if not history_root.resolve().is_relative_to(task_root):
            raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务历史目录不可用。")
        archive = history_root / uuid.uuid4().hex
        history_root.mkdir(parents=True, exist_ok=True)
        archive.mkdir()
        source_record = {
            "read_only": True, "path": snapshot["path"],
            "size_bytes": snapshot["size_bytes"], "mtime_ns": snapshot["mtime_ns"],
            "quick_fingerprint": snapshot["quick_fingerprint"],
            "file_identity": snapshot.get("file_identity", ""),
            "asset_id": plan["asset_id"], "library_id": plan["library_id"],
        }
        (archive / "revision.json").write_text(json.dumps({
            "previous_source": old_source, "new_source": source_record,
            "previous_brief_digest": canonical_digest(brief),
        }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        if artifacts.is_dir():
            os.replace(artifacts, archive / "artifacts")
            moved = True
        artifacts.mkdir()
        created_empty = not moved
        _register_source_plan_connection(connection, plan, snapshot)
        connection.execute(
            "DELETE FROM task_assets WHERE task_id=? AND asset_id=?", (task_id, old_assets[0]["asset_id"])
        )
        connection.execute(
            "INSERT INTO task_assets(task_id,asset_id,role) VALUES(?,?,'digital_human')",
            (task_id, plan["asset_id"]),
        )
        brief["digital_human_source"] = source_record
        connection.execute(
            "UPDATE tasks SET brief_json=?,status='draft',updated_at=datetime('now') WHERE task_id=?",
            (json.dumps(brief, ensure_ascii=False), task_id),
        )
        rollback_to_stage_connection(
            connection, task_id, "brief_lock", reason="source_revision:transcript_review"
        )
        result = {"task": get_task(db, task_id), "archive": str(archive)}
        connection.commit()
        return result
    except Exception:
        try:
            if moved:
                if artifacts.exists():
                    shutil.rmtree(artifacts)
                os.replace(archive / "artifacts", artifacts)
            elif created_empty and artifacts is not None and artifacts.exists():
                shutil.rmtree(artifacts)
            if archive is not None and archive.exists():
                shutil.rmtree(archive)
        finally:
            connection.rollback()
        raise
