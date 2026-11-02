"""Lease-backed FIFO execution for persistent local workbench jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import threading
from typing import Any, Callable, Mapping
import uuid

from .database import CatalogDatabase
from .jobs import HeavyJobBusy, JobManager, bounded_error


DEFAULT_LEASE_SECONDS = 30


class JobRequestConflict(ValueError):
    """A provider idempotency key was reused for a different request."""

    code = "JOB_IDEMPOTENCY_CONFLICT"


class ProviderOutcomeUnknown(RuntimeError):
    """The provider may have accepted work but no terminal result is known."""

    def __init__(self, message: str, *, provider_state: str = "result_unknown"):
        self.provider_state = provider_state
        super().__init__(message)


class JobInterrupted(RuntimeError):
    """A deterministic handler stopped at a recoverable boundary."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    job_type: str
    payload: dict[str, Any]
    attempt_no: int
    worker_id: str
    task_id: str | None
    is_paid: bool


@dataclass(frozen=True)
class JobAttempt:
    job_id: str
    attempt_no: int
    worker_id: str
    status: str
    error_summary: str | None
    started_at: str
    finished_at: str | None


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    payload_json = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return payload_json, digest


def _paid_or_generative(
    job_type: str,
    cost_status: str,
    provider_request_id: str | None,
) -> bool:
    return (
        job_type in JobManager.PAID_OR_GENERATIVE_TYPES
        or provider_request_id is not None
        or cost_status in {"estimated", "approved", "incurred"}
    )


def _now(clock: Callable[[], datetime] | None = None) -> datetime:
    value = datetime.now(timezone.utc) if clock is None else clock()
    if not isinstance(value, datetime):
        raise TypeError("clock 必须返回 datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _lease_times(
    clock: Callable[[], datetime] | None,
    lease_seconds: int,
) -> tuple[str, str]:
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or lease_seconds <= 0
    ):
        raise ValueError("lease_seconds 必须是正整数")
    now = _now(clock)
    return _timestamp(now), _timestamp(now + timedelta(seconds=lease_seconds))


