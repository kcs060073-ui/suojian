"""Categorized review rework with local, transaction-safe workflow rollback."""

from __future__ import annotations

import json
import uuid

from .candidate_versions import get_candidate
from .reviews import insert_review_decision
from .workflow import WORKFLOW_STAGES, rollback_to_stage_connection


REWORK_BOUNDARIES = {
    "script": ("brief_lock", True),
    "material": ("ai_storyboard", True),
    "rhythm": ("rough_cut", False),
    "subtitle": ("packaging_confirm", False),
    "audio": ("packaging_confirm", False),
    "packaging": ("packaging_confirm", False),
    "technical": ("fine_render", False),
}


def preview_rework_impact(
    category: str,
    *,
    technical_scope: str | None = None,
) -> dict:
    if category not in REWORK_BOUNDARIES:
        raise ValueError("INVALID_REWORK_CATEGORY: 未知返工分类")
    target, may_cost = REWORK_BOUNDARIES[category]
    if category == "technical" and technical_scope == "qa_only":
        target = "auto_qa"
    elif technical_scope is not None and category != "technical":
        raise ValueError("INVALID_TECHNICAL_SCOPE: 仅技术返工可指定 technical_scope")
    elif category == "technical" and technical_scope not in (None, "qa_only"):
        raise ValueError("INVALID_TECHNICAL_SCOPE: technical_scope 必须是 qa_only")
    index = WORKFLOW_STAGES.index(target)
    return {
        "category": category,
        "technical_scope": technical_scope,
        "target_stage": target,
        "preserved_stages": list(WORKFLOW_STAGES[:index]),
        "invalidated_stages": list(WORKFLOW_STAGES[index + 1:]),
        "rerun_stages": list(WORKFLOW_STAGES[index:]),
        "cost_confirmation_required": may_cost,
    }


