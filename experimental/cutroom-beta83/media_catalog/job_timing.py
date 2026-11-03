"""Evidence-bounded timing estimates for the durable heavy-job queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median


def _as_utc(value: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime):
        raise TypeError("observed_at 必须是 datetime")
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _sample_duration(db, job_type: str) -> float | None:
    rows = db.connection.execute(
        """
        SELECT (julianday(finished_at)-julianday(started_at))*86400.0 AS seconds
        FROM jobs
        WHERE job_type=? AND status='completed'
          AND started_at IS NOT NULL AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, rowid DESC
        LIMIT 20
        """,
        (job_type,),
    ).fetchall()
    samples = [float(row["seconds"]) for row in rows if row["seconds"] is not None and float(row["seconds"]) > 0]
    return None if not samples else float(median(samples))


def _elapsed_seconds(job, observed):
    try:
        started = datetime.fromisoformat(str(job.get("started_at") or "").replace("Z", "+00:00"))
        return max(0, int((observed - _as_utc(started)).total_seconds()))
    except (ValueError, TypeError):
        return None


def _remaining_seconds(db, job: dict) -> float | None:
    estimate = _sample_duration(db, str(job.get("job_type") or ""))
    if estimate is None:
        return None
    total = int(job.get("progress_total") or 0)
    current = int(job.get("progress_current") or 0)
    if total > 0:
        ratio = min(1.0, max(0.0, current / total))
        return max(0.0, estimate * (1.0 - ratio))
    return estimate


def _queued_ahead_seconds(db, job: dict) -> float | None:
    sequence = job.get("queue_sequence")
    if sequence is None:
        return None
    rows = db.connection.execute(
        """
        SELECT j.job_type,j.status,j.progress_current,j.progress_total
        FROM jobs j
        LEFT JOIN job_queue_entries q ON q.job_id=j.job_id
        LEFT JOIN job_controls control ON control.job_id=j.job_id
        WHERE (
          j.status='running'
          OR (
            j.status='queued' AND q.queue_sequence<?
            AND COALESCE(control.state,'active')='active'
          )
        )
        ORDER BY CASE j.status WHEN 'running' THEN 0 ELSE 1 END,q.queue_sequence
        """,
        (sequence,),
    ).fetchall()
    total = 0.0
    for row in rows:
        remaining = _remaining_seconds(db, dict(row))
        if remaining is None:
            return None
        total += remaining
    return total


def job_timing(db, job: dict, *, observed_at: datetime | None = None) -> dict:
    observed = _as_utc(observed_at)
    result = {
        "estimated_start_at": None,
        "estimated_remaining_seconds": None,
        "estimate_status": "unknown",
        "observed_at": _timestamp(observed),
        "elapsed_seconds": None,
        "estimate_basis": None,
    }
    status = str(job.get("public_status") or job.get("status") or "")
    result["heartbeat_expired"] = False
    if status == "running" and job.get("lease_expires_at"):
        try:
            deadline = datetime.fromisoformat(str(job["lease_expires_at"]).replace("Z", "+00:00"))
            result["heartbeat_expired"] = _as_utc(deadline) < observed
        except (ValueError, TypeError):
            pass
    if status == "completed":
        return {
            **result,
            "estimated_start_at": result["observed_at"],
            "estimated_remaining_seconds": 0,
            "estimate_status": "complete",
        }
    if status == "running":
        result["elapsed_seconds"] = _elapsed_seconds(job, observed)
        elapsed = result["elapsed_seconds"]
        current, total = int(job.get("progress_current") or 0), int(job.get("progress_total") or 0)
        remaining = _remaining_seconds(db, job)
        basis = "history"
        if remaining is None and elapsed is not None and elapsed >= 5 and 0 < current < total:
            remaining = elapsed * (total - current) / current
            basis = "progress"
        if remaining is None:
            return result
        return {
            **result,
            "estimated_start_at": result["observed_at"],
            "estimated_remaining_seconds": int(round(remaining)),
            "estimate_status": "estimated",
            "estimate_basis": basis,
        }
    if status == "queued":
        own_duration = _sample_duration(db, str(job.get("job_type") or ""))
        ahead = _queued_ahead_seconds(db, job)
        if own_duration is None or ahead is None:
            return result
        return {
            **result,
            "estimated_start_at": _timestamp(observed + timedelta(seconds=ahead)),
            "estimated_remaining_seconds": int(round(own_duration)),
            "estimate_basis": "history",
            "estimate_status": "estimated",
        }
    return result