def enqueue_job(
    db,
    *,
    job_id: str,
    job_type: str,
    payload: Mapping[str, Any],
    task_id: str | None = None,
    library_id: str | None = None,
    cost_status: str = "unknown",
    provider_request_id: str | None = None,
    retry_policy: str = "explicit",
) -> str:
    """Persist one request and assign its database-generated FIFO sequence."""
    if job_type not in JobManager.HEAVY_TYPES:
        raise ValueError(f"不支持的重任务类型：{job_type}")
    if task_id is not None and (
        not isinstance(task_id, str) or not task_id.strip()
    ):
        raise ValueError("task_id 必须是非空字符串或 None")
    if library_id is not None and (
        not isinstance(library_id, str) or not library_id.strip()
    ):
        raise ValueError("library_id 必须是非空字符串或 None")
    if retry_policy not in {"explicit", "never"}:
        raise ValueError("retry_policy 必须是 explicit 或 never")
    payload_json, payload_digest = _canonical_payload(payload)
    is_paid = _paid_or_generative(
        job_type, cost_status, provider_request_id
    )
    if is_paid:
        retry_policy = "never"

    with db.transaction(immediate=True) as connection:
        if provider_request_id is not None:
            existing = connection.execute(
                """
                SELECT j.job_id, j.task_id, j.library_id, j.job_type,
                       p.payload_json, p.payload_digest, p.is_paid,
                       p.requested_cost_status
                FROM jobs AS j
                JOIN job_payloads AS p ON p.job_id=j.job_id
                WHERE p.provider_request_id=?
                """,
                (provider_request_id,),
            ).fetchone()
            if existing is not None:
                existing_digest = existing["payload_digest"]
                if not existing_digest:
                    existing_digest = hashlib.sha256(
                        json.dumps(
                            json.loads(existing["payload_json"]),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                    ).hexdigest()
                identical = (
                    existing["task_id"] == task_id
                    and existing["library_id"] == library_id
                    and existing["job_type"] == job_type
                    and existing_digest == payload_digest
                    and bool(existing["is_paid"]) == is_paid
                    and existing["requested_cost_status"] == cost_status
                )
                if not identical:
                    raise JobRequestConflict(
                        "provider_request_id 已绑定不同任务或请求内容"
                    )
                return existing["job_id"]

        if connection.execute(
            "SELECT 1 FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone():
            raise ValueError(f"任务编号已存在：{job_id}")

        connection.execute(
            """
            INSERT INTO jobs(
              job_id, job_type, task_id, library_id, status,
              created_at, updated_at, retry_policy
            ) VALUES(
              ?, ?, ?, ?, 'queued', datetime('now'), datetime('now'), ?
            )
            """,
            (job_id, job_type, task_id, library_id, retry_policy),
        )
        connection.execute(
            """
            INSERT INTO job_payloads(
              job_id, payload_json, payload_digest, cost_status,
              requested_cost_status,
              provider_request_id, is_paid, outcome_status
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'not_started')
            """,
            (
                job_id,
                payload_json,
                payload_digest,
                cost_status,
                cost_status,
                provider_request_id,
                int(is_paid),
            ),
        )
        connection.execute(
            "INSERT INTO job_queue_entries(job_id) VALUES(?)", (job_id,)
        )
        connection.execute(
            "INSERT INTO job_controls(job_id,state) VALUES(?,'active')",
            (job_id,),
        )
    return job_id


def claim_next_heavy_job(
    db,
    *,
    worker_id: str,
    clock: Callable[[], datetime] | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    expected_job_id: str | None = None,
) -> ClaimedJob | None:
    """Atomically claim the FIFO head when no heavy job currently runs."""
    now, lease_expires = _lease_times(clock, lease_seconds)
    heavy_types = tuple(sorted(JobManager.HEAVY_TYPES))
    placeholders = ",".join("?" for _ in heavy_types)
    with db.transaction(immediate=True) as connection:
        running = connection.execute(
            f"""
            SELECT 1 FROM jobs
            WHERE status='running' AND job_type IN ({placeholders})
            LIMIT 1
            """,
            heavy_types,
        ).fetchone()
        if running is not None:
            return None

        row = connection.execute(
            f"""
            SELECT j.job_id, j.job_type, j.task_id,
                   p.payload_json, p.is_paid
            FROM job_queue_entries AS q
            JOIN jobs AS j ON j.job_id=q.job_id
            JOIN job_payloads AS p ON p.job_id=j.job_id
            LEFT JOIN job_controls AS control ON control.job_id=j.job_id
            WHERE j.status='queued'
              AND j.cancel_requested=0
              AND COALESCE(control.state,'active')='active'
              AND j.job_type IN ({placeholders})
            ORDER BY q.queue_sequence
            LIMIT 1
            """,
            heavy_types,
        ).fetchone()
        if row is None:
            return None
        if expected_job_id is not None and row["job_id"] != expected_job_id:
            return None

        attempt_no = connection.execute(
            """
            SELECT COALESCE(MAX(attempt_no), 0) + 1
            FROM job_attempts WHERE job_id=?
            """,
            (row["job_id"],),
        ).fetchone()[0]
        changed = connection.execute(
            """
            UPDATE jobs
            SET status='running', worker_id=?, heartbeat_at=?,
                lease_expires_at=?, started_at=?, finished_at=NULL,
                error_summary=NULL, updated_at=?
            WHERE job_id=? AND status='queued' AND cancel_requested=0
            """,
            (
                worker_id,
                now,
                lease_expires,
                now,
                now,
                row["job_id"],
            ),
        ).rowcount
        if changed != 1:
            return None
        connection.execute(
            """
            INSERT INTO job_attempts(
              job_id, attempt_no, worker_id, status, started_at
            ) VALUES(?, ?, ?, 'running', ?)
            """,
            (row["job_id"], attempt_no, worker_id, now),
        )
        if row["is_paid"]:
            connection.execute(
                """
                UPDATE job_payloads
                SET outcome_status='in_progress',
                    provider_state='worker_claimed',
                    outcome_reason=NULL,
                    updated_at=?
                WHERE job_id=?
                """,
                (now, row["job_id"]),
            )

    return ClaimedJob(
        job_id=row["job_id"],
        job_type=row["job_type"],
        payload=json.loads(row["payload_json"]),
        attempt_no=attempt_no,
        worker_id=worker_id,
        task_id=row["task_id"],
        is_paid=bool(row["is_paid"]),
    )


def renew_job_lease(
    db,
    job_id: str,
    *,
    worker_id: str,
    clock: Callable[[], datetime] | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    now, lease_expires = _lease_times(clock, lease_seconds)
    with db.transaction(immediate=True) as connection:
        changed = connection.execute(
            """
            UPDATE jobs
            SET heartbeat_at=?, lease_expires_at=?, updated_at=?
            WHERE job_id=? AND status='running' AND worker_id=?
            """,
            (now, lease_expires, now, job_id, worker_id),
        ).rowcount
    if changed != 1:
        raise RuntimeError(f"任务租约当前不可续期：{job_id}")


heartbeat_job = renew_job_lease


def recover_interrupted_jobs(
    db,
    *,
    worker_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Recover only expired leases.

    A worker id can be reused accidentally by two live processes, so ownership
    alone is not proof that the previous process died. The lease expiry is the
    single recovery authority.
    """
    now = _timestamp(_now(clock))
    _ = worker_id  # Kept for API compatibility; never weakens lease checks.
    with db.transaction(immediate=True) as connection:
        rows = connection.execute(
            """
            SELECT j.job_id, j.worker_id, p.is_paid
            FROM jobs AS j
            JOIN job_payloads AS p ON p.job_id=j.job_id
            WHERE j.status='running'
              AND (j.lease_expires_at IS NULL OR j.lease_expires_at<=?)
            """,
            (now,),
        ).fetchall()
        for row in rows:
            reason = (
                "工作进程在供应商结果确认前停止"
                if row["is_paid"]
                else "工作进程意外停止，可手动恢复"
            )
            connection.execute(
                """
                UPDATE job_attempts
                SET status='interrupted', error_summary=?, finished_at=?
                WHERE job_id=? AND status='running'
                """,
                (reason, now, row["job_id"]),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status='interrupted', worker_id=NULL,
                    lease_expires_at=NULL, error_summary=?,
                    finished_at=?, updated_at=?
                WHERE job_id=? AND status='running'
                """,
                (reason, now, now, row["job_id"]),
            )
            if row["is_paid"]:
                _set_unknown_outcome(
                    connection,
                    row["job_id"],
                    provider_state="worker_crashed",
                    reason=reason,
                    now=now,
                )
    return len(rows)


def request_cancel(db, job_id: str, *, reason: str) -> None:
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT status FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"任务不存在：{job_id}")
        if row["status"] == "running":
            connection.execute(
                """
                UPDATE job_controls
                SET state='active',requested_at=NULL,paused_at=NULL
                WHERE job_id=?
                """,
                (job_id,),
            )
            connection.execute(
                """
                UPDATE jobs
                SET cancel_requested=1, cancel_reason=?,
                    updated_at=datetime('now')
                WHERE job_id=?
                """,
                (reason, job_id),
            )
            return
        if row["status"] in {"pending", "queued", "interrupted"}:
            connection.execute(
                """
                UPDATE job_controls
                SET state='active',requested_at=NULL,paused_at=NULL
                WHERE job_id=?
                """,
                (job_id,),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status='cancelled', cancel_requested=1, cancel_reason=?,
                    worker_id=NULL, lease_expires_at=NULL,
                    finished_at=datetime('now'), updated_at=datetime('now')
                WHERE job_id=?
                """,
                (reason, job_id),
            )
            return
        if row["status"] == "cancelled":
            return
        raise RuntimeError(f"任务当前不可取消：{job_id}")


def request_job_pause(db, job_id: str) -> None:
    """Pause queued work now, or request a running pause at the next safe boundary."""
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            """
            SELECT j.status,COALESCE(control.state,'active') AS control_state
            FROM jobs j
            LEFT JOIN job_controls control USING(job_id)
            WHERE j.job_id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"任务不存在：{job_id}")
        if row["control_state"] == "paused":
            return
        if row["status"] == "queued":
            connection.execute(
                "DELETE FROM job_queue_entries WHERE job_id=?",
                (job_id,),
            )
            connection.execute(
                """
                INSERT INTO job_controls(job_id,state,requested_at,paused_at)
                VALUES(?,'paused',datetime('now'),datetime('now'))
                ON CONFLICT(job_id) DO UPDATE SET
                  state='paused',requested_at=datetime('now'),paused_at=datetime('now')
                """,
                (job_id,),
            )
            return
        if row["status"] == "running":
            connection.execute(
                """
                INSERT INTO job_controls(job_id,state,requested_at,paused_at)
                VALUES(?,'pause_requested',datetime('now'),NULL)
                ON CONFLICT(job_id) DO UPDATE SET
                  state='pause_requested',requested_at=datetime('now'),paused_at=NULL
                """,
                (job_id,),
            )
            return
        raise RuntimeError(f"任务当前不可暂停：{job_id}")


def cancel_at_safe_point(db, job_id: str, *, worker_id: str) -> bool:
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            """
            SELECT cancel_requested FROM jobs
            WHERE job_id=? AND status='running' AND worker_id=?
            """,
            (job_id, worker_id),
        ).fetchone()
        if row is None or not row["cancel_requested"]:
            return False
        connection.execute(
            """
            UPDATE jobs
            SET status='cancelled', worker_id=NULL, lease_expires_at=NULL,
                finished_at=datetime('now'), updated_at=datetime('now')
            WHERE job_id=? AND status='running' AND worker_id=?
            """,
            (job_id, worker_id),
        )
        _finish_attempt(connection, job_id, worker_id, "cancelled", None)
    return True


def pause_at_safe_point(db, job_id: str, *, worker_id: str) -> bool:
    """Move a running job to a resumable paused checkpoint after result persistence."""
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            """
            SELECT control.state
            FROM jobs j JOIN job_controls control USING(job_id)
            WHERE j.job_id=? AND j.status='running' AND j.worker_id=?
            """,
            (job_id, worker_id),
        ).fetchone()
        if row is None or row["state"] != "pause_requested":
            return False
        changed = connection.execute(
            """
            UPDATE jobs
            SET status='interrupted',worker_id=NULL,lease_expires_at=NULL,
                finished_at=datetime('now'),updated_at=datetime('now')
            WHERE job_id=? AND status='running' AND worker_id=?
            """,
            (job_id, worker_id),
        ).rowcount
        if changed != 1:
            return False
        connection.execute(
            """
            UPDATE job_controls
            SET state='paused',paused_at=datetime('now')
            WHERE job_id=? AND state='pause_requested'
            """,
            (job_id,),
        )
        _finish_attempt(
            connection,
            job_id,
            worker_id,
            "interrupted",
            "使用人在安全步骤暂停",
        )
    return True


