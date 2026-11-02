"""Trusted projection of the twelve-stage colleague workflow."""

import json

from .reviews import matching_approval
from .snapshots import latest_review_candidate


WORKFLOW_STAGES = (
    "create_preflight",
    "brief_lock",
    "material_sync",
    "ai_storyboard",
    "rough_cut",
    "picture_order",
    "picture_lock",
    "packaging_confirm",
    "fine_render",
    "auto_qa",
    "full_review",
    "publish_package",
)

_STAGE_ACTIONS = {
    "create_preflight": "complete_preflight",
    "brief_lock": "lock_brief",
    "material_sync": "sync_materials",
    "ai_storyboard": "generate_storyboard",
    "rough_cut": "generate_rough_cut",
    "picture_order": "confirm_picture_order",
    "picture_lock": "lock_picture",
    "packaging_confirm": "confirm_packaging",
    "fine_render": "render_fine_cut",
    "auto_qa": "run_auto_qa",
    "full_review": "complete_review",
    "publish_package": "build_publish_package",
}
_HUMAN_STAGES = {
    "brief_lock",
    "picture_order",
    "picture_lock",
    "packaging_confirm",
    "full_review",
}
_JOB_RUN_STATUS = {
    "queued": "queued",
    "running": "running",
    "pending": "queued",
    "failed": "failed",
    "interrupted": "interrupted",
    "stale": "interrupted",
    "cancelled": "cancelled",
}


