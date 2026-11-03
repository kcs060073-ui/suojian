"""Version-bound advisory content-check evidence; never a technical QA decision."""

from __future__ import annotations

import json
import math
import uuid
from contextlib import nullcontext

from .candidate_versions import list_candidates, resolve_candidate_media


_JSON_COLUMNS = {
    "coverage_refs_json": "coverage_refs",
    "uncovered_json": "uncovered",
    "issues_json": "issues",
    "unknowns_json": "unknowns",
}
_CONCLUSIONS = {"issues_found", "no_issue_in_coverage", "insufficient_evidence"}
_SEVERITIES = {"conflict", "needs_review", "suggestion"}


def _public(row) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for column, name in _JSON_COLUMNS.items():
        result[name] = json.loads(result.pop(column))
    return result


def get_content_review(db, task_id: str, candidate_id: str) -> dict | None:
    row = db.connection.execute(
        """SELECT * FROM content_review_runs
        WHERE task_id=? AND candidate_id=? ORDER BY rowid DESC LIMIT 1""",
        (task_id, candidate_id),
    ).fetchone()
    return _public(row)


def list_content_reviews(db, task_id: str) -> list[dict]:
    """Show one most recent run per candidate, including read-only history."""
    rows = db.connection.execute(
        """SELECT * FROM content_review_runs
        WHERE task_id=? ORDER BY rowid DESC""", (task_id,),
    ).fetchall()
    latest = {}
    for row in rows:
        latest.setdefault(row["candidate_id"], _public(row))
    return list(latest.values())


def _current_candidate(db, task_id: str, candidate_id: str) -> dict:
    candidates = list_candidates(db, task_id)
    current = candidates[0] if candidates else None
    if current is None or current["candidate_id"] != candidate_id or current.get("invalidation"):
        raise RuntimeError("CONTENT_REVIEW_CANDIDATE_STALE: 检查结果只属于当前成片版本")
    if not current.get("qa_snapshot_id") or not current.get("qa_report_id"):
        raise RuntimeError("CONTENT_REVIEW_QA_REQUIRED: 当前成片还没有通过技术 QA")
    report = current.get("qa_report") or {}
    if report.get("status") != "passed" or report.get("blocking_count") != 0:
        raise RuntimeError("CONTENT_REVIEW_QA_REQUIRED: 当前成片还没有通过技术 QA")
    try:
        resolve_candidate_media(db, task_id, candidate_id)
    except (OSError, RuntimeError, KeyError) as error:
        raise RuntimeError("CONTENT_REVIEW_MEDIA_CHANGED: 当前成片文件已变化") from error
    return current


def begin_content_review(
    db, task_id: str, candidate_id: str, *,
    executor: str, prompt_version: str, model_version: str, connection=None, supplemental: bool = False,
) -> dict:
    if executor not in {"vision_service", "agent"}:
        raise ValueError("CONTENT_REVIEW_EXECUTOR_INVALID: 检查执行者无效")
    if not all(isinstance(value, str) and value.strip()
               for value in (prompt_version, model_version)):
        raise ValueError("CONTENT_REVIEW_VERSION_REQUIRED: 请记录提示词和模型版本")
    if connection is not None and (connection is not db.connection or not connection.in_transaction or db._transaction_context_depth <= 0):
        raise RuntimeError("CONTENT_REVIEW_TRANSACTION_REQUIRED: 请在当前事务中创建检查")
    with (db.transaction(immediate=True) if connection is None else nullcontext(connection)) as connection:
        candidate = _current_candidate(db, task_id, candidate_id)
        approved = connection.execute(
            """SELECT 1 FROM review_approvals WHERE task_id=?
            AND snapshot_id=? AND qa_report_id=? AND decision='approved' LIMIT 1""",
            (task_id, candidate["qa_snapshot_id"], candidate["qa_report_id"]),
        ).fetchone()
        if approved is not None and not supplemental:
            raise RuntimeError("CONTENT_REVIEW_ALREADY_APPROVED: 已批准成片不能再启动新检查")
        duration = float((candidate.get("qa_report") or {}).get("video_spec", {}).get("duration", 0))
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("CONTENT_REVIEW_DURATION_REQUIRED: 技术 QA 缺少真实时长")
        run_id = f"content-review-{uuid.uuid4().hex}"
        connection.execute(
            """INSERT INTO content_review_runs(
                run_id,task_id,candidate_id,qa_snapshot_id,qa_report_id,
                video_sha256,duration_seconds,executor,prompt_version,model_version,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'running')""",
            (run_id, task_id, candidate_id, candidate["qa_snapshot_id"],
             candidate["qa_report_id"], candidate["output_signature"]["sha256"],
             duration, executor, prompt_version.strip(), model_version.strip()),
        )
        return _public(connection.execute(
            "SELECT * FROM content_review_runs WHERE run_id=?", (run_id,),
        ).fetchone())