def _decoded(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _public_rework(row) -> dict:
    result = dict(row)
    result["impact"] = _decoded(result.pop("impact_json", None))
    return result


def get_rework_request(db, task_id: str, rework_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM rework_requests WHERE task_id=? AND rework_id=?",
        (task_id, rework_id),
    ).fetchone()
    return None if row is None else _public_rework(row)


def list_rework_requests(db, task_id: str) -> list[dict]:
    rows = db.connection.execute(
        """SELECT * FROM rework_requests
        WHERE task_id=? ORDER BY created_at DESC, rowid DESC""",
        (task_id,),
    ).fetchall()
    return [_public_rework(row) for row in rows]


def _review_for_rework(connection, row) -> dict:
    approval_id = _decoded(row["impact_json"]).get("review_approval_id")
    review = connection.execute(
        "SELECT * FROM review_approvals WHERE approval_id=? AND task_id=?",
        (approval_id, row["task_id"]),
    ).fetchone()
    if review is None:
        raise RuntimeError("REWORK_REVIEW_MISSING: 返工单缺少原子驳回记录")
    return dict(review)


def reject_with_rework(
    db,
    task_id: str,
    candidate_id: str,
    *,
    reviewed_by: str,
    category: str,
    notes: str,
    position_seconds: float | None = None,
    qa_issue_ref: str | None = None,
    idempotency_key: str,
    technical_scope: str | None = None,
) -> dict:
    """Atomically reject the current candidate and create one rework request."""
    if db.connection.in_transaction:
        raise RuntimeError(
            "ACTIVE_TRANSACTION_UNSUPPORTED: 返工驳回不支持调用方持有活动事务"
        )
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise ValueError("REVIEWER_REQUIRED: reviewed_by 不能为空")
    if not isinstance(notes, str) or not notes.strip():
        raise ValueError("REJECTION_NOTES_REQUIRED: 驳回时必须说明问题")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED: idempotency_key 不能为空")
    if position_seconds is not None and position_seconds < 0:
        raise ValueError("INVALID_REWORK_POSITION: position_seconds 不能小于 0")
    impact = preview_rework_impact(category, technical_scope=technical_scope)

    connection = db.connection
    started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        started = True
        existing = connection.execute(
            "SELECT * FROM rework_requests WHERE task_id=? AND idempotency_key=?",
            (task_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if (
                existing["source_candidate_id"] != candidate_id
                or existing["category"] != category
                or existing["notes"] != notes.strip()
            ):
                raise RuntimeError(
                    "REWORK_IDEMPOTENCY_CONFLICT: 幂等键已用于不同返工请求"
                )
            result = {
                "review": _review_for_rework(connection, existing),
                "rework": _public_rework(existing),
            }
            connection.commit()
            return result

        candidate = get_candidate(db, task_id, candidate_id)
        current = connection.execute(
            """SELECT candidate_id FROM candidate_versions
            WHERE task_id=? ORDER BY version_no DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if (
            candidate is None
            or current is None
            or current["candidate_id"] != candidate_id
            or not candidate.get("qa_report_id")
        ):
            raise RuntimeError(
                "UNTRUSTED_REWORK_CANDIDATE: 只能驳回当前已通过 QA 的候选"
            )
        if candidate.get("qa_snapshot_id"):
            review = insert_review_decision(
                connection,
                db,
                task_id,
                candidate["qa_snapshot_id"],
                candidate["qa_report_id"],
                "rejected",
                reviewed_by.strip(),
                notes.strip(),
            )
        elif candidate.get("status") == "qa_blocked":
            trusted = connection.execute(
                """SELECT 1 FROM render_snapshots AS s
                JOIN qa_reports AS q ON q.report_id=? AND q.task_id=s.task_id
                WHERE s.snapshot_id=? AND s.task_id=? AND s.stage='mix'
                  AND s.producer IN ('assemble-v1','assemble-ad-v1')
                  AND q.status!='passed'""",
                (candidate["qa_report_id"], candidate["mix_snapshot_id"], task_id),
            ).fetchone()
            if trusted is None:
                raise RuntimeError(
                    "UNTRUSTED_REWORK_CANDIDATE: QA 阻断候选缺少可信渲染或报告"
                )
            approval_id = f"approval-{uuid.uuid4().hex[:16]}"
            connection.execute(
                """INSERT INTO review_approvals(
                  approval_id,task_id,snapshot_id,qa_report_id,decision,reviewed_by,notes
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    approval_id, task_id, candidate["mix_snapshot_id"],
                    candidate["qa_report_id"], "rejected", reviewed_by.strip(),
                    notes.strip(),
                ),
            )
            review = dict(connection.execute(
                "SELECT * FROM review_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone())
        else:
            raise RuntimeError(
                "UNTRUSTED_REWORK_CANDIDATE: 只能驳回当前可信候选"
            )
        impact["review_approval_id"] = review["approval_id"]
        rework_id = f"rework-{uuid.uuid4().hex[:16]}"
        cost_status = (
            "confirmation_required"
            if impact["cost_confirmation_required"]
            else "none"
        )
        connection.execute(
            """INSERT INTO rework_requests(
              rework_id,task_id,source_candidate_id,category,notes,
              position_seconds,qa_issue_ref,target_stage,impact_json,cost_status,
              status,idempotency_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rework_id,
                task_id,
                candidate_id,
                category,
                notes.strip(),
                position_seconds,
                qa_issue_ref,
                impact["target_stage"],
                json.dumps(impact, ensure_ascii=False, sort_keys=True),
                cost_status,
                "pending_confirmation",
                idempotency_key.strip(),
            ),
        )
        row = connection.execute(
            "SELECT * FROM rework_requests WHERE rework_id=?", (rework_id,)
        ).fetchone()
        connection.commit()
        return {"review": review, "rework": _public_rework(row)}
    except BaseException:
        if started:
            connection.rollback()
        raise


def approve_rework_cost(db, task_id: str, rework_id: str) -> dict:
    """Explicitly approve a cost-bearing rework before workflow rollback."""
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM rework_requests WHERE task_id=? AND rework_id=?",
            (task_id, rework_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"REWORK_NOT_FOUND: {rework_id}")
        if row["cost_status"] == "confirmation_required":
            connection.execute(
                """UPDATE rework_requests SET cost_status='approved',
                updated_at=datetime('now') WHERE rework_id=?""",
                (rework_id,),
            )

        current = connection.execute(
            """SELECT candidate_id FROM candidate_versions
            WHERE task_id=? ORDER BY version_no DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if current is None or current["candidate_id"] != row["source_candidate_id"]:
            raise RuntimeError(
                "REWORK_SOURCE_STALE: 返工单来源已不是当前候选，请重新记录"
            )
        updated = connection.execute(
            "SELECT * FROM rework_requests WHERE rework_id=?", (rework_id,)
        ).fetchone()
        return _public_rework(updated)


def execute_rework(
    db,
    task_id: str,
    rework_id: str,
    *,
    idempotency_key: str,
) -> dict:
    """Apply only the local stage rollback; never render or call providers."""
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED: idempotency_key 不能为空")
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM rework_requests WHERE task_id=? AND rework_id=?",
            (task_id, rework_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"REWORK_NOT_FOUND: {rework_id}")
        if row["status"] == "applied":
            if row["execution_key"] == idempotency_key:
                return _public_rework(row)
            raise RuntimeError(
                "REWORK_ALREADY_APPLIED: 返工已由不同执行键应用"
            )
        if row["status"] not in ("draft", "pending_confirmation"):
            raise RuntimeError(f"REWORK_NOT_EXECUTABLE: 当前状态为 {row['status']}")
        if row["cost_status"] == "confirmation_required":
            raise RuntimeError(
                "REWORK_COST_CONFIRMATION_REQUIRED: 预计产生外部成本，需先确认"
            )

        current = connection.execute(
            """SELECT candidate_id FROM candidate_versions
            WHERE task_id=? ORDER BY version_no DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if current is None or current["candidate_id"] != row["source_candidate_id"]:
            raise RuntimeError(
                "REWORK_SOURCE_STALE: 返工单来源已不是当前候选，请重新记录"
            )

        reason = f"rework:{rework_id}"
        rollback_to_stage_connection(
            connection,
            task_id,
            row["target_stage"],
            reason=reason,
        )
        connection.execute(
            """UPDATE candidate_versions
            SET status='rejected', updated_at=datetime('now')
            WHERE task_id=? AND candidate_id=?""",
            (task_id, row["source_candidate_id"]),
        )
        connection.execute(
            """UPDATE publish_records
            SET package_status='expired'
            WHERE task_id=? AND candidate_id=? AND package_status='current'""",
            (task_id, row["source_candidate_id"]),
        )
        connection.execute(
            """UPDATE rework_requests
            SET status='applied', execution_key=?, updated_at=datetime('now')
            WHERE rework_id=?""",
            (idempotency_key.strip(), rework_id),
        )
        updated = connection.execute(
            "SELECT * FROM rework_requests WHERE rework_id=?", (rework_id,)
        ).fetchone()
        return _public_rework(updated)
