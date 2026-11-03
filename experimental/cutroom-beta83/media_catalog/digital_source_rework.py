"""Candidate-bound digital source revision with actual-impact confirmation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from . import digital_workflow
from .digital_source_revision import _current_task, _replacement_plan
from .digital_workflow import (
    _default_file_snapshot, _register_source_plan_connection, _source_snapshot_identity,
)
from .provenance import canonical_digest
from .rework import approve_rework_cost, execute_rework, get_rework_request, preview_rework_impact
from .script_rework import (
    _current_downstream_approvals, _encode_token, _manifest, _verify_token,
    pre_candidate_rework_available,
)
from .task_script import ScriptDraftError, read_task_script
from .tasks import get_task


def _state(db, project_root: Path, task_id: str, rework_id: str | None, source_path: str) -> dict:
    task, task_root = _current_task(db, project_root, task_id)
    if not isinstance(source_path, str) or not source_path.strip():
        raise ScriptDraftError(400, "SOURCE_REQUIRED", "请先选择新的数字人口播 MP4。")
    brief = task.get("brief") or {}
    script = read_task_script(project_root, task)
    script_sha = (brief.get("final_script") or {}).get("sha256")
    if hashlib.sha256(script.encode("utf-8")).hexdigest() != script_sha:
        raise ScriptDraftError(409, "SCRIPT_CHANGED", "文案文件与保存版本不一致，请恢复后重试。")
    old = brief.get("digital_human_source") or {}
    try:
        old_identity = _source_snapshot_identity(old)
    except (KeyError, TypeError, ValueError) as error:
        raise ScriptDraftError(409, "SOURCE_BINDING_INVALID", "当前主底片身份记录不完整。") from error
    current = db.connection.execute(
        "SELECT candidate_id,version_no,status,qa_report_id,qa_snapshot_id FROM candidate_versions "
        "WHERE task_id=? ORDER BY version_no DESC LIMIT 1", (task_id,),
    ).fetchone()
    if rework_id is None:
        if not pre_candidate_rework_available(db, task_id):
            raise ScriptDraftError(409, "REWORK_ROUTE_INVALID", "当前阶段不能直接更换已确认底片。")
        row = {
            "rework_id": None, "source_candidate_id": None,
            "status": "pending_confirmation", "cost_status": "none", "execution_key": None,
        }
    else:
        row = db.connection.execute(
            "SELECT * FROM rework_requests WHERE task_id=? AND rework_id=?", (task_id, rework_id),
        ).fetchone()
        if row is None:
            raise ScriptDraftError(404, "REWORK_NOT_FOUND", "返工单不存在。")
        if row["category"] != "script" or row["target_stage"] != "brief_lock":
            raise ScriptDraftError(409, "REWORK_ROUTE_INVALID", "此返工单不能更换数字人口播底片。")
        if current is None or current["candidate_id"] != row["source_candidate_id"]:
            raise ScriptDraftError(409, "REWORK_SOURCE_STALE", "返工来源已不是当前候选。")
    from .task_sources import get_task_sources
    current_sources = get_task_sources(db, task_id)
    materials = ({
        "primary_library_id": current_sources["primary"]["library_id"],
        "selected_generic_ids": [item["library_id"] for item in current_sources["selected_generic"]],
        "include_default_generic": current_sources["include_default_generic"],
    } if current_sources["primary"] else brief.get("materials") or {})
    request = {
        "route": "digital_human", "subtype": brief.get("subtype"),
        "title": task.get("title"), "final_script": script,
        "digital_human_mp4": source_path,
        "primary_library_id": materials.get("primary_library_id"),
        "selected_generic_ids": materials.get("selected_generic_ids", []),
        "include_default_generic": materials.get("include_default_generic", False),
        "orchestration_mode": task.get("orchestration_mode") or "standalone",
    }
    preflight = digital_workflow.preflight_digital_task(db, request, project_root)
    snapshot = preflight.get("source")
    try:
        source_identity = None if snapshot is None else _source_snapshot_identity(snapshot)
    except (KeyError, TypeError, ValueError) as error:
        raise ScriptDraftError(409, "SOURCE_PREFLIGHT_INVALID", "新底片预检身份不完整。") from error
    cards = [dict(item) for item in db.connection.execute(
        "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id", (task_id,),
    )]
    segments = [dict(item) for item in db.connection.execute(
        "SELECT * FROM script_segments WHERE task_id=? AND script_revision NOT LIKE 'archived:%' ORDER BY script_revision,idx",
        (task_id,),
    )]
    requirements = [dict(item) for item in db.connection.execute(
        "SELECT * FROM reshoot_requirements WHERE task_id=? AND script_revision NOT LIKE 'archived:%' ORDER BY gap_id",
        (task_id,),
    )]
    feedback = [dict(item) for item in db.connection.execute(
        "SELECT * FROM storyboard_feedback WHERE task_id=? ORDER BY rowid", (task_id,),
    )]
    jobs = [dict(item) for item in db.connection.execute(
        "SELECT job_id,status FROM jobs WHERE task_id=? AND status IN ('queued','running','pending') ORDER BY rowid",
        (task_id,),
    )]
    packages = [dict(item) for item in db.connection.execute(
        "SELECT record_id,package_status FROM publish_records WHERE task_id=? AND candidate_id=? ORDER BY rowid",
        (task_id, current["candidate_id"] if current is not None else None),
    )]
    artifacts = task_root / "artifacts"
    if artifacts.is_symlink() or (artifacts.exists() and not artifacts.is_dir()):
        raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务产物目录不可用。")
    manifest = _manifest(artifacts)
    approval_state, approval_affected = _current_downstream_approvals(db, task_id)
    revision = canonical_digest({
        "task_id": task_id, "brief": brief, "script_sha": script_sha,
        "rework": (row["rework_id"], row["status"], row["cost_status"], row["execution_key"]),
        "candidate": dict(current) if current is not None else None, "cards": cards, "feedback": feedback, "script_segments": segments, "reshoot_requirements": requirements,
        "jobs": jobs, "packages": packages, "artifacts": manifest,
        "downstream_approvals": approval_state,
        "replacement": source_identity,
    })
    changed = source_identity is not None and source_identity != old_identity
    affected = []
    if changed:
        affected.extend({"kind": "file", "name": item["path"]} for item in manifest if item["type"] == "file")
        affected.extend({
            "kind": "scene_card", "id": card["card_id"],
            "name": f"镜头 {card['idx'] + 1} · {card['narrative_text'][:30]}",
        } for card in cards)
        affected.extend(approval_affected)
        if current is not None:
            affected.append({
                "kind": "candidate", "id": current["candidate_id"],
                "name": f"候选成片 · 第 {current['version_no']} 版",
            })
            if current["qa_report_id"]:
                affected.append({
                    "kind": "qa_report", "id": current["qa_report_id"],
                    "name": "当前候选的成片检查报告",
                })
        affected.extend({"kind": "publish_package", "id": item["record_id"],
                         "name": f"交付发布包 · {item['record_id'][:8]}"}
                        for item in packages if item["package_status"] == "current")
    return {
        "task": task, "task_root": task_root, "artifacts": artifacts,
        "rework": row, "candidate": current, "snapshot": snapshot,
        "old_identity": old_identity, "source_identity": source_identity,
        "cards": cards, "feedback": feedback, "script_segments": segments, "reshoot_requirements": requirements, "jobs": jobs,
        "preflight": preflight, "changed": changed, "affected": affected,
        "revision": revision,
    }


def preview_source_rework(db, project_root: Path, task_id: str, rework_id: str | None, source_path: str) -> dict:
    state = _state(db, project_root, task_id, rework_id, source_path)
    if state["rework"]["status"] not in {"draft", "pending_confirmation"}:
        raise ScriptDraftError(409, "REWORK_NOT_PENDING", "当前返工单已处理。")
    blockers = list(state["preflight"].get("blocking") or [])
    if state["jobs"]:
        blockers.append({"code": "TASK_BUSY", "message": "任务正在处理"})
    if not state["changed"] and state["source_identity"] is not None:
        blockers.append({"code": "SOURCE_UNCHANGED", "message": "所选底片与当前版本相同"})
    payload = {
        "kind": "digital-source-rework", "task_id": task_id, "rework_id": rework_id,
        "revision": state["revision"], "source_identity": list(state["source_identity"]) if state["source_identity"] else None,
        "issued_at": int(time.time()),
    }
    token = _encode_token(payload)
    return {
        "token": token, "base_revision": state["revision"],
        "changed_inputs": [] if not state["changed"] else [{
            "field": "digital_human_source", "before": canonical_digest(state["old_identity"]),
            "after": canonical_digest(state["source_identity"]),
        }],
        "actual_affected_artifacts": state["affected"],
        "running_jobs": state["jobs"], "blocking": blockers,
        "warnings": state["preflight"].get("warnings") or [],
        "source": None if state["snapshot"] is None else {
            "name": Path(str(state["snapshot"]["path"])).name,
            "sizeBytes": state["snapshot"]["size_bytes"],
            "durationSeconds": (state["snapshot"].get("media") or {}).get("duration_seconds"),
        },
        "changed": state["changed"], "ready": state["changed"] and not blockers,
        "cost_confirmation_required": state["rework"]["cost_status"] == "confirmation_required",
        "expires_at": payload["issued_at"] + 300,
    }


def _ensure_source_unchanged(source_path: str, expected_identity: tuple) -> None:
    try:
        current = _source_snapshot_identity(_default_file_snapshot(Path(source_path)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ScriptDraftError(409, "SOURCE_REWORK_STALE", "新底片已变化，请重新预检。") from error
    if current != expected_identity:
        raise ScriptDraftError(409, "SOURCE_REWORK_STALE", "新底片已变化，请重新预检。")


def commit_source_rework(
    db, project_root: Path, task_id: str, rework_id: str | None, source_path: str,
    token: str, *, expected_revision: str, request_id: str, confirm_cost: bool = False,
) -> dict:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ScriptDraftError(400, "REQUEST_ID_REQUIRED", "缺少本次提交编号。")
    request_digest = canonical_digest({
        "task_id": task_id, "rework_id": rework_id, "source_path": source_path,
        "token": token, "expected_revision": expected_revision, "confirm_cost": confirm_cost,
    })
    connection = db.connection
    if connection.in_transaction:
        raise RuntimeError("Source rework commit requires its own transaction")
    connection.execute("BEGIN IMMEDIATE")
    archive = artifacts = task_root = None
    moved_artifacts = created_artifacts = False
    try:
        if rework_id is None:
            existing = connection.execute(
                "SELECT status,impact_json FROM rework_requests WHERE task_id=? AND idempotency_key=?",
                (task_id, request_id.strip()),
            ).fetchone()
        else:
            existing = connection.execute(
                "SELECT status,impact_json FROM rework_requests WHERE task_id=? AND rework_id=?",
                (task_id, rework_id),
            ).fetchone()
        if existing is not None and existing["status"] == "applied":
            impact = json.loads(existing["impact_json"] or "{}")
            if impact.get("source_commit_request_id") != request_id.strip() or impact.get("source_commit_request_digest") != request_digest:
                raise ScriptDraftError(409, "REWORK_IDEMPOTENCY_CONFLICT", "提交编号已用于不同返工内容。")
            connection.commit()
            return {"receipt": impact["source_commit_receipt"]}
        if rework_id is None and existing is not None:
            raise ScriptDraftError(409, "REWORK_IDEMPOTENCY_CONFLICT", "提交编号已用于其他返工请求。")
        state = _state(db, project_root, task_id, rework_id, source_path)
        row = state["rework"]
        if row["status"] not in {"draft", "pending_confirmation"}:
            raise ScriptDraftError(409, "REWORK_NOT_PENDING", "当前返工单不可提交。")
        if not state["changed"]:
            raise ScriptDraftError(409, "SOURCE_UNCHANGED", "底片没有变化，无需返工。")
        if state["jobs"]:
            raise ScriptDraftError(409, "TASK_BUSY", "任务正在处理，请等待作业结束。")
        if state["preflight"].get("blocking") or state["snapshot"] is None:
            raise ScriptDraftError(409, "SOURCE_PREFLIGHT_BLOCKED", "新底片未通过预检。")
        if expected_revision != state["revision"]:
            raise ScriptDraftError(409, "SOURCE_REWORK_STALE", "返工影响已变化，请重新预览。")
        _verify_token(token, {
            "kind": "digital-source-rework", "task_id": task_id,
            "rework_id": rework_id, "revision": state["revision"],
            "source_identity": list(state["source_identity"]),
        })
        _ensure_source_unchanged(source_path, state["source_identity"])
        if row["cost_status"] == "confirmation_required" and not confirm_cost:
            raise ScriptDraftError(409, "REWORK_COST_CONFIRMATION_REQUIRED", "请确认后续外部调用费用。")
        effective_rework_id = rework_id
        if effective_rework_id is None:
            effective_rework_id = f"rework-{uuid.uuid4().hex[:16]}"
            impact = preview_rework_impact("script")
            impact["origin"] = "pre_candidate_source_revision"
            impact["cost_confirmation_required"] = False
            connection.execute(
                """INSERT INTO rework_requests(
                  rework_id,task_id,source_candidate_id,category,notes,target_stage,
                  impact_json,cost_status,status,idempotency_key
                ) VALUES(?,?,NULL,'script',?,'brief_lock',?,'none','pending_confirmation',?)""",
                (effective_rework_id, task_id, "用户更换已确认口播底片",
                 json.dumps(impact, ensure_ascii=False, sort_keys=True), request_id.strip()),
            )
        old_source = (state["task"].get("brief") or {})["digital_human_source"]
        old_assets = connection.execute(
            "SELECT asset_id FROM task_assets WHERE task_id=? AND role='digital_human'", (task_id,),
        ).fetchall()
        if len(old_assets) != 1 or old_assets[0]["asset_id"] != old_source.get("asset_id"):
            raise ScriptDraftError(409, "SOURCE_BINDING_INVALID", "当前主底片台账绑定不一致。")
        snapshot = state["snapshot"]
        plan = _replacement_plan(db, snapshot, old_assets[0]["asset_id"])
        if connection.execute(
            "SELECT 1 FROM task_assets WHERE task_id=? AND asset_id=?", (task_id, plan["asset_id"]),
        ).fetchone():
            raise ScriptDraftError(409, "SOURCE_ASSET_ALREADY_USED", "新底片已作为任务其他素材使用。")
        task_root = state["task_root"]
        artifacts = state["artifacts"]
        history = task_root / "history" / "source-revisions"
        if not history.resolve().is_relative_to(task_root):
            raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务历史目录不可用。")
        archive = history / uuid.uuid4().hex
        history.mkdir(parents=True, exist_ok=True)
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
            "previous_brief_digest": canonical_digest(state["task"]["brief"]),
            "rework_id": effective_rework_id,
        }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        (archive / "scene-cards.json").write_text(json.dumps({
            "cards": state["cards"], "feedback": state["feedback"],
            "script_segments": state["script_segments"],
            "reshoot_requirements": state["reshoot_requirements"],
            "rework_id": effective_rework_id,
        }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        if artifacts.is_dir():
            os.replace(artifacts, archive / "artifacts")
            moved_artifacts = True
        artifacts.mkdir()
        created_artifacts = True
        _register_source_plan_connection(connection, plan, snapshot)
        connection.execute("DELETE FROM task_assets WHERE task_id=? AND asset_id=?", (task_id, old_assets[0]["asset_id"]))
        connection.execute("INSERT INTO task_assets(task_id,asset_id,role) VALUES(?,?,'digital_human')", (task_id, plan["asset_id"]))
        brief = dict(state["task"]["brief"])
        brief["digital_human_source"] = source_record
        connection.execute(
            "UPDATE tasks SET brief_json=?,status='draft',updated_at=datetime('now') WHERE task_id=?",
            (json.dumps(brief, ensure_ascii=False), task_id),
        )
        connection.execute("DELETE FROM scene_cards WHERE task_id=?", (task_id,))
        from .script_segments import archive_script_segments
        archive_script_segments(connection, task_id, effective_rework_id)

        connection.execute("UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?", (task_id,))
        if confirm_cost:
            approve_rework_cost(db, task_id, effective_rework_id)
        execute_rework(db, task_id, effective_rework_id, idempotency_key=request_id.strip())
        updated = connection.execute("SELECT impact_json FROM rework_requests WHERE rework_id=?", (effective_rework_id,)).fetchone()
        impact = json.loads(updated["impact_json"] or "{}")
        receipt = {
            "taskId": task_id, "reworkId": effective_rework_id, "status": "applied",
            "targetStage": "brief_lock", "sourceAssetId": plan["asset_id"],
            "actualAffectedArtifacts": state["affected"], "archive": str(archive),
        }
        impact.update({
            "source_revised": True, "source_commit_request_id": request_id.strip(),
            "source_commit_request_digest": request_digest,
            "source_commit_receipt": receipt,
        })
        connection.execute(
            "UPDATE rework_requests SET impact_json=? WHERE rework_id=?",
            (json.dumps(impact, ensure_ascii=False, sort_keys=True), effective_rework_id),
        )
        _ensure_source_unchanged(source_path, state["source_identity"])
        connection.commit()
        return {"receipt": receipt}
    except Exception:
        try:
            if created_artifacts and artifacts is not None and artifacts.exists():
                shutil.rmtree(artifacts)
            if moved_artifacts:
                os.replace(archive / "artifacts", artifacts)
            if archive is not None and archive.exists():
                shutil.rmtree(archive)
        finally:
            connection.rollback()
        raise