def _latest_running_run(db, run_id: str):
    row = db.connection.execute(
        "SELECT * FROM content_review_runs WHERE run_id=?", (run_id,),
    ).fetchone()
    if row is None:
        raise KeyError("CONTENT_REVIEW_RUN_NOT_FOUND: 未找到内容检查记录")
    latest = db.connection.execute(
        """SELECT run_id FROM content_review_runs
        WHERE task_id=? AND candidate_id=? ORDER BY rowid DESC LIMIT 1""",
        (row["task_id"], row["candidate_id"]),
    ).fetchone()
    if latest["run_id"] != run_id:
        raise RuntimeError("CONTENT_REVIEW_RUN_STALE: 后续检查已开始")
    if row["status"] != "running":
        raise RuntimeError("CONTENT_REVIEW_RUN_CLOSED: 该次检查已有终态")
    candidate = _current_candidate(db, row["task_id"], row["candidate_id"])
    if (candidate["qa_snapshot_id"] != row["qa_snapshot_id"]
            or candidate["qa_report_id"] != row["qa_report_id"]
            or candidate["output_signature"]["sha256"] != row["video_sha256"]):
        raise RuntimeError("CONTENT_REVIEW_INPUT_CHANGED: 检查输入版本已变化")
    return row


def _text_list(value, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"CONTENT_REVIEW_RESULT_INVALID: {name} 必须是文字列表")
    return list(dict.fromkeys(item.strip() for item in value))


def _validated_issues(issues, coverage_refs: list[str], duration: float) -> list[dict]:
    if not isinstance(issues, list):
        raise ValueError("CONTENT_REVIEW_ISSUE_INVALID: 问题必须是列表")
    clean = []
    required = {"target_ref", "category", "severity", "description",
                "evidence_refs", "recommendation", "position_seconds"}
    for issue in issues:
        if not isinstance(issue, dict) or set(issue) != required:
            raise ValueError("CONTENT_REVIEW_ISSUE_INVALID: 问题缺少时码或依据")
        if (not all(isinstance(issue[key], str) and issue[key].strip()
                    for key in required - {"evidence_refs", "position_seconds"})
                or issue["severity"] not in _SEVERITIES):
            raise ValueError("CONTENT_REVIEW_ISSUE_INVALID: 问题说明或严重级别无效")
        position = issue["position_seconds"]
        if (isinstance(position, bool) or not isinstance(position, (int, float))
                or not math.isfinite(position) or position < 0 or position > duration):
            raise ValueError("CONTENT_REVIEW_ISSUE_INVALID: 问题时码超出成片")
        refs = _text_list(issue["evidence_refs"], "evidence_refs")
        if not refs or not set(refs).issubset(coverage_refs):
            raise ValueError("CONTENT_REVIEW_ISSUE_INVALID: 问题必须引用本次覆盖证据")
        clean.append({**issue, "evidence_refs": refs, "position_seconds": float(position)})
    return clean


def finish_content_review(
    db, run_id: str, *, coverage_refs: list[str], uncovered: list[str],
    conclusion: str, unknowns: list[str], issues: list[dict], binding_guard=None,
) -> dict:
    coverage = _text_list(coverage_refs, "coverage_refs")
    missing = _text_list(uncovered, "uncovered")
    uncertainties = _text_list(unknowns, "unknowns")
    if conclusion not in _CONCLUSIONS:
        raise ValueError("CONTENT_REVIEW_RESULT_INVALID: 检查结论无效")
    with db.transaction(immediate=True) as connection:
        if binding_guard is not None:binding_guard()
        run = _latest_running_run(db, run_id)
        clean_issues = _validated_issues(issues, coverage, run["duration_seconds"])
        if ((conclusion == "issues_found") != bool(clean_issues)
                or (conclusion == "no_issue_in_coverage" and not coverage)):
            raise ValueError("CONTENT_REVIEW_RESULT_INVALID: 问题与结论不一致")
        status = "partial" if missing or not coverage or conclusion == "insufficient_evidence" else "completed"
        connection.execute(
            """UPDATE content_review_runs
            SET status=?,coverage_refs_json=?,uncovered_json=?,issues_json=?,
                unknowns_json=?,conclusion=?,updated_at=datetime('now')
            WHERE run_id=?""",
            (status, json.dumps(coverage, ensure_ascii=False),
             json.dumps(missing, ensure_ascii=False), json.dumps(clean_issues, ensure_ascii=False),
             json.dumps(uncertainties, ensure_ascii=False), conclusion, run_id),
        )
        return _public(connection.execute(
            "SELECT * FROM content_review_runs WHERE run_id=?", (run_id,),
        ).fetchone())


