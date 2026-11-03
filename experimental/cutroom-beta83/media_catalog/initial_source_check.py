"""Durable, creation-time source inspection for the task's chosen libraries."""

from __future__ import annotations

import hashlib
import json

from .task_sources import source_readiness, sync_task_sources
from .workbench_worker import enqueue_job
from .workflow import complete_stage


def list_initial_source_check_statuses(db) -> dict[str, str]:
    """Project durable check status for task lists without scanning media."""
    return {
        row["task_id"]: row["status"]
        for row in db.connection.execute(
            """SELECT c.task_id,CASE
               WHEN j.status IN ('failed','interrupted','cancelled','stale') THEN 'failed'
               WHEN j.status IN ('queued','running') THEN j.status
               WHEN j.status='pending' THEN 'queued' ELSE c.status END AS status
               FROM task_source_checks c LEFT JOIN jobs j ON j.job_id=c.job_id"""
        )
    }


def get_initial_source_check(db, task_id: str) -> dict | None:
    row = db.connection.execute(
        """SELECT task_id,job_id,status,source_revision,summary_json,checked_at
           FROM task_source_checks WHERE task_id=?""",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    summary_json = result.pop("summary_json")
    result["summary"] = None if summary_json is None else json.loads(summary_json)
    job = db.connection.execute("SELECT status FROM jobs WHERE job_id=?", (result["job_id"],)).fetchone()
    if job is not None:
        if job["status"] in {"queued", "running", "pending"}:
            result["status"] = "queued" if job["status"] == "pending" else job["status"]
        elif job["status"] in {"failed", "interrupted", "cancelled", "stale"}:
            result["status"] = "failed"
    return result


def enqueue_initial_source_check(db, task_id: str) -> str:
    """Queue once while creating a task, without scanning on the request thread."""
    existing = get_initial_source_check(db, task_id)
    if existing is not None:
        return existing["job_id"]
    setting = db.connection.execute(
        "SELECT revision FROM task_source_settings WHERE task_id=?", (task_id,)
    ).fetchone()
    if setting is None:
        raise ValueError("任务尚未绑定素材范围")
    job_id = f"job-initial-source-{task_id}"
    revision = int(setting["revision"])

    def record_queued(connection):
        connection.execute(
            """INSERT INTO task_source_checks(
                 task_id,job_id,status,source_revision
               ) VALUES(?,?,'queued',?)""",
            (task_id, job_id, revision),
        )

    return enqueue_job(
        db,
        job_id=job_id,
        job_type="initial_source_check",
        task_id=task_id,
        payload={"action": "initial_source_check", "task_id": task_id},
        cost_status="waived",
        pre_enqueue_check=record_queued,
    )


def _observation_digest(preview: dict) -> str:
    observed = {
        "configuration": preview["configuration"],
        "scopes": [
            {
                "scope_id": scope["scope_id"],
                "baseline_digest": scope["baseline_digest"],
                "proposed": scope["proposed"],
                "online": scope["online"],
            }
            for scope in preview["scopes"]
        ],
    }
    encoded = json.dumps(observed, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_result(db, task_id: str, status: str, preview: dict) -> dict:
    summary = {
        "reason": preview["reason"],
        "scope_count": len(preview["scopes"]),
        "changed": bool(preview["changed"]),
        "health_status": preview["health"]["status"],
        "observation_digest": _observation_digest(preview),
    }
    with db.transaction() as connection:
        connection.execute(
            """UPDATE task_source_checks
               SET status=?,summary_json=?,checked_at=datetime('now'),
                   updated_at=datetime('now') WHERE task_id=?""",
            (status, json.dumps(summary, ensure_ascii=False, sort_keys=True), task_id),
        )
    return summary


def run_initial_source_check(db, job, project_root=None, runner=None) -> dict:
    """Scan new-task changes before a user finishes the script, then record proof."""
    _ = project_root, runner
    task_id = job.task_id
    record = get_initial_source_check(db, task_id)
    if record is None or record["job_id"] != job.job_id:
        raise RuntimeError("INITIAL_SOURCE_CHECK_MISSING: 创建检查记录不存在")
    with db.transaction() as connection:
        connection.execute(
            """UPDATE task_source_checks SET status='running',updated_at=datetime('now')
               WHERE task_id=?""",
            (task_id,),
        )
    try:
        preview = source_readiness(db, task_id)
        if preview["reason"] != "primary_offline" and preview["changed"]:
            has_downstream = db.connection.execute(
                "SELECT 1 FROM scene_cards WHERE task_id=? LIMIT 1", (task_id,)
            ).fetchone() is not None
            if not has_downstream:
                sync_task_sources(db, task_id, impact_token=preview["impact_token"])
                preview = source_readiness(db, task_id)
        status = (
            "ready" if preview["ready"] else
            "offline" if preview["reason"] == "primary_offline" else
            "review_required"
        )
        summary = _record_result(db, task_id, status, preview)
        if status == "ready":
            complete_stage(
                db, task_id, "material_sync",
                input_digest=summary["observation_digest"],
                details={"initial_source_check_job_id": job.job_id, **summary},
            )
    except BaseException:
        with db.transaction() as connection:
            connection.execute(
                """UPDATE task_source_checks SET status='failed',
                   updated_at=datetime('now') WHERE task_id=?""",
                (task_id,),
            )
        raise
    return {
        "cursor": "initial_source_check:complete",
        "phase_label": "素材自动检查完成",
        "progress": {"current": 1, "total": 1},
        "result": {"status": status, **summary},
    }
