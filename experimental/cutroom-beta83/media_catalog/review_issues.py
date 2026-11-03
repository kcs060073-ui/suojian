"""Candidate-bound human observations for the frozen final-review page."""

from __future__ import annotations

import json
import math
import uuid

from .candidate_versions import get_candidate, list_candidates, resolve_candidate_media


CATEGORIES = frozenset({"material", "packaging", "script", "audio", "technical"})


def _current_candidate(db, task_id: str, candidate_id: str) -> dict:
    candidates = list_candidates(db, task_id)
    if not candidates or candidates[0]["candidate_id"] != candidate_id:
        raise RuntimeError("REVIEW_ISSUE_CANDIDATE_STALE: 只能记录当前成片的问题")
    candidate = get_candidate(db, task_id, candidate_id)
    if candidate is None:
        raise RuntimeError("REVIEW_ISSUE_CANDIDATE_STALE: 候选不存在")
    return candidate


def _media_duration(db, task_id: str, candidate: dict) -> float:
    try:
        resolve_candidate_media(db, task_id, candidate["candidate_id"])
    except (OSError, KeyError, RuntimeError) as error:
        raise RuntimeError("REVIEW_ISSUE_MEDIA_CHANGED: 候选成片缺失或内容已变化") from error
    report_id = candidate.get("qa_report_id")
    row = db.connection.execute(
        "SELECT report_json FROM qa_reports WHERE task_id=? AND report_id=?",
        (task_id, report_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("REVIEW_ISSUE_QA_MISSING: 当前候选缺少 QA 报告")
    try:
        duration = float(json.loads(row["report_json"])["video_spec"]["duration"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise RuntimeError("REVIEW_ISSUE_QA_INVALID: QA 缺少成片时长") from error
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("REVIEW_ISSUE_QA_INVALID: QA 成片时长无效")
    return duration


def list_review_issues(db, task_id: str, candidate_id: str) -> list[dict]:
    rows = db.connection.execute(
        """SELECT * FROM review_issues WHERE task_id=? AND candidate_id=?
        ORDER BY created_at, rowid""",
        (task_id, candidate_id),
    ).fetchall()
    return [dict(row) for row in rows]


def add_review_issue(db, task_id: str, candidate_id: str, category: str,
                     notes: str, position_seconds: float, *, request_id: str) -> dict:
    if category not in CATEGORIES:
        raise ValueError("REVIEW_ISSUE_CATEGORY_INVALID: 未知问题类型")
    if not isinstance(notes, str) or not notes.strip():
        raise ValueError("REVIEW_ISSUE_NOTES_REQUIRED: 请描述发现的问题")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("REVIEW_ISSUE_REQUEST_REQUIRED: 缺少请求身份")
    try:
        position = float(position_seconds)
    except (TypeError, ValueError) as error:
        raise ValueError("REVIEW_ISSUE_POSITION_INVALID: 时间点必须是数字") from error
    if not math.isfinite(position) or position < 0:
        raise ValueError("REVIEW_ISSUE_POSITION_INVALID: 时间点无效")

    # Serialize candidate validation and insertion with approval/publication.
    # A request waiting behind publication must observe the now-closed candidate.
    with db.transaction(immediate=True) as connection:
        candidate = _current_candidate(db, task_id, candidate_id)
        existing = connection.execute(
            "SELECT * FROM review_issues WHERE task_id=? AND request_id=?",
            (task_id, request_id.strip()),
        ).fetchone()
        if existing is not None:
            if (existing["candidate_id"] != candidate_id or existing["category"] != category
                    or existing["notes"] != notes.strip()
                    or existing["position_seconds"] != position):
                raise RuntimeError("REVIEW_ISSUE_REQUEST_CONFLICT: 请求身份已用于不同问题")
            return dict(existing)
        if candidate["status"] not in {"awaiting_review", "qa_blocked"}:
            raise RuntimeError("REVIEW_ISSUE_CANDIDATE_CLOSED: 当前候选已结束审查")
        duration = _media_duration(db, task_id, candidate)
        if position > duration:
            raise ValueError("REVIEW_ISSUE_POSITION_INVALID: 时间点超过成片时长")
        issue_id = f"issue-{uuid.uuid4().hex[:16]}"
        connection.execute(
            """INSERT INTO review_issues(
              issue_id,task_id,candidate_id,category,notes,position_seconds,request_id
            ) VALUES(?,?,?,?,?,?,?)""",
            (issue_id, task_id, candidate_id, category, notes.strip(), position,
             request_id.strip()),
        )
        return dict(connection.execute(
            "SELECT * FROM review_issues WHERE issue_id=?", (issue_id,),
        ).fetchone())


def withdraw_review_issue(db, task_id: str, candidate_id: str, issue_id: str) -> dict:
    with db.transaction() as connection:
        _current_candidate(db, task_id, candidate_id)
        row = connection.execute(
            "SELECT * FROM review_issues WHERE task_id=? AND candidate_id=? AND issue_id=?",
            (task_id, candidate_id, issue_id),
        ).fetchone()
        if row is None:
            raise KeyError("REVIEW_ISSUE_NOT_FOUND: 未找到本版人工问题")
        if row["status"] == "open":
            connection.execute(
                """UPDATE review_issues SET status='withdrawn',
                withdrawn_at=datetime('now') WHERE issue_id=?""", (issue_id,),
            )
        return dict(connection.execute(
            "SELECT * FROM review_issues WHERE issue_id=?", (issue_id,),
        ).fetchone())