def fail_content_review(db, run_id: str, *, reason: str) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("CONTENT_REVIEW_FAILURE_REASON_REQUIRED: 请记录真实失败原因")
    with db.transaction(immediate=True) as connection:
        _latest_running_run(db, run_id)
        connection.execute(
            """UPDATE content_review_runs SET status='failed',error_reason=?,
                updated_at=datetime('now') WHERE run_id=?""",
            (reason.strip(), run_id),
        )
        return _public(connection.execute(
            "SELECT * FROM content_review_runs WHERE run_id=?", (run_id,),
        ).fetchone())


def get_content_review_takeover(
    db, task_id: str, candidate_id: str, run_id: str, *,
    qa_snapshot_id: str | None = None, qa_report_id: str | None = None,
) -> dict | None:
    if (qa_snapshot_id is None) != (qa_report_id is None):
        raise ValueError("CONTENT_REVIEW_QA_BINDING_REQUIRED: 请提供完整质检版本")
    query = "SELECT * FROM content_review_takeovers WHERE task_id=? AND candidate_id=? AND run_id=?"
    params = [task_id, candidate_id, run_id]
    if qa_snapshot_id is not None:
        query += " AND qa_snapshot_id=? AND qa_report_id=?"
        params += [qa_snapshot_id, qa_report_id]
    row = db.connection.execute(query + " ORDER BY rowid DESC LIMIT 1", params).fetchone()
    return None if row is None else dict(row)