def _set_unknown_outcome(
    connection,
    job_id: str,
    *,
    provider_state: str,
    reason: str,
    now: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE job_payloads
        SET outcome_status='unknown', provider_state=?,
            outcome_reason=?, cost_status='unknown',
            updated_at=COALESCE(?, datetime('now'))
        WHERE job_id=?
        """,
        (provider_state, reason, now, job_id),
    )


def mark_provider_outcome_unknown(
    db,
    job_id: str,
    *,
    worker_id: str,
    provider_state: str,
    reason: BaseException | str,
) -> None:
    summary = bounded_error(reason)
    with db.transaction(immediate=True) as connection:
        changed = connection.execute(
            """
            UPDATE jobs
            SET status='interrupted', worker_id=NULL,
                lease_expires_at=NULL, error_summary=?,
                finished_at=datetime('now'), updated_at=datetime('now')
            WHERE job_id=? AND status='running' AND worker_id=?
            """,
            (summary, job_id, worker_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"任务当前不可标记结果未知：{job_id}")
        _set_unknown_outcome(
            connection,
            job_id,
            provider_state=provider_state,
            reason=summary,
        )
        _finish_attempt(
            connection, job_id, worker_id, "interrupted", summary
        )


def fail_job(
    db,
    job_id: str,
    *,
    worker_id: str,
    error: BaseException | str,
) -> None:
    summary = bounded_error(error)
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            """
            SELECT p.is_paid
            FROM jobs AS j JOIN job_payloads AS p USING(job_id)
            WHERE j.job_id=? AND j.status='running' AND j.worker_id=?
            """,
            (job_id, worker_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"任务当前不可标记失败：{job_id}")
        if row["is_paid"]:
            connection.execute(
                """
                UPDATE jobs
                SET status='interrupted', worker_id=NULL,
                    lease_expires_at=NULL, error_summary=?,
                    finished_at=datetime('now'), updated_at=datetime('now')
                WHERE job_id=?
                """,
                (summary, job_id),
            )
            _set_unknown_outcome(
                connection,
                job_id,
                provider_state="handler_error_after_dispatch",
                reason=summary,
            )
            _finish_attempt(
                connection, job_id, worker_id, "interrupted", summary
            )
            return
        connection.execute(
            """
            UPDATE jobs
            SET status='failed', worker_id=NULL, lease_expires_at=NULL,
                error_summary=?, finished_at=datetime('now'),
                updated_at=datetime('now')
            WHERE job_id=?
            """,
            (summary, job_id),
        )
        _finish_attempt(connection, job_id, worker_id, "failed", summary)


def interrupt_job(
    db,
    job_id: str,
    *,
    worker_id: str,
    error: BaseException | str,
) -> None:
    summary = bounded_error(error)
    with db.transaction(immediate=True) as connection:
        changed = connection.execute(
            """
            UPDATE jobs
            SET status='interrupted', worker_id=NULL, lease_expires_at=NULL,
                error_summary=?, finished_at=datetime('now'),
                updated_at=datetime('now')
            WHERE job_id=? AND status='running' AND worker_id=?
            """,
            (summary, job_id, worker_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"任务当前不可中断：{job_id}")
        _finish_attempt(
            connection, job_id, worker_id, "interrupted", summary
        )


def resume_job(db, job_id: str) -> None:
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            """
            SELECT j.status, j.retry_policy, p.outcome_status,
                   COALESCE(control.state,'active') AS control_state
            FROM jobs AS j JOIN job_payloads AS p USING(job_id)
            LEFT JOIN job_controls control USING(job_id)
            WHERE j.job_id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"任务当前不可恢复：{job_id}")
        paused = row["control_state"] == "paused"
        if paused:
            if row["status"] not in {"queued", "interrupted"}:
                raise RuntimeError(f"任务当前不可恢复：{job_id}")
        elif row["status"] not in {"failed", "interrupted"}:
            raise RuntimeError(f"任务当前不可恢复：{job_id}")
        if row["outcome_status"] == "unknown" or (
            not paused and row["retry_policy"] == "never"
        ):
            raise RuntimeError(
                "生成式、付费或供应商结果未知的任务不能重试，请人工核对"
            )
        connection.execute(
            "DELETE FROM job_queue_entries WHERE job_id=?", (job_id,)
        )
        connection.execute(
            """
            UPDATE jobs
            SET status='queued', worker_id=NULL, cancel_requested=0,
                cancel_reason=NULL, lease_expires_at=NULL,
                finished_at=NULL, updated_at=datetime('now')
            WHERE job_id=?
            """,
            (job_id,),
        )
        connection.execute(
            "INSERT INTO job_queue_entries(job_id) VALUES(?)", (job_id,)
        )
        connection.execute(
            """
            INSERT INTO job_controls(job_id,state)
            VALUES(?,'active')
            ON CONFLICT(job_id) DO UPDATE SET
              state='active',requested_at=NULL,paused_at=NULL
            """,
            (job_id,),
        )


