"""Human review decisions bound to a trusted render/QA candidate."""

import uuid

from .candidate_versions import resolve_candidate_media
from .snapshots import latest_review_candidate


_VALID_DECISIONS = ("approved", "rejected")


def _run_hook(name: str) -> None:
    hook = globals().get(name)
    if hook is not None:
        hook()


def _validate_candidate(db, task_id: str, snapshot_id: str, qa_report_id: str,
                        *, require_current_authorization: bool = True) -> dict:
    candidate = latest_review_candidate(db, task_id)
    if (
        candidate is None
        or candidate["qa_snapshot_id"] != snapshot_id
        or candidate["qa_report_id"] != qa_report_id
        or candidate.get("producer") not in ("assemble-v1", "assemble-ad-v1")
    ):
        raise RuntimeError("UNTRUSTED_REVIEW_CANDIDATE: 只能记录 assemble-v1 可信候选")

    try:
        resolve_candidate_media(db, task_id, candidate["candidate_id"])
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            "UNTRUSTED_REVIEW_CANDIDATE: 可信候选成片不存在或无法读取"
        ) from error
    if candidate.get("qa_output_signature") != candidate.get("output_signature"):
        raise RuntimeError("UNTRUSTED_REVIEW_CANDIDATE: 可信候选成片内容已变化")
    # Rights records are historical metadata, not a review or delivery gate.
    return candidate


def insert_review_decision(
    connection,
    db,
    task_id: str,
    snapshot_id: str,
    qa_report_id: str,
    decision: str,
    reviewed_by: str,
    notes: str | None = None,
    watch_session_id: str | None = None,
) -> dict:
    """Insert one decision inside an already-held write transaction."""
    if not connection.in_transaction:
        raise RuntimeError(
            "REVIEW_TRANSACTION_REQUIRED: 审片决策必须在调用方事务内写入"
        )
    if decision not in _VALID_DECISIONS:
        raise ValueError("INVALID_REVIEW_DECISION: decision 必须是 approved 或 rejected")

    candidate = _validate_candidate(
        db, task_id, snapshot_id, qa_report_id,
        require_current_authorization=decision == "approved",
    )
    if decision == "rejected" and (
        not isinstance(notes, str) or not notes.strip()
    ):
        raise ValueError("REJECTION_NOTES_REQUIRED: 驳回时必须说明问题")
    if decision == "approved":
        from .full_review import validate_completed_watch

        if not watch_session_id:
            raise RuntimeError(
                "FULL_WATCH_REQUIRED: 批准前必须完整观看当前候选成片"
            )
        validate_completed_watch(
            db, watch_session_id, task_id, snapshot_id, qa_report_id
        )
    _run_hook("_before_review_insert_hook")
    candidate = _validate_candidate(
        db, task_id, snapshot_id, qa_report_id,
        require_current_authorization=decision == "approved",
    )
    if decision == "approved":
        open_issue = connection.execute(
            """SELECT issue_id FROM review_issues
            WHERE task_id=? AND candidate_id=? AND status='open' LIMIT 1""",
            (task_id, candidate["candidate_id"]),
        ).fetchone()
        if open_issue is not None:
            raise RuntimeError("REVIEW_ISSUES_OPEN: 请先处理或撤回本版审片问题")
        from .content_review import require_content_review_clearance
        require_content_review_clearance(db, task_id, candidate["candidate_id"])

    approval_id = f"approval-{uuid.uuid4().hex[:16]}"
    connection.execute(
        """
        INSERT INTO review_approvals(
          approval_id,task_id,snapshot_id,qa_report_id,decision,reviewed_by,
          notes,watch_session_id
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            approval_id, task_id, snapshot_id, qa_report_id, decision,
            reviewed_by, notes, watch_session_id,
        ),
    )
    return dict(connection.execute(
        "SELECT * FROM review_approvals WHERE approval_id=?", (approval_id,)
    ).fetchone())


def record_review(
    db,
    task_id: str,
    snapshot_id: str,
    qa_report_id: str,
    decision: str,
    reviewed_by: str,
    notes: str | None = None,
    watch_session_id: str | None = None,
) -> dict:
    """Record one immutable human decision for the exact current trusted candidate."""
    if db.connection.in_transaction:
        raise RuntimeError(
            "ACTIVE_TRANSACTION_UNSUPPORTED: 审片决策不支持调用方持有活动事务"
        )
    if decision not in _VALID_DECISIONS:
        raise ValueError("INVALID_REVIEW_DECISION: decision 必须是 approved 或 rejected")

    _run_hook("_before_review_lock_hook")
    connection = db.connection
    transaction_started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        row = insert_review_decision(
            connection,
            db,
            task_id,
            snapshot_id,
            qa_report_id,
            decision,
            reviewed_by,
            notes,
            watch_session_id,
        )
        connection.commit()
        return row
    except BaseException:
        if transaction_started:
            connection.rollback()
        raise


def approve_review(db, task_id: str, snapshot_id: str, qa_report_id: str,
                   reviewed_by: str, notes: str | None = None,
                   *, watch_session_id: str | None = None) -> dict:
    return record_review(
        db, task_id, snapshot_id, qa_report_id, "approved", reviewed_by, notes,
        watch_session_id,
    )


def reject_review(db, task_id: str, snapshot_id: str, qa_report_id: str,
                  reviewed_by: str, notes: str | None = None) -> dict:
    return record_review(
        db, task_id, snapshot_id, qa_report_id, "rejected", reviewed_by, notes,
    )


def matching_approval(db, task_id: str, snapshot_id: str, qa_report_id: str):
    return db.connection.execute(
        """
        SELECT a.* FROM review_approvals AS a
        JOIN candidate_versions AS c
          ON c.task_id=a.task_id AND c.qa_snapshot_id=a.snapshot_id
         AND c.qa_report_id=a.qa_report_id
        LEFT JOIN review_watch_sessions AS w
          ON w.session_id=a.watch_session_id
        WHERE a.task_id=? AND a.snapshot_id=? AND a.qa_report_id=?
          AND (a.decision='rejected' OR (
            a.decision='approved' AND w.status='completed'
            AND w.task_id=a.task_id AND w.snapshot_id=a.snapshot_id
            AND w.qa_report_id=a.qa_report_id
            AND NOT EXISTS(
              SELECT 1 FROM review_issues AS i
              WHERE i.task_id=c.task_id AND i.candidate_id=c.candidate_id
                AND i.status='open'
            )
          ))
          AND c.status IN ('awaiting_review','approved','published')
          AND NOT EXISTS(
            SELECT 1 FROM rework_requests AS r
            WHERE r.task_id=c.task_id AND r.source_candidate_id=c.candidate_id
              AND r.status='applied' AND r.result_candidate_id IS NULL
          )
        ORDER BY a.rowid DESC LIMIT 1
        """, (task_id, snapshot_id, qa_report_id),
    ).fetchone()
