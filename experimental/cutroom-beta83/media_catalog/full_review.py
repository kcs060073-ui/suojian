"""Server-observed full-watch gate for one trusted render/QA candidate."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Callable
import uuid

from .reviews import _validate_candidate


COVERAGE_THRESHOLD = 0.98
PLAYBACK_RATE_LIMIT = 1.25
TICK_JITTER_SECONDS = 0.5
MAX_CONTIGUOUS_TICK_SECONDS = 5.0
END_TOLERANCE_SECONDS = 0.5


def _now(clock: Callable[[], datetime] | None = None) -> datetime:
    value = datetime.now(timezone.utc) if clock is None else clock()
    if not isinstance(value, datetime):
        raise TypeError("clock 必须返回 datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration_for_candidate(db, candidate: dict) -> float:
    row = db.connection.execute(
        "SELECT report_json FROM qa_reports WHERE report_id=?",
        (candidate["qa_report_id"],),
    ).fetchone()
    try:
        duration = float(json.loads(row["report_json"])["video_spec"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("FULL_WATCH_DURATION_UNKNOWN: 无法确认候选成片时长") from error
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("FULL_WATCH_DURATION_UNKNOWN: 无法确认候选成片时长")
    return duration


def _signature_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _session_result(row) -> dict:
    result = dict(row)
    duration = float(result["duration_seconds"])
    covered = min(float(result["covered_seconds"]), duration)
    result["coverage_ratio"] = covered / duration
    result["can_approve"] = bool(
        result["status"] == "completed"
        and result["reached_end"]
        and result["coverage_ratio"] >= COVERAGE_THRESHOLD
    )
    return result


def start_watch(
    db,
    task_id: str,
    snapshot_id: str,
    qa_report_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict:
    now = _stamp(_now(clock))
    session_id = f"watch-{uuid.uuid4().hex[:20]}"
    with db.transaction(immediate=True) as connection:
        candidate = _validate_candidate(
            db, task_id, snapshot_id, qa_report_id
        )
        duration = _duration_for_candidate(db, candidate)
        connection.execute(
            """
            INSERT INTO review_watch_sessions(
              session_id, task_id, snapshot_id, qa_report_id,
              output_signature, duration_seconds, started_at, last_tick_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                task_id,
                snapshot_id,
                qa_report_id,
                _signature_text(candidate["output_signature"]),
                duration,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM review_watch_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
    return _session_result(row)


def _merge_interval(connection, session_id: str, start: float, end: float) -> float:
    intervals = [
        (float(row["start_seconds"]), float(row["end_seconds"]))
        for row in connection.execute(
            """
            SELECT start_seconds, end_seconds FROM review_watch_intervals
            WHERE session_id=? ORDER BY start_seconds, end_seconds
            """,
            (session_id,),
        )
    ]
    intervals.append((start, end))
    intervals.sort()
    merged: list[list[float]] = []
    for current_start, current_end in intervals:
        # Never credit a positive gap: even sub-millisecond rejected seeks can
        # otherwise accumulate coverage without spending playback credit.
        if not merged or current_start > merged[-1][1]:
            merged.append([current_start, current_end])
        else:
            merged[-1][1] = max(merged[-1][1], current_end)
    connection.execute(
        "DELETE FROM review_watch_intervals WHERE session_id=?", (session_id,)
    )
    connection.executemany(
        """
        INSERT INTO review_watch_intervals(
          interval_id, session_id, start_seconds, end_seconds
        ) VALUES(?, ?, ?, ?)
        """,
        [
            (f"interval-{uuid.uuid4().hex[:20]}", session_id, item[0], item[1])
            for item in merged
        ],
    )
    return sum(item[1] - item[0] for item in merged)


def record_watch_tick(
    db,
    session_id: str,
    position_seconds: float,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict:
    if isinstance(position_seconds, bool) or not isinstance(position_seconds, (int, float)):
        raise ValueError("INVALID_WATCH_POSITION: 播放位置必须是有效秒数")
    position = float(position_seconds)
    if not math.isfinite(position) or position < 0:
        raise ValueError("INVALID_WATCH_POSITION: 播放位置必须是有效秒数")
    now_value = _now(clock)
    now = _stamp(now_value)
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM review_watch_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"WATCH_SESSION_NOT_FOUND: {session_id}")
        if row["status"] != "active":
            raise RuntimeError("WATCH_SESSION_COMPLETED: 观看会话已经完成")
        candidate = _validate_candidate(
            db, row["task_id"], row["snapshot_id"], row["qa_report_id"]
        )
        if _signature_text(candidate["output_signature"]) != row["output_signature"]:
            raise RuntimeError("WATCH_CANDIDATE_CHANGED: 候选成片已变化")
        duration = float(row["duration_seconds"])
        if position > duration + END_TOLERANCE_SECONDS:
            raise ValueError("INVALID_WATCH_POSITION: 播放位置超过视频时长")
        position = min(position, duration)
        previous = float(row["last_position_seconds"])
        elapsed = max(
            0.0, (now_value - _parse_stamp(row["last_tick_at"])).total_seconds()
        )
        delta = position - previous
        allowed = min(
            elapsed * PLAYBACK_RATE_LIMIT + float(row["playback_credit_seconds"]),
            MAX_CONTIGUOUS_TICK_SECONDS,
        )
        accepted = delta > 0 and delta <= allowed
        # Carry at most one jitter allowance across requests. Pauses/seek jumps
        # cannot bank wall-clock time, and every accepted advance spends credit.
        credit = min(TICK_JITTER_SECONDS, max(0.0, allowed - (delta if accepted else 0)))
        covered = float(row["covered_seconds"])
        reached_end = bool(row["reached_end"])
        if accepted:
            covered = _merge_interval(connection, session_id, previous, position)
            reached_end = reached_end or position >= duration - END_TOLERANCE_SECONDS
        connection.execute(
            """
            UPDATE review_watch_sessions
            SET covered_seconds=?, last_position_seconds=?, reached_end=?,
                last_tick_at=?, playback_credit_seconds=?, updated_at=datetime('now')
            WHERE session_id=?
            """,
            (covered, position, int(reached_end), now, credit, session_id),
        )
        updated = connection.execute(
            "SELECT * FROM review_watch_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
    result = _session_result(updated)
    result["tick_accepted"] = accepted
    return result


def validate_completed_watch(
    db, session_id: str, task_id: str, snapshot_id: str, qa_report_id: str
) -> dict:
    row = db.connection.execute(
        "SELECT * FROM review_watch_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("FULL_WATCH_REQUIRED: 没有找到完整观看记录")
    candidate = _validate_candidate(db, task_id, snapshot_id, qa_report_id)
    result = _session_result(row)
    if (
        row["task_id"] != task_id
        or row["snapshot_id"] != snapshot_id
        or row["qa_report_id"] != qa_report_id
        or row["output_signature"] != _signature_text(candidate["output_signature"])
        or not result["can_approve"]
    ):
        raise RuntimeError("FULL_WATCH_REQUIRED: 必须完整观看当前候选成片")
    return result


def get_watch_session(db, session_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM review_watch_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    return None if row is None else _session_result(row)


def latest_watch_session(
    db,
    task_id: str,
    snapshot_id: str | None = None,
    qa_report_id: str | None = None,
) -> dict | None:
    clauses = ["task_id=?"]
    params: list[str] = [task_id]
    if snapshot_id is not None:
        clauses.append("snapshot_id=?")
        params.append(snapshot_id)
    if qa_report_id is not None:
        clauses.append("qa_report_id=?")
        params.append(qa_report_id)
    row = db.connection.execute(
        "SELECT * FROM review_watch_sessions WHERE " + " AND ".join(clauses)
        + " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return None if row is None else _session_result(row)


def complete_watch(db, session_id: str) -> dict:
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM review_watch_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"WATCH_SESSION_NOT_FOUND: {session_id}")
        candidate = _validate_candidate(
            db, row["task_id"], row["snapshot_id"], row["qa_report_id"]
        )
        result = _session_result(row)
        if (
            row["output_signature"] != _signature_text(candidate["output_signature"])
            or not row["reached_end"]
            or result["coverage_ratio"] < COVERAGE_THRESHOLD
        ):
            raise RuntimeError("FULL_WATCH_REQUIRED: 覆盖不足或尚未观看到片尾")
        connection.execute(
            """
            UPDATE review_watch_sessions
            SET status='completed', completed_at=datetime('now'),
                updated_at=datetime('now')
            WHERE session_id=?
            """,
            (session_id,),
        )
        completed = connection.execute(
            "SELECT * FROM review_watch_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
    return _session_result(completed)


def approve_after_full_watch(
    db,
    task_id: str,
    snapshot_id: str,
    qa_report_id: str,
    watch_session_id: str,
    reviewed_by: str,
    notes: str | None = None,
) -> dict:
    from .reviews import approve_review

    return approve_review(
        db,
        task_id,
        snapshot_id,
        qa_report_id,
        reviewed_by,
        notes,
        watch_session_id=watch_session_id,
    )
