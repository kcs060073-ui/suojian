"""Preview and atomically apply a candidate-bound script revision."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import time
import uuid

from .provenance import canonical_digest
from .rework import approve_rework_cost, execute_rework, get_rework_request, preview_rework_impact
from .task_script import ScriptDraftError, read_task_script
from .tasks import get_task
from .workflow import project_gates


_TOKEN_SECRET = secrets.token_bytes(32)
_TOKEN_TTL_SECONDS = 300


def pre_candidate_rework_available(db, task_id: str) -> bool:
    """Only an approved brief without a rendered candidate may use the early route."""
    return (
        project_gates(db, task_id)["brief_locked"]
        and db.connection.execute(
            "SELECT 1 FROM candidate_versions WHERE task_id=? LIMIT 1", (task_id,),
        ).fetchone() is None
        and db.connection.execute(
            "SELECT 1 FROM render_snapshots WHERE task_id=? AND stage='mix' LIMIT 1",
            (task_id,),
        ).fetchone() is None
    )


def _current_downstream_approvals(db, task_id: str) -> tuple[dict, list[dict]]:
    """Describe approvals that really lose currency when the brief changes."""
    gates = project_gates(db, task_id)
    picture = db.connection.execute(
        "SELECT lock_version,selection_digest,changed_since_lock FROM picture_lock WHERE task_id=?",
        (task_id,),
    ).fetchone()
    plan = db.connection.execute(
        "SELECT plan_id,plan_name,confirmed_at FROM packaging_plans "
        "WHERE task_id=? AND status='confirmed' ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    events = {
        kind: db.connection.execute(
            "SELECT event_id FROM task_gate_events WHERE task_id=? AND gate_type=? "
            "ORDER BY rowid DESC LIMIT 1", (task_id, kind),
        ).fetchone()
        for kind in ("picture_lock", "packaging")
    }
    state = {
        "gates": {kind: gates[kind] for kind in ("picture_lock", "packaging_confirmed")},
        "picture": dict(picture) if picture is not None else None,
        "plan": dict(plan) if plan is not None else None,
        "event_ids": {kind: row["event_id"] if row is not None else None
                      for kind, row in events.items()},
    }
    affected = []
    if gates["picture_lock"] and picture is not None:
        affected.append({
            "kind": "picture_lock", "id": picture["lock_version"],
            "name": "当前画面确认",
        })
    if gates["packaging_confirmed"] and plan is not None:
        affected.append({
            "kind": "packaging_plan", "id": plan["plan_id"],
            "name": f"已确认包装方案 · {plan['plan_name']}",
        })
    return state, affected


def _checked_text(text: str) -> str:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 200_000:
        raise ScriptDraftError(400, "INVALID_REQUEST", "文案必须是有效文本且不超过 200KB。")
    return text


def _task_paths(project_root: Path, task_id: str) -> tuple[Path, Path, Path]:
    tasks_root = (Path(project_root) / "runtime" / "workbench" / "tasks").resolve()
    task_root = (tasks_root / task_id).resolve()
    if (not task_id or Path(task_id).name != task_id or task_root == tasks_root
            or not task_root.is_relative_to(tasks_root) or not task_root.is_dir()):
        raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务目录不可用。")
    script_path = task_root / "inputs" / "final-script.txt"
    artifacts = task_root / "artifacts"
    if artifacts.is_symlink() or (artifacts.exists() and not artifacts.is_dir()):
        raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务产物目录不可用。")
    return task_root, script_path, artifacts


def _manifest(artifacts: Path) -> list[dict]:
    if not artifacts.exists():
        return []
    result = []
    for path in sorted(artifacts.rglob("*")):
        if path.is_symlink():
            raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务产物包含符号链接。")
        stat = path.stat()
        content_sha256 = None
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            content_sha256 = digest.hexdigest()
        result.append({
            "path": path.relative_to(artifacts).as_posix(),
            "type": "directory" if path.is_dir() else "file",
            "size": stat.st_size if path.is_file() else 0,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": content_sha256,
        })
    return result


def _state(db, project_root: Path, task_id: str, rework_id: str | None, text: str) -> dict:
    task = get_task(db, task_id)
    if task is None:
        raise ScriptDraftError(404, "NOT_FOUND", "任务不存在。")
    task_root, script_path, artifacts = _task_paths(project_root, task_id)
    previous = read_task_script(project_root, task)
    brief = task.get("brief") or {}
    old_sha = (brief.get("final_script") or {}).get("sha256")
    if previous is None or hashlib.sha256(previous.encode("utf-8")).hexdigest() != old_sha:
        raise ScriptDraftError(409, "SCRIPT_CHANGED", "文案文件与保存版本不一致，请恢复后重试。")
    current = db.connection.execute(
        "SELECT candidate_id,version_no,status,qa_report_id,qa_snapshot_id FROM candidate_versions "
        "WHERE task_id=? ORDER BY version_no DESC LIMIT 1", (task_id,),
    ).fetchone()
    if rework_id is None:
        if not pre_candidate_rework_available(db, task_id):
            raise ScriptDraftError(409, "REWORK_ROUTE_INVALID", "当前阶段不能直接修改已确认文案。")
        row = {
            "rework_id": None, "source_candidate_id": None,
            "status": "pending_confirmation", "cost_status": "none", "execution_key": None,
        }
    else:
        row = db.connection.execute(
            "SELECT * FROM rework_requests WHERE task_id=? AND rework_id=?", (task_id, rework_id),
        ).fetchone()
        if row is None:
            raise ScriptDraftError(404, "REWORK_NOT_FOUND", "文案返工单不存在。")
        if row["category"] != "script" or row["target_stage"] != "brief_lock":
            raise ScriptDraftError(409, "REWORK_ROUTE_INVALID", "当前返工单不是文案返工。")
        if current is None or current["candidate_id"] != row["source_candidate_id"]:
            raise ScriptDraftError(409, "REWORK_SOURCE_STALE", "返工来源已不是当前候选。")
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
    manifest = _manifest(artifacts)
    approval_state, approval_affected = _current_downstream_approvals(db, task_id)
    revision = canonical_digest({
        "task_id": task_id, "brief": brief, "old_sha": old_sha,
        "rework": (row["rework_id"], row["status"], row["cost_status"], row["execution_key"]),
        "candidate": dict(current) if current is not None else None, "cards": cards, "feedback": feedback, "script_segments": segments, "reshoot_requirements": requirements,
        "jobs": jobs, "packages": packages, "artifacts": manifest,
        "downstream_approvals": approval_state,
    })
    affected = []
    if text != previous:
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
        affected.extend({
            "kind": "publish_package", "id": item["record_id"],
            "name": f"交付发布包 · {item['record_id'][:8]}",
        } for item in packages if item["package_status"] == "current")
    return {
        "task": task, "task_root": task_root, "script_path": script_path,
        "artifacts": artifacts, "previous": previous, "old_sha": old_sha,
        "rework": row, "cards": cards, "feedback": feedback, "script_segments": segments, "reshoot_requirements": requirements,
        "running_jobs": jobs, "affected": affected, "revision": revision,
    }


def _encode_token(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(_TOKEN_SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _verify_token(token: str, payload: dict) -> None:
    try:
        body, signature = token.rsplit(".", 1)
        expected_signature = hmac.new(_TOKEN_SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError
        decoded = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (AttributeError, ValueError, UnicodeError, binascii.Error, json.JSONDecodeError) as error:
        raise ScriptDraftError(409, "IMPACT_TOKEN_INVALID", "影响预览已失效，请重新预览。") from error
    issued_at = decoded.get("issued_at") if isinstance(decoded, dict) else None
    if not isinstance(issued_at, int) or time.time() - issued_at > _TOKEN_TTL_SECONDS or time.time() < issued_at - 5:
        raise ScriptDraftError(409, "IMPACT_TOKEN_EXPIRED", "影响预览已过期，请重新预览。")
    if decoded != {**payload, "issued_at": issued_at}:
        raise ScriptDraftError(409, "SCRIPT_REWORK_STALE", "返工影响已变化，请重新预览。")


def preview_script_rework(db, project_root: Path, task_id: str, rework_id: str | None, text: str) -> dict:
    text = _checked_text(text)
    state = _state(db, project_root, task_id, rework_id, text)
    if state["rework"]["status"] not in {"draft", "pending_confirmation"}:
        raise ScriptDraftError(409, "REWORK_NOT_PENDING", "当前返工单已处理。")
    new_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    issued_at = int(time.time())
    token = _encode_token({
        "task_id": task_id, "rework_id": rework_id,
        "revision": state["revision"], "new_sha": new_sha,
        "issued_at": issued_at,
    })
    return {
        "token": token, "base_revision": state["revision"],
        "dependency_digest": state["revision"],
        "changed_inputs": [] if text == state["previous"] else [{"field": "final_script", "before": state["old_sha"], "after": new_sha}],
        "actual_affected_artifacts": state["affected"],
        "running_jobs": state["running_jobs"],
        "changed": text != state["previous"],
        "ready": text != state["previous"] and not state["running_jobs"],
        "cost_confirmation_required": state["rework"]["cost_status"] == "confirmation_required",
        "expires_at": issued_at + _TOKEN_TTL_SECONDS,
    }


def commit_script_rework(
    db, project_root: Path, task_id: str, rework_id: str | None, text: str,
    token: str, *, expected_revision: str, request_id: str, confirm_cost: bool = False,
) -> dict:
    text = _checked_text(text)
    if not isinstance(request_id, str) or not request_id.strip():
        raise ScriptDraftError(400, "REQUEST_ID_REQUIRED", "缺少本次提交编号。")
    request_digest = canonical_digest({
        "task_id": task_id, "rework_id": rework_id, "text": text,
        "token": token, "expected_revision": expected_revision,
        "confirm_cost": confirm_cost,
    })
    if db.connection.in_transaction:
        raise RuntimeError("Script rework commit requires its own transaction")
    connection = db.connection
    connection.execute("BEGIN IMMEDIATE")
    archive = artifacts = script_path = temporary = None
    moved_artifacts = created_artifacts = replaced = False
    previous = None
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
        new_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if existing is not None and existing["status"] == "applied":
            impact = json.loads(existing["impact_json"] or "{}")
            if impact.get("script_commit_request_id") != request_id.strip() or impact.get("script_commit_request_digest") != request_digest:
                raise ScriptDraftError(409, "REWORK_IDEMPOTENCY_CONFLICT", "提交编号已用于不同返工内容。")
            applied_rework_id = rework_id or impact["script_commit_receipt"]["reworkId"]
            result = {
                "task": impact.get("script_commit_task") or get_task(db, task_id),
                "rework": get_rework_request(db, task_id, applied_rework_id),
                "archive": impact.get("script_archive"),
                "receipt": impact.get("script_commit_receipt"),
            }
            connection.commit()
            return result
        if rework_id is None and existing is not None:
            raise ScriptDraftError(409, "REWORK_IDEMPOTENCY_CONFLICT", "提交编号已用于其他返工请求。")
        state = _state(db, project_root, task_id, rework_id, text)
        row = state["rework"]
        if row["status"] not in {"draft", "pending_confirmation"}:
            raise ScriptDraftError(409, "REWORK_NOT_PENDING", "当前返工单不可提交。")
        if text == state["previous"]:
            raise ScriptDraftError(409, "SCRIPT_UNCHANGED", "文案没有变化，无需返工。")
        if state["running_jobs"]:
            raise ScriptDraftError(409, "TASK_BUSY", "任务正在处理，请等待作业结束。")
        if expected_revision != state["revision"]:
            raise ScriptDraftError(409, "SCRIPT_REWORK_STALE", "返工影响已变化，请重新预览。")
        _verify_token(token, {
            "task_id": task_id, "rework_id": rework_id,
            "revision": state["revision"], "new_sha": new_sha,
        })
        if row["cost_status"] == "confirmation_required" and not confirm_cost:
            raise ScriptDraftError(409, "REWORK_COST_CONFIRMATION_REQUIRED", "请确认后续外部调用费用。")

        effective_rework_id = rework_id
        if effective_rework_id is None:
            effective_rework_id = f"rework-{uuid.uuid4().hex[:16]}"
            impact = preview_rework_impact("script")
            impact["origin"] = "pre_candidate_script_revision"
            impact["cost_confirmation_required"] = False
            connection.execute(
                """INSERT INTO rework_requests(
                  rework_id,task_id,source_candidate_id,category,notes,target_stage,
                  impact_json,cost_status,status,idempotency_key
                ) VALUES(?,?,NULL,'script',?,'brief_lock',?,'none','pending_confirmation',?)""",
                (effective_rework_id, task_id, "用户修改已确认文案",
                 json.dumps(impact, ensure_ascii=False, sort_keys=True), request_id.strip()),
            )

        task_root = state["task_root"]
        script_path = state["script_path"]
        artifacts = state["artifacts"]
        previous = state["previous"]
        history = task_root / "history" / "script-revisions"
        if not history.resolve().is_relative_to(task_root):
            raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务历史目录不可用。")
        archive = history / uuid.uuid4().hex
        history.mkdir(parents=True, exist_ok=True)
        archive.mkdir()
        (archive / "previous-script.txt").write_text(previous, encoding="utf-8")
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
        temporary = script_path.with_name(f".script-{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(text.encode("utf-8"))
        os.replace(temporary, script_path)
        replaced = True

        connection.execute("DELETE FROM scene_cards WHERE task_id=?", (task_id,))
        from .script_segments import archive_script_segments
        archive_script_segments(connection, task_id, effective_rework_id)

        connection.execute("UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?", (task_id,))
        brief = dict(state["task"]["brief"])
        brief["final_script"] = {"relative_path": "inputs/final-script.txt", "sha256": new_sha}
        connection.execute(
            "UPDATE tasks SET brief_json=?,status='draft',updated_at=datetime('now') WHERE task_id=?",
            (json.dumps(brief, ensure_ascii=False), task_id),
        )
        if confirm_cost:
            approve_rework_cost(db, task_id, effective_rework_id)
        execute_rework(db, task_id, effective_rework_id, idempotency_key=request_id.strip())
        current = connection.execute(
            "SELECT impact_json FROM rework_requests WHERE rework_id=?", (effective_rework_id,),
        ).fetchone()
        impact = json.loads(current["impact_json"] or "{}")
        impact.update({
            "script_revised": True,
            "script_commit_request_id": request_id.strip(),
            "script_commit_request_digest": request_digest,
            "script_commit_sha256": new_sha,
            "script_archive": str(archive),
            "script_commit_task": get_task(db, task_id),
            "script_commit_receipt": {
                "taskId": task_id, "reworkId": effective_rework_id,
                "status": "applied", "targetStage": "brief_lock",
                "scriptSha256": new_sha, "finalScriptText": text,
                "actualAffectedArtifacts": state["affected"],
            },
        })
        connection.execute(
            "UPDATE rework_requests SET impact_json=? WHERE rework_id=?",
            (json.dumps(impact, ensure_ascii=False, sort_keys=True), effective_rework_id),
        )
        result = {
            "task": get_task(db, task_id),
            "rework": get_rework_request(db, task_id, effective_rework_id),
            "archive": str(archive), "receipt": impact["script_commit_receipt"],
        }
        connection.commit()
        return result
    except Exception:
        try:
            if replaced and previous is not None:
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                temporary.write_bytes(previous.encode("utf-8"))
                os.replace(temporary, script_path)
            if archive is not None:
                if created_artifacts and artifacts.exists():
                    shutil.rmtree(artifacts)
                if moved_artifacts:
                    os.replace(archive / "artifacts", artifacts)
                if archive.exists():
                    shutil.rmtree(archive)
        finally:
            connection.rollback()
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