def _require_task(db, task_id: str) -> None:
    row = db.connection.execute(
        "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _latest_candidate(db, task_id: str) -> dict | None:
    try:
        return latest_review_candidate(db, task_id)
    except (TypeError, ValueError, OSError):
        return None


def project_gates(db, task_id: str) -> dict:
    """Project trusted workflow gates from durable evidence, never task status."""
    _require_task(db, task_id)
    brief_event = db.connection.execute(
        """
        SELECT 1 FROM task_gate_events
        WHERE task_id=? AND gate_type='brief'
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    picture = db.connection.execute(
        """
        SELECT changed_since_lock FROM picture_lock WHERE task_id=?
        """,
        (task_id,),
    ).fetchone()
    picture_event = db.connection.execute(
        """
        SELECT 1 FROM task_gate_events
        WHERE task_id=? AND gate_type='picture_lock'
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    confirmed_plan = db.connection.execute(
        """
        SELECT 1 FROM packaging_plans
        WHERE task_id=? AND status='confirmed'
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    packaging_event = db.connection.execute(
        """
        SELECT 1 FROM task_gate_events
        WHERE task_id=? AND gate_type='packaging'
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    qa = db.connection.execute(
        """
        SELECT status FROM qa_reports WHERE task_id=?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()

    candidate = _latest_candidate(db, task_id)
    approval = None
    if candidate is not None:
        approval = matching_approval(
            db,
            task_id,
            candidate["qa_snapshot_id"],
            candidate["qa_report_id"],
        )

    gates = {
        "brief_locked": brief_event is not None,
        "picture_lock": bool(
            picture is not None
            and not picture["changed_since_lock"]
            and picture_event is not None
        ),
        "packaging_confirmed": bool(
            confirmed_plan is not None and packaging_event is not None
        ),
        "qa_passed": bool(qa is not None and qa["status"] == "passed"),
        "human_reviewed": bool(
            approval is not None and approval["decision"] == "approved"
        ),
    }
    active_rework = db.connection.execute(
        """SELECT target_stage FROM rework_requests
        WHERE task_id=? AND status='applied' AND result_candidate_id IS NULL
        ORDER BY updated_at DESC,rowid DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    if active_rework is not None:
        target_index = WORKFLOW_STAGES.index(active_rework["target_stage"])
        for key, stage in (
            ("brief_locked", "brief_lock"),
            ("picture_lock", "picture_lock"),
            ("packaging_confirmed", "packaging_confirm"),
            ("qa_passed", "auto_qa"),
            ("human_reviewed", "full_review"),
        ):
            if target_index <= WORKFLOW_STAGES.index(stage):
                gates[key] = False
    return gates


def _seed_stage_states(connection, task_id: str) -> None:
    for index, stage_key in enumerate(WORKFLOW_STAGES):
        connection.execute(
            """
            INSERT OR IGNORE INTO task_stage_states(task_id, stage_key, status)
            VALUES(?, ?, ?)
            """,
            (task_id, stage_key, "ready" if index == 0 else "not_started"),
        )


def seed_stage_states(db, task_id: str) -> None:
    """Idempotently create one durable state row for every workflow stage."""
    _require_task(db, task_id)
    with db.transaction() as connection:
        _seed_stage_states(connection, task_id)


def complete_stage(
    db,
    task_id: str,
    stage_key: str,
    *,
    input_digest: str,
    details,
) -> dict:
    """Complete one stage and make its immediate successor runnable."""
    if stage_key not in WORKFLOW_STAGES:
        raise ValueError(f"未知工作流阶段：{stage_key}")
    seed_stage_states(db, task_id)
    detail_json = _canonical_json(
        {"input_digest": input_digest, "details": details}
    )
    index = WORKFLOW_STAGES.index(stage_key)
    with db.transaction() as connection:
        changed = connection.execute(
            """
            UPDATE task_stage_states
            SET status='completed', detail_json=?, updated_at=datetime('now')
            WHERE task_id=? AND stage_key=?
              AND (status != 'completed' OR detail_json IS NOT ?)
            """,
            (detail_json, task_id, stage_key, detail_json),
        ).rowcount
        if changed and index + 1 < len(WORKFLOW_STAGES):
            connection.execute(
                """
                UPDATE task_stage_states
                SET status='ready', updated_at=datetime('now')
                WHERE task_id=? AND stage_key=?
                  AND status IN ('not_started', 'invalidated')
                """,
                (task_id, WORKFLOW_STAGES[index + 1]),
            )
    row = db.connection.execute(
        """
        SELECT task_id, stage_key, status, detail_json, updated_at
        FROM task_stage_states WHERE task_id=? AND stage_key=?
        """,
        (task_id, stage_key),
    ).fetchone()
    return dict(row)


def invalidate_from_connection(
    connection,
    task_id: str,
    stage_key: str,
    *,
    reason: str,
) -> int:
    """Invalidate downstream stages inside the caller-owned transaction."""
    if stage_key not in WORKFLOW_STAGES:
        raise ValueError(f"未知工作流阶段：{stage_key}")
    if connection.execute(
        "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone() is None:
        raise KeyError(f"任务不存在：{task_id}")
    _seed_stage_states(connection, task_id)
    downstream = WORKFLOW_STAGES[WORKFLOW_STAGES.index(stage_key) + 1 :]
    if not downstream:
        return 0
    detail_json = _canonical_json({"reason": reason})
    placeholders = ",".join("?" for _ in downstream)
    return connection.execute(
        f"""
        UPDATE task_stage_states
        SET status='invalidated', detail_json=?, updated_at=datetime('now')
        WHERE task_id=? AND stage_key IN ({placeholders})
        """,
        (detail_json, task_id, *downstream),
    ).rowcount


def invalidate_from(
    db,
    task_id: str,
    stage_key: str,
    *,
    reason: str,
) -> int:
    """Invalidate every stage downstream of a stable change boundary."""
    with db.transaction() as connection:
        return invalidate_from_connection(
            connection,
            task_id,
            stage_key,
            reason=reason,
        )


def rollback_to_stage_connection(
    connection,
    task_id: str,
    stage_key: str,
    *,
    reason: str,
) -> int:
    """Make one stage runnable and invalidate only its downstream stages."""
    if stage_key not in WORKFLOW_STAGES:
        raise ValueError(f"未知工作流阶段：{stage_key}")
    _seed_stage_states(connection, task_id)
    detail_json = _canonical_json({"reason": reason})
    connection.execute(
        """UPDATE task_stage_states
        SET status='ready', detail_json=?, updated_at=datetime('now')
        WHERE task_id=? AND stage_key=?""",
        (detail_json, task_id, stage_key),
    )
    return invalidate_from_connection(
        connection,
        task_id,
        stage_key,
        reason=reason,
    )


def _parsed_detail(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _trusted_mix_exists(db, task_id: str) -> bool:
    row = db.connection.execute(
        """
        SELECT params_json, input_digest FROM render_snapshots
        WHERE task_id=? AND stage='mix' AND producer='assemble-v1'
          AND input_digest IS NOT NULL
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        params = json.loads(row["params_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(
        params.get("dependency_digest") == row["input_digest"]
        and params.get("dependency_snapshot") is not None
        and params.get("packaging") is not None
        and params.get("output_signature") is not None
    )


def _generic_states(db, task_id: str) -> dict[str, dict]:
    states = {
        stage_key: {"status": "not_started", "detail": None}
        for stage_key in WORKFLOW_STAGES
    }
    rows = db.connection.execute(
        """
        SELECT stage_key, status, detail_json FROM task_stage_states
        WHERE task_id=?
        """,
        (task_id,),
    ).fetchall()
    for row in rows:
        states[row["stage_key"]] = {
            "status": row["status"],
            "detail": _parsed_detail(row["detail_json"]),
        }
    return states


def _mark_completed(states: dict[str, dict], stage_keys) -> None:
    for stage_key in stage_keys:
        # An invalidation is a current freshness veto over preserved history.
        detail = states[stage_key].get("detail")
        rework_ready = (
            states[stage_key]["status"] == "ready"
            and isinstance(detail, dict)
            and str(detail.get("reason") or "").startswith("rework:")
        )
        if states[stage_key]["status"] != "invalidated" and not rework_ready:
            states[stage_key]["status"] = "completed"


def _latest_publish_status(db, task_id: str) -> str | None:
    candidate = _latest_candidate(db, task_id)
    if candidate is None or not candidate.get("candidate_id"):
        return None
    row = db.connection.execute(
        """
        SELECT status FROM publish_records
        WHERE task_id=? AND candidate_id=? AND package_status='current'
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id, candidate["candidate_id"]),
    ).fetchone()
    return None if row is None else row["status"]


def _apply_trusted_facts(db, task_id: str, states: dict[str, dict], gates: dict) -> None:
    if gates["brief_locked"]:
        _mark_completed(states, WORKFLOW_STAGES[:2])

    source_snapshot = db.connection.execute(
        "SELECT 1 FROM task_source_snapshots WHERE task_id=? LIMIT 1",
        (task_id,),
    ).fetchone()
    if source_snapshot is not None:
        _mark_completed(states, ("material_sync",))

    card_summary = db.connection.execute(
        """
        SELECT COUNT(*) AS card_count,
               COALESCE(MIN(confirm_status), 0) AS all_confirmed
        FROM scene_cards WHERE task_id=?
        """,
        (task_id,),
    ).fetchone()
    if card_summary["card_count"]:
        _mark_completed(states, ("ai_storyboard",))
        if card_summary["all_confirmed"]:
            _mark_completed(states, ("picture_order",))

    applied_edit = db.connection.execute(
        """
        SELECT 1 FROM rough_cut_edits
        WHERE task_id=? AND status='applied' LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if applied_edit is not None:
        _mark_completed(states, ("rough_cut",))

    if gates["picture_lock"]:
        _mark_completed(
            states,
            WORKFLOW_STAGES[: WORKFLOW_STAGES.index("picture_lock") + 1],
        )
    if gates["packaging_confirmed"]:
        _mark_completed(states, ("packaging_confirm",))

    candidate = _latest_candidate(db, task_id)
    if candidate is not None or _trusted_mix_exists(db, task_id):
        _mark_completed(states, ("fine_render",))
    if candidate is not None:
        _mark_completed(states, ("auto_qa",))
    if gates["human_reviewed"]:
        _mark_completed(states, ("full_review",))

    publish_status = _latest_publish_status(db, task_id)
    if publish_status is not None and not gates["human_reviewed"]:
        states["publish_package"]["status"] = "invalidated"
        states["publish_package"]["detail"] = {
            "reason": "当前候选缺少有效的完整审片批准；旧发布记录仅作历史保留"
        }
    elif publish_status == "cleared":
        _mark_completed(states, ("publish_package",))
    elif publish_status == "blocked" and states["publish_package"]["status"] != "invalidated":
        states["publish_package"]["status"] = "blocked"
    elif publish_status == "draft" and states["publish_package"]["status"] != "invalidated":
        states["publish_package"]["status"] = "in_progress"


def _project_run_status(db, task_id: str, current_stage: str | None) -> str:
    latest_job = db.connection.execute(
        """
        SELECT j.status,COALESCE(control.state,'active') AS control_state
        FROM jobs j
        LEFT JOIN job_controls control USING(job_id)
        WHERE j.task_id=?
        ORDER BY j.updated_at DESC, j.created_at DESC, j.rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if latest_job is not None and latest_job["control_state"] == "paused":
        return "paused"
    if latest_job is not None and latest_job["status"] in _JOB_RUN_STATUS:
        return _JOB_RUN_STATUS[latest_job["status"]]
    if current_stage is None:
        return "completed"
    if current_stage in _HUMAN_STAGES:
        return "awaiting_user"
    return "ready"


def project_workflow(db, task_id: str) -> dict:
    """Return one stable projection of durable workflow and trusted history."""
    _require_task(db, task_id)
    gates = project_gates(db, task_id)
    states = _generic_states(db, task_id)
    _apply_trusted_facts(db, task_id, states, gates)

    current_stage = next(
        (
            stage_key
            for stage_key in WORKFLOW_STAGES
            if states[stage_key]["status"] != "completed"
        ),
        None,
    )
    if current_stage is not None and states[current_stage]["status"] == "not_started":
        states[current_stage]["status"] = "ready"

    stages = [
        {
            "stage_key": stage_key,
            "status": states[stage_key]["status"],
            "detail": states[stage_key]["detail"],
        }
        for stage_key in WORKFLOW_STAGES
    ]
    return {
        "task_id": task_id,
        "current_stage": current_stage,
        "run_status": _project_run_status(db, task_id, current_stage),
        "next_action": None if current_stage is None else _STAGE_ACTIONS[current_stage],
        "gates": gates,
        "stages": stages,
    }
