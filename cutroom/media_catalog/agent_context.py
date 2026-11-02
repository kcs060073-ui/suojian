"""Credential-free, source-bounded planning context for local Agents."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from .task_sources import list_eligible_assets
from .tasks import get_task
from .workflow import project_workflow


_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_MATERIALS = 500


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _safe_relative_path(value: object, *, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{error_code}: 相对路径缺失")
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.parts in {(), (".",)}
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise RuntimeError(f"{error_code}: 路径不在允许的任务范围内")
    return path.as_posix()


def _locked_script(task: dict, project_root: Path) -> dict:
    task_id = str(task["task_id"])
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise RuntimeError("AGENT_SCRIPT_PATH_INVALID: 任务标识不能映射到安全目录")
    brief = task.get("brief")
    script_record = brief.get("final_script") if isinstance(brief, dict) else None
    if not isinstance(script_record, dict):
        raise RuntimeError("AGENT_SCRIPT_REQUIRED: 任务没有锁定文案记录")
    relative_path = _safe_relative_path(
        script_record.get("relative_path"),
        error_code="AGENT_SCRIPT_PATH_INVALID",
    )
    task_root = (
        project_root
        / "runtime"
        / "workbench"
        / "tasks"
        / task_id
    ).resolve()
    script_path = (task_root / Path(relative_path)).resolve(strict=False)
    try:
        script_path.relative_to(task_root)
    except ValueError as error:
        raise RuntimeError(
            "AGENT_SCRIPT_PATH_INVALID: 文案路径逃逸任务目录"
        ) from error
    if not script_path.is_file():
        raise RuntimeError("AGENT_SCRIPT_REQUIRED: 锁定文案文件不存在")
    try:
        script_bytes = script_path.read_bytes()
        text = script_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("AGENT_SCRIPT_INVALID: 锁定文案无法读取") from error
    digest = hashlib.sha256(script_bytes).hexdigest()
    expected_digest = script_record.get("sha256")
    if not isinstance(expected_digest, str) or expected_digest != digest:
        raise RuntimeError("AGENT_SCRIPT_CHANGED: 锁定文案与任务摘要不一致")
    return {
        "text": text,
        "sha256": digest,
        "relativePath": relative_path,
    }


def _active_source_snapshot_ids(db, task_id: str) -> list[str]:
    rows = db.connection.execute(
        """
        SELECT s.scope_id, ss.snapshot_id
        FROM task_source_scopes AS s
        JOIN task_source_snapshots AS ss ON ss.scope_id=s.scope_id
        WHERE s.task_id=? AND s.active=1
          AND ss.rowid=(
            SELECT newest.rowid
            FROM task_source_snapshots AS newest
            WHERE newest.scope_id=s.scope_id
            ORDER BY newest.created_at DESC, newest.rowid DESC
            LIMIT 1
          )
        ORDER BY s.scope_id, ss.snapshot_id
        """,
        (task_id,),
    ).fetchall()
    return sorted(str(row["snapshot_id"]) for row in rows)


def _materials(db, task_id: str) -> tuple[list[dict], list[dict]]:
    public_items = []
    digest_items = []
    assets = sorted(
        list_eligible_assets(db, task_id),
        key=lambda item: (
            str(item["asset_id"]),
            str(item["media_kind"]),
            str(item["relative_path"]),
        ),
    )[:_MAX_MATERIALS]
    for item in assets:
        relative_path = _safe_relative_path(
            item["relative_path"],
            error_code="AGENT_MATERIAL_PATH_INVALID",
        )
        public_items.append(
            {
                "assetId": item["asset_id"],
                "mediaKind": item["media_kind"],
                "relativePath": relative_path,
                "sizeBytes": item["size_bytes"],
                "sourceMode": item["source_mode"],
                "taskRole": item.get("task_role"),
            }
        )
        digest_items.append(
            {
                "asset_id": item["asset_id"],
                "media_kind": item["media_kind"],
                "relative_path": relative_path,
            }
        )
    return public_items, digest_items


def _planning_schema(route: str) -> dict:
    if route == "digital_human":
        artifact_type = "digital_storyboard"
        required = ["brief", "plan"]
    elif route == "ad_mix":
        artifact_type = "ad_storyboard"
        required = ["plan"]
    else:
        raise RuntimeError("AGENT_ROUTE_UNSUPPORTED: 当前任务路线不支持 Agent 规划")
    return {
        "artifactType": artifact_type,
        "schemaVersion": 1,
        "contentSchema": {
            "type": "object",
            "required": required,
            "properties": {
                name: {"type": "object"}
                for name in required
            },
            "additionalProperties": False,
        },
    }


def _input_binding(
    task: dict,
    script_sha256: str,
    source_snapshot_ids: list[str],
    assets: list[dict],
) -> dict:
    return {
        "task_id": task["task_id"],
        "route": task["task_type"],
        "orchestration_mode": task["orchestration_mode"],
        "script_sha256": script_sha256,
        "source_snapshot_ids": sorted(source_snapshot_ids),
        "assets": sorted(
            assets,
            key=lambda item: (
                str(item["asset_id"]),
                str(item["media_kind"]),
                str(item["relative_path"]),
            ),
        ),
    }


def build_agent_context(db, task_id: str, project_root: Path | str) -> dict:
    """Return the deterministic minimum context needed for Agent planning."""
    task = get_task(db, task_id)
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    if task["orchestration_mode"] != "agent":
        raise RuntimeError("AGENT_MODE_REQUIRED: 任务不是 Agent 编排模式")

    workflow = project_workflow(db, task_id)
    if not workflow["gates"]["brief_locked"]:
        raise RuntimeError("AGENT_BRIEF_LOCK_REQUIRED: 请先在 CUTROOM 锁定文案")
    material_stage = next(
        (
            item
            for item in workflow["stages"]
            if item["stage_key"] == "material_sync"
        ),
        None,
    )
    if material_stage is None or material_stage["status"] != "completed":
        raise RuntimeError(
            "AGENT_MATERIAL_SYNC_REQUIRED: 请先完成任务素材同步"
        )

    script = _locked_script(task, Path(project_root).resolve())
    materials, digest_assets = _materials(db, task_id)
    source_snapshot_ids = _active_source_snapshot_ids(db, task_id)
    binding = _input_binding(
        task,
        script["sha256"],
        source_snapshot_ids,
        digest_assets,
    )
    input_digest = hashlib.sha256(
        _canonical_json(binding).encode("utf-8")
    ).hexdigest()
    return {
        "task": {
            "taskId": task["task_id"],
            "title": task["title"],
            "route": task["task_type"],
            "orchestrationMode": task["orchestration_mode"],
        },
        "script": script,
        "workflow": {
            "currentStage": workflow["current_stage"],
            "runStatus": workflow["run_status"],
            "nextAction": workflow["next_action"],
            "stages": [
                {
                    "stageKey": item["stage_key"],
                    "status": item["status"],
                }
                for item in workflow["stages"]
            ],
        },
        "gates": dict(workflow["gates"]),
        "materials": materials,
        "planningSchema": _planning_schema(str(task["task_type"])),
        "inputDigest": input_digest,
    }


def current_agent_input_digest(
    db,
    task_id: str,
    project_root: Path | str,
) -> str:
    return str(build_agent_context(db, task_id, project_root)["inputDigest"])
