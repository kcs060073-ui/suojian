from datetime import datetime, timedelta, timezone

from media_catalog.full_review import (
    approve_after_full_watch,
    complete_watch,
    record_watch_tick,
    start_watch,
)


def complete_watch_session(db, task_id, snapshot_id, qa_report_id):
    now = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
    session = start_watch(
        db, task_id, snapshot_id, qa_report_id, clock=lambda: now
    )
    position = 0.0
    while position < session["duration_seconds"]:
        next_position = min(position + 4.0, session["duration_seconds"])
        now += timedelta(seconds=next_position - position)
        position = next_position
        record_watch_tick(
            db, session["session_id"], position, clock=lambda: now
        )
    complete_watch(db, session["session_id"])
    return session["session_id"]


def approve_review(
    db,
    task_id,
    snapshot_id,
    qa_report_id,
    reviewed_by,
    notes=None,
):
    session_id = complete_watch_session(
        db, task_id, snapshot_id, qa_report_id
    )
    return approve_after_full_watch(
        db,
        task_id,
        snapshot_id,
        qa_report_id,
        session_id,
        reviewed_by,
        notes,
    )