def list_job_attempts(db, job_id: str) -> list[JobAttempt]:
    rows = db.connection.execute(
        """
        SELECT job_id, attempt_no, worker_id, status, error_summary,
               started_at, finished_at
        FROM job_attempts WHERE job_id=? ORDER BY attempt_no
        """,
        (job_id,),
    ).fetchall()
    return [JobAttempt(**dict(row)) for row in rows]


def get_job(db, job_id: str) -> dict | None:
    row = db.connection.execute(
        """
        SELECT j.job_id,j.job_type,j.task_id,j.library_id,j.status,j.cursor,
               j.progress_current,j.progress_total,j.phase_label,j.cancel_requested,
               j.cancel_reason,j.error_summary,j.created_at,j.updated_at,
               j.started_at,j.finished_at,j.retry_policy,
               p.outcome_status,p.is_paid,q.queue_sequence,
               COALESCE(control.state,'active') AS control_state,
               control.requested_at AS pause_requested_at,
               control.paused_at,
               CASE WHEN j.status='queued' THEN (
                 SELECT COUNT(*)
                 FROM job_queue_entries AS q2
                 JOIN jobs AS j2 ON j2.job_id=q2.job_id
                 WHERE j2.status='queued' AND j2.cancel_requested=0
                   AND q2.queue_sequence<=q.queue_sequence
               ) ELSE NULL END AS queue_position
        FROM jobs AS j
        LEFT JOIN job_payloads AS p ON p.job_id=j.job_id
        LEFT JOIN job_queue_entries AS q ON q.job_id=j.job_id
        LEFT JOIN job_controls AS control ON control.job_id=j.job_id
        WHERE j.job_id=?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["public_status"] = "paused" if result["control_state"] == "paused" else result["status"]
    result["pause_requested"] = result["control_state"] == "pause_requested"
    return result


def list_jobs(db, *, task_id: str | None = None, limit: int = 100) -> list[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit 必须在 1 到 500 之间")
    sql = (
        "SELECT j.job_id,j.job_type,j.task_id,j.library_id,j.status,j.cursor,"
        "j.progress_current,j.progress_total,j.phase_label,j.cancel_requested,"
        "j.cancel_reason,j.error_summary,j.created_at,j.updated_at,j.started_at,"
        "j.finished_at,j.retry_policy,p.outcome_status,p.is_paid,q.queue_sequence,"
        "COALESCE(control.state,'active') AS control_state,control.requested_at AS pause_requested_at,control.paused_at,"
        "CASE WHEN j.status='queued' THEN (SELECT COUNT(*) "
        "FROM job_queue_entries AS q2 JOIN jobs AS j2 ON j2.job_id=q2.job_id "
        "WHERE j2.status='queued' AND j2.cancel_requested=0 "
        "AND q2.queue_sequence<=q.queue_sequence) ELSE NULL END AS queue_position "
        "FROM jobs AS j LEFT JOIN job_payloads AS p ON p.job_id=j.job_id "
        "LEFT JOIN job_queue_entries AS q ON q.job_id=j.job_id "
        "LEFT JOIN job_controls AS control ON control.job_id=j.job_id"
    )
    params: list[object] = []
    if task_id is not None:
        sql += " WHERE j.task_id=?"
        params.append(task_id)
    sql += " ORDER BY j.updated_at DESC,j.created_at DESC,j.rowid DESC LIMIT ?"
    params.append(limit)
    result = []
    for row in db.connection.execute(sql, params).fetchall():
        item = dict(row)
        item["public_status"] = "paused" if item["control_state"] == "paused" else item["status"]
        item["pause_requested"] = item["control_state"] == "pause_requested"
        result.append(item)
    return result


def _finish_attempt(
    connection,
    job_id: str,
    worker_id: str,
    status: str,
    error_summary: str | None,
) -> None:
    changed = connection.execute(
        """
        UPDATE job_attempts
        SET status=?, error_summary=?, finished_at=datetime('now')
        WHERE job_id=? AND worker_id=? AND status='running'
        """,
        (status, error_summary, job_id, worker_id),
    ).rowcount
    if changed != 1:
        raise RuntimeError(f"任务执行记录不存在：{job_id}")


def _persist_action_result(db, job_id: str, worker_id: str, value) -> None:
    """Persist the stable, public progress envelope returned by workbench actions."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise TypeError("动作处理器必须返回字典或 None")
    cursor = value.get("cursor")
    progress = value.get("progress")
    phase_label = value.get("phase_label")
    result = value.get("result")
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("动作结果缺少稳定 cursor")
    if not isinstance(phase_label, str) or not phase_label:
        raise ValueError("动作结果缺少阶段名称")
    if not isinstance(progress, dict) or not isinstance(result, dict):
        raise ValueError("动作结果缺少 progress 或 result")
    current = progress.get("current")
    total = progress.get("total")
    if (
        not isinstance(current, int)
        or isinstance(current, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or current < 0
        or total < 0
        or current > total
    ):
        raise ValueError("动作 progress 必须满足 0 <= current <= total")
    with db.transaction(immediate=True) as connection:
        changed = connection.execute(
            """
            UPDATE jobs
            SET cursor=?,progress_current=?,progress_total=?,phase_label=?,
                updated_at=datetime('now')
            WHERE job_id=? AND status='running' AND worker_id=?
            """,
            (cursor, current, total, phase_label, job_id, worker_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"任务当前不可更新动作结果：{job_id}")


def _complete_job(db, job_id: str, *, worker_id: str) -> None:
    with db.transaction(immediate=True) as connection:
        changed = connection.execute(
            """
            UPDATE jobs
            SET status='completed', worker_id=NULL, lease_expires_at=NULL,
                finished_at=datetime('now'), updated_at=datetime('now')
            WHERE job_id=? AND status='running' AND worker_id=?
            """,
            (job_id, worker_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"任务当前不可完成：{job_id}")
        connection.execute(
            """
            UPDATE job_payloads
            SET outcome_status=CASE WHEN is_paid=1 THEN 'confirmed'
                                    ELSE outcome_status END,
                provider_state=CASE WHEN is_paid=1 THEN 'completed'
                                    ELSE provider_state END,
                cost_status=CASE
                  WHEN is_paid=1 AND requested_cost_status IN (
                    'estimated', 'approved', 'incurred'
                  ) THEN 'incurred'
                  WHEN is_paid=1 THEN requested_cost_status
                  ELSE cost_status
                END,
                outcome_reason=NULL,
                updated_at=datetime('now')
            WHERE job_id=?
            """,
            (job_id,),
        )
        _finish_attempt(connection, job_id, worker_id, "completed", None)


class DurableCliJob:
    """Synchronous CLI compatibility backed by the durable global queue.

    The browser workbench uses :class:`WorkbenchWorker`; existing catalog CLI
    commands retain their blocking interface but may only start when their own
    request is at the FIFO head. Progress callbacks renew a longer CLI lease.
    """

    def __init__(
        self,
        db,
        job_id: str,
        job_type: str,
        library_id: str | None = None,
    ) -> None:
        if job_type not in JobManager.HEAVY_TYPES:
            raise ValueError(f"不是重任务类型：{job_type}")
        self.db = db
        self.base_job_id = job_id
        self.job_id = job_id
        self.job_type = job_type
        self.library_id = library_id
        self.worker_id = f"cli-{os.getpid()}-{uuid.uuid4().hex}"
        self.lease_seconds = 300

    def recover_interrupted(self, max_age_seconds: int) -> int:
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds 不能小于 0")
        self.lease_seconds = max(DEFAULT_LEASE_SECONDS, max_age_seconds)
        return recover_interrupted_jobs(self.db)

    def _latest_open_job_id(self) -> str | None:
        rows = self.db.connection.execute(
            """
            SELECT j.job_id, j.status, p.payload_json
            FROM jobs AS j
            JOIN job_payloads AS p ON p.job_id=j.job_id
            WHERE j.job_type=? AND j.task_id IS NULL AND j.library_id IS ?
              AND j.status IN ('queued', 'running', 'failed', 'interrupted')
            ORDER BY j.updated_at DESC, j.created_at DESC, j.rowid DESC
            """,
            (self.job_type, self.library_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if (
                isinstance(payload, dict)
                and payload.get("entrypoint") == "media_catalog_cli"
                and payload.get("operation_key") == self.base_job_id
            ):
                return row["job_id"]
        return None

    def start(self) -> None:
        target_job_id = self._latest_open_job_id()
        if target_job_id is None:
            existing = self.db.connection.execute(
                "SELECT 1 FROM jobs WHERE job_id=?", (self.base_job_id,)
            ).fetchone()
            target_job_id = (
                self.base_job_id
                if existing is None
                else f"{self.base_job_id}:{uuid.uuid4().hex}"
            )
            enqueue_job(
                self.db,
                job_id=target_job_id,
                job_type=self.job_type,
                library_id=self.library_id,
                payload={
                    "entrypoint": "media_catalog_cli",
                    "library_id": self.library_id,
                    "operation_key": self.base_job_id,
                },
            )
        else:
            status = self.db.connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (target_job_id,)
            ).fetchone()[0]
            if status in {"failed", "interrupted"}:
                resume_job(self.db, target_job_id)

        claimed = claim_next_heavy_job(
            self.db,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            expected_job_id=target_job_id,
        )
        if claimed is None:
            raise HeavyJobBusy(
                "重任务已进入持久队列；请等待队首任务完成后重试。"
            )
        self.job_id = target_job_id

    def heartbeat(self, cursor: str | None = None) -> None:
        renew_job_lease(
            self.db,
            self.job_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        with self.db.transaction(immediate=True) as connection:
            changed = connection.execute(
                """
                UPDATE jobs SET cursor=?, updated_at=datetime('now')
                WHERE job_id=? AND status='running' AND worker_id=?
                """,
                (cursor, self.job_id, self.worker_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"任务当前不可更新进度：{self.job_id}")

    def resume_cursor(self) -> str | None:
        row = self.db.connection.execute(
            "SELECT cursor FROM jobs WHERE job_id=?", (self.job_id,)
        ).fetchone()
        return None if row is None else row["cursor"]

    def complete(self) -> None:
        _complete_job(self.db, self.job_id, worker_id=self.worker_id)

    def fail(self, error: BaseException | str) -> None:
        fail_job(self.db, self.job_id, worker_id=self.worker_id, error=error)

    def interrupt(self, error: BaseException | str = "任务被中断") -> None:
        interrupt_job(
            self.db, self.job_id, worker_id=self.worker_id, error=error
        )


class WorkbenchWorker:
    def __init__(
        self,
        db,
        *,
        worker_id: str,
        handlers: Mapping[str, Callable[[ClaimedJob], dict | None]],
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_interval: float | None = None,
    ) -> None:
        self.db = db
        self.worker_id = worker_id
        self.handlers = dict(handlers)
        self.clock = clock
        self.lease_seconds = lease_seconds
        interval = (
            min(float(lease_seconds) / 3.0, 5.0)
            if heartbeat_interval is None
            else heartbeat_interval
        )
        if (
            not isinstance(interval, (int, float))
            or isinstance(interval, bool)
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("heartbeat_interval 必须是有限正数")
        self.heartbeat_interval = float(interval)

    def heartbeat(self, job_id: str) -> None:
        renew_job_lease(
            self.db,
            job_id,
            worker_id=self.worker_id,
            clock=self.clock,
            lease_seconds=self.lease_seconds,
        )

    def _keep_lease_alive(
        self,
        job_id: str,
        stop_event: threading.Event,
        failures: list[BaseException],
    ) -> None:
        """Renew a long-running handler lease on a dedicated DB connection."""
        try:
            with CatalogDatabase.open(self.db.path) as heartbeat_db:
                heartbeat_db.connection.execute("PRAGMA busy_timeout=1000")
                while not stop_event.wait(self.heartbeat_interval):
                    renew_job_lease(
                        heartbeat_db,
                        job_id,
                        worker_id=self.worker_id,
                        clock=self.clock,
                        lease_seconds=self.lease_seconds,
                    )
        except BaseException as error:
            failures.append(error)

    def run_next(self) -> ClaimedJob | None:
        recover_interrupted_jobs(
            self.db, worker_id=self.worker_id, clock=self.clock
        )
        job = claim_next_heavy_job(
            self.db,
            worker_id=self.worker_id,
            clock=self.clock,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return None
        if cancel_at_safe_point(
            self.db, job.job_id, worker_id=self.worker_id
        ):
            return job
        handler = self.handlers.get(job.job_type)
        if handler is None:
            fail_job(
                self.db,
                job.job_id,
                worker_id=self.worker_id,
                error=f"没有可执行 {job.job_type} 的处理器",
            )
            return job
        heartbeat_stop = threading.Event()
        heartbeat_failures: list[BaseException] = []
        heartbeat_thread = threading.Thread(
            target=self._keep_lease_alive,
            args=(job.job_id, heartbeat_stop, heartbeat_failures),
            name=f"workbench-heartbeat-{job.job_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        handler_error: BaseException | None = None
        handler_result = None
        try:
            handler_result = handler(job)
        except BaseException as error:
            handler_error = error
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(
                timeout=max(1.0, self.heartbeat_interval * 2.0)
            )
        if heartbeat_thread.is_alive():
            heartbeat_failures.append(
                RuntimeError("心跳续租线程未能及时停止")
            )
        if handler_error is None and heartbeat_failures:
            detail = bounded_error(heartbeat_failures[0])
            if job.is_paid:
                handler_error = ProviderOutcomeUnknown(
                    f"任务心跳续租失败，供应商结果需人工核对：{detail}",
                    provider_state="lease_heartbeat_failed",
                )
            else:
                handler_error = JobInterrupted(
                    f"任务心跳续租失败，可安全恢复：{detail}"
                )

        if isinstance(handler_error, ProviderOutcomeUnknown):
            mark_provider_outcome_unknown(
                self.db,
                job.job_id,
                worker_id=self.worker_id,
                provider_state=handler_error.provider_state,
                reason=handler_error,
            )
        elif isinstance(handler_error, JobInterrupted):
            interrupt_job(
                self.db,
                job.job_id,
                worker_id=self.worker_id,
                error=handler_error,
            )
        elif handler_error is not None:
            fail_job(
                self.db,
                job.job_id,
                worker_id=self.worker_id,
                error=handler_error,
            )
        else:
            if not cancel_at_safe_point(
                self.db, job.job_id, worker_id=self.worker_id
            ):
                try:
                    _persist_action_result(
                        self.db, job.job_id, self.worker_id, handler_result
                    )
                    if not pause_at_safe_point(
                        self.db, job.job_id, worker_id=self.worker_id
                    ):
                        _complete_job(
                            self.db, job.job_id, worker_id=self.worker_id
                        )
                except BaseException as error:
                    fail_job(
                        self.db,
                        job.job_id,
                        worker_id=self.worker_id,
                        error=error,
                    )
        return job

    def run_once(self) -> ClaimedJob | None:
        return self.run_next()

    def run_forever(
        self,
        stop_event,
        *,
        poll_interval: float = 0.25,
        poll: Callable[[float], None] | None = None,
    ) -> None:
        wait = poll or stop_event.wait
        while not stop_event.is_set():
            if self.run_next() is None:
                wait(poll_interval)