def get_content_review_issue_decisions(db, task_id: str, run_id: str) -> list[dict]:
    rows = db.connection.execute(
        """SELECT * FROM content_review_issue_decisions
        WHERE task_id=? AND run_id=? ORDER BY issue_index""",
        (task_id, run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _unresolved_issue_indexes(db, task_id: str, review: dict) -> list[int]:
    retained = {item["issue_index"] for item in get_content_review_issue_decisions(
        db, task_id, review["run_id"]
    ) if item["disposition"] == "retain"}
    return [index for index, issue in enumerate(review["issues"])
            if issue.get("severity") != "suggestion" and index not in retained]


def retain_content_review_issue(db, task_id, candidate_id, run_id, issue_index, *, reviewed_by, reason):
    return retain_content_review_issues(db, task_id, candidate_id, run_id, [issue_index],
                                       reviewed_by=reviewed_by, reason=reason)[0]


def retain_content_review_issues(db, task_id, candidate_id, run_id, issue_indexes, *,
                                reviewed_by, reason, expected_decisions=None):
    """One transaction, one decision per explicitly selected issue; no QA/watch approval."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("CONTENT_REVIEW_REASON_REQUIRED: 请说明保留本版成片的依据")
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise ValueError("CONTENT_REVIEW_REVIEWER_REQUIRED: 请记录处理人")
    if (not isinstance(issue_indexes, list) or not 1 <= len(issue_indexes) <= 100
            or any(type(i) is not int or i < 0 for i in issue_indexes)
            or len(set(issue_indexes)) != len(issue_indexes)):
        raise ValueError("CONTENT_REVIEW_ISSUE_INVALID: 请选择有效且不重复的问题")
    expected = {} if expected_decisions is None else expected_decisions
    if (not isinstance(expected, dict) or any(k not in {str(i) for i in issue_indexes}
            or not isinstance(v, str) or not v for k, v in expected.items())):
        raise ValueError("CONTENT_REVIEW_DECISION_INVALID: 原处理记录无效")
    with db.transaction(immediate=True) as connection:
        candidate = _current_candidate(db, task_id, candidate_id)
        if connection.execute("SELECT 1 FROM review_approvals WHERE task_id=? AND snapshot_id=? AND qa_report_id=? AND decision='approved' LIMIT 1",
                              (task_id, candidate['qa_snapshot_id'], candidate['qa_report_id'])).fetchone():
            raise RuntimeError("CONTENT_REVIEW_ALREADY_APPROVED: 已确认成片的核对记录只读")
        review = get_content_review(db, task_id, candidate_id)
        if (review is None or review['run_id'] != run_id or review['status'] not in {'completed', 'partial'}
                or any(i >= len(review['issues']) for i in issue_indexes)):
            raise RuntimeError("CONTENT_REVIEW_ISSUE_STALE: 本版检查问题已变化")
        result = []
        for index in issue_indexes:
            existing = connection.execute('SELECT * FROM content_review_issue_decisions WHERE run_id=? AND issue_index=?', (run_id, index)).fetchone()
            if existing is not None and existing['reason'] == reason.strip() and existing['reviewed_by'] == reviewed_by.strip():
                result.append(dict(existing)); continue
            if (existing is not None and expected.get(str(index)) != existing['decision_id']) or (existing is None and str(index) in expected):
                raise RuntimeError("CONTENT_REVIEW_DECISION_CONFLICT: 处理记录已变化，请刷新后重新核对")
            decision_id = f"content-decision-{uuid.uuid4().hex}"
            if existing is not None:
                connection.execute("""INSERT INTO content_review_decision_history
                    (decision_id,task_id,candidate_id,run_id,issue_index,disposition,reason,reviewed_by,created_at,replaced_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", tuple(existing[k] for k in
                    ['decision_id','task_id','candidate_id','run_id','issue_index','disposition','reason','reviewed_by','created_at']) + (decision_id,))
                connection.execute("""UPDATE content_review_issue_decisions SET decision_id=?,reason=?,reviewed_by=?,created_at=datetime('now')
                    WHERE run_id=? AND issue_index=?""", (decision_id,reason.strip(),reviewed_by.strip(),run_id,index))
            else:
                connection.execute("""INSERT INTO content_review_issue_decisions
                    (decision_id,task_id,candidate_id,run_id,issue_index,disposition,reason,reviewed_by)
                    VALUES(?,?,?,?,?,'retain',?,?)""", (decision_id,task_id,candidate_id,run_id,index,reason.strip(),reviewed_by.strip()))
            result.append(dict(connection.execute('SELECT * FROM content_review_issue_decisions WHERE decision_id=?',(decision_id,)).fetchone()))
        return result


def acknowledge_content_review_takeover(
    db, task_id: str, candidate_id: str, watch_session_id: str,
    *, acknowledged_by: str, acknowledged: bool,
) -> dict:
    with db.transaction(immediate=True):
        return _acknowledge_content_review_takeover(db, task_id, candidate_id,
            watch_session_id, acknowledged_by=acknowledged_by, acknowledged=acknowledged)


def approve_with_content_acknowledgement(
    db, task_id: str, candidate_id: str, snapshot_id: str, qa_report_id: str,
    watch_session_id: str, reviewed_by: str, *, run_id: str,
    acknowledged: bool, notes: str | None = None,
) -> dict:
    """One explicit user decision, two bound audit records, one atomic commit."""
    from .reviews import insert_review_decision
    with db.transaction(immediate=True) as connection:
        candidate = _current_candidate(db, task_id, candidate_id)
        if candidate["qa_snapshot_id"] != snapshot_id or candidate["qa_report_id"] != qa_report_id:
            raise RuntimeError("CONTENT_REVIEW_INPUT_CHANGED: 成片版本已变化")
        review = get_content_review(db, task_id, candidate_id)
        if not review or review["run_id"] != run_id:
            raise RuntimeError("CONTENT_REVIEW_RUN_STALE: 检查结果已更新，请重新核对")
        _acknowledge_content_review_takeover(db, task_id, candidate_id, watch_session_id,
            acknowledged_by=reviewed_by, acknowledged=acknowledged)
        return insert_review_decision(connection, db, task_id, snapshot_id, qa_report_id,
            "approved", reviewed_by, notes, watch_session_id)


def _acknowledge_content_review_takeover(
    db, task_id: str, candidate_id: str, watch_session_id: str,
    *, acknowledged_by: str, acknowledged: bool,
) -> dict:
    if acknowledged is not True:
        raise ValueError("CONTENT_REVIEW_ACK_REQUIRED: 请明确确认内容检查未完成")
    if not isinstance(acknowledged_by, str) or not acknowledged_by.strip():
        raise ValueError("CONTENT_REVIEW_REVIEWER_REQUIRED: 请记录确认人")
    from .full_review import validate_completed_watch

    connection = db.connection
    if not connection.in_transaction:
        raise RuntimeError("REVIEW_TRANSACTION_REQUIRED")
    candidate = _current_candidate(db, task_id, candidate_id)
    review = get_content_review(db, task_id, candidate_id)
    if review is None or review["status"] not in {"partial", "failed"}:
        raise RuntimeError("CONTENT_REVIEW_TAKEOVER_UNAVAILABLE: 只有失败或覆盖不足才可接管")
    if _unresolved_issue_indexes(db, task_id, review):
        raise RuntimeError("CONTENT_REVIEW_ISSUES_OPEN: 请先处理本版内容检查问题")
    validate_completed_watch(
        db, watch_session_id, task_id,
        candidate["qa_snapshot_id"], candidate["qa_report_id"],
    )
    open_issue = connection.execute(
        """SELECT 1 FROM review_issues WHERE task_id=? AND candidate_id=?
        AND status='open' LIMIT 1""", (task_id, candidate_id),
    ).fetchone()
    if open_issue is not None:
        raise RuntimeError("REVIEW_ISSUES_OPEN: 请先处理本版人工审片问题")
    if review["video_sha256"] != candidate["output_signature"]["sha256"]:
        raise RuntimeError("CONTENT_REVIEW_INPUT_CHANGED: 检查输入版本已变化")
    existing = get_content_review_takeover(
        db, task_id, candidate_id, review["run_id"],
        qa_snapshot_id=candidate["qa_snapshot_id"], qa_report_id=candidate["qa_report_id"],
    )
    if existing is not None:
        return existing
    takeover_id = f"content-takeover-{uuid.uuid4().hex}"
    connection.execute(
        """INSERT INTO content_review_takeovers(
            takeover_id,task_id,candidate_id,run_id,qa_snapshot_id,qa_report_id,
            watch_session_id,review_status,acknowledged_by
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (takeover_id, task_id, candidate_id, review["run_id"],
         candidate["qa_snapshot_id"], candidate["qa_report_id"],
         watch_session_id, review["status"], acknowledged_by.strip()),
    )
    return get_content_review_takeover(db, task_id, candidate_id, review["run_id"])

def require_content_review_clearance(db, task_id: str, candidate_id: str) -> None:
    """Check advisory evidence within the same transaction as human approval."""
    pending = db.connection.execute("""SELECT 1 FROM jobs j JOIN job_payloads p USING(job_id)
        WHERE j.task_id=? AND j.job_type='content_review' AND j.status IN ('queued','running','paused')
        AND json_extract(p.payload_json,'$.candidate_id')=? LIMIT 1""",(task_id,candidate_id)).fetchone()
    if pending:
        raise RuntimeError("CONTENT_REVIEW_RUNNING: 智能审查正在排队或执行，请等待检查结果")
    review = get_content_review(db, task_id, candidate_id)
    latest_job = db.connection.execute("""SELECT j.status,j.cursor FROM jobs j
        JOIN job_payloads p USING(job_id) WHERE j.task_id=? AND j.job_type='content_review'
        AND json_extract(p.payload_json,'$.candidate_id')=? ORDER BY j.rowid DESC LIMIT 1""",
        (task_id,candidate_id)).fetchone()
    if latest_job and latest_job['status']!='completed' and (not review or latest_job['cursor']!='content_review:'+review['run_id']):
        raise RuntimeError("CONTENT_REVIEW_NOT_COMPLETED: 本次智能审查尚未取得结果，请处理检查任务")
    if review is None:
        # Transitional tasks have no approved P10 executor yet; the UI says so.
        return
    candidate = _current_candidate(db, task_id, candidate_id)
    if review["video_sha256"] != candidate["output_signature"]["sha256"]:
        raise RuntimeError("CONTENT_REVIEW_INPUT_CHANGED: 检查输入版本已变化")
    if review["status"] == "running":
        raise RuntimeError("CONTENT_REVIEW_RUNNING: 内容辅助检查仍在运行")
    if _unresolved_issue_indexes(db, task_id, review):
        raise RuntimeError("CONTENT_REVIEW_ISSUES_OPEN: 请先处理本版内容检查问题")
    if review["status"] in {"partial", "failed"}:
        takeover = get_content_review_takeover(
            db, task_id, candidate_id, review["run_id"],
            qa_snapshot_id=candidate["qa_snapshot_id"], qa_report_id=candidate["qa_report_id"],
        )
        if takeover is None or takeover["review_status"] != review["status"]:
            raise RuntimeError("CONTENT_REVIEW_TAKEOVER_REQUIRED: 请知情确认本版检查未完成")
