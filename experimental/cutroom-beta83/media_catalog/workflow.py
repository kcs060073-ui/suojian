"""Trusted projection of the twelve-stage colleague workflow."""

import json
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path
from time import monotonic

from .provenance import canonical_digest
from .reviews import matching_approval
from .snapshots import latest_review_candidate, file_content_signature
from .script_segments import storyboard_digest


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


@lru_cache(maxsize=512)
def _projection_file_signature(path: str, stamp: tuple, second: int) -> dict:
    # Display-only, <=1s cache. Render/QA/export continue to rehash unconditionally.
    return file_content_signature(path)


def _packaging_preview_files_current(db, task_id: str, event=None) -> bool:
    """Check frozen receipt bytes without recompilation or media probing."""
    from . import packaging_plan
    if event is None:
        event = db.connection.execute(
            "SELECT details_json,gate_digest FROM task_gate_events "
            "WHERE task_id=? AND gate_type='packaging' ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if event is None:
        return True
    try:
        details = json.loads(event['details_json'] or '{}')
        evidence = details.get('confirmation_evidence') or {}
        preview_id = evidence.get('preview_id')
        if not preview_id:
            return True  # Legacy CLI imports did not retain preview receipts.
        row = db.connection.execute(
            "SELECT * FROM packaging_preview_artifacts WHERE preview_id=? AND task_id=? AND plan_id=?",
            (preview_id, task_id, details.get('plan_id')),
        ).fetchone()
        if (row is None or row['status'] != 'ready' or not row['viewed_at']
                or row['canonical_key'] != evidence.get('canonical_key')):
            return False
        receipt = json.loads(row['preview_receipt_json'] or '{}')
        for digest_key in ('recipe_digest', 'recipe_content_digest'):
            if digest_key in evidence and receipt.get(digest_key) != evidence[digest_key]:
                return False
        entries = receipt.get('asset_versions')
        if not isinstance(entries, list) or not entries:
            return False
        signed = {str(Path(item['path']).resolve()): item for item in entries}
        plan = packaging_plan.get_plan(db, details['plan_id'])
        if (plan is None or plan['status'] != 'confirmed'
                or canonical_digest(packaging_plan.canonical_packaging_gate_payload(plan)) != event['gate_digest']):
            return False
        # Older digital receipts omitted non-primary instances: fail closed on those.
        selected = [(item['slot'], item['asset_ref']) for item in packaging_plan.active_items(plan)
                    if not item.get('is_backup')]
        selected.extend((item['role'], item['asset_ref']) for item in packaging_plan.active_instances(plan))
        for role, asset_ref in set(selected):
            category = 'title_card' if role in {'brand', 'info_card'} else role
            if packaging_plan.find_item(category, asset_ref, db) is None:
                return False
            path = packaging_plan.resolve_manifest_asset_path(category, asset_ref, db)
            if path is not None and str(path.resolve()) not in signed:
                return False
        for path, entry in signed.items():
            stat = Path(path).stat()
            stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_dev)
            if _projection_file_signature(path, stamp, int(monotonic())) != {
                'size_bytes': entry['size_bytes'], 'sha256': entry['sha256'],
            }:
                return False
        return True
    except (OSError, TypeError, ValueError, KeyError, RuntimeError):
        return False

def project_brief_locked(db, task_id: str, *, brief_event=None) -> bool:
    """Read only the current brief gate; voice context has no packaging dependency."""
    _require_task(db, task_id)
    if brief_event is None:
        brief_event = db.connection.execute(
            "SELECT event_id,gate_digest FROM task_gate_events "
            "WHERE task_id=? AND gate_type='brief' ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    task_brief_row = db.connection.execute(
        "SELECT brief_json,status FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    try:
        task_brief = json.loads(task_brief_row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        task_brief = {}
    if not isinstance(task_brief, dict):
        task_brief = {}
    # Legacy tasks may have no structured brief. A versioned script, however,
    # only has approval when its current brief exactly matches the latest gate.
    script_versioned = isinstance(task_brief.get("final_script"), dict)
    brief_current = brief_event is not None and (
        not script_versioned or brief_event["gate_digest"] == canonical_digest(task_brief)
    )
    # A transcript revision explicitly reopens confirmation and archives its
    # artifacts. Saving A -> B -> A must not resurrect A's historical gate.
    # This is only a veto for that durable rollback, never approval by status.
    if brief_current and script_versioned and task_brief_row["status"] == "draft":
        boundary = db.connection.execute(
            "SELECT status,detail_json FROM task_stage_states WHERE task_id=? AND stage_key='brief_lock'",
            (task_id,),
        ).fetchone()
        detail = _parsed_detail(boundary["detail_json"]) if boundary is not None else None
        if (boundary is not None and boundary["status"] == "ready"
                and isinstance(detail, dict)
                and detail.get("reason") == "script_revision:transcript_review"):
            brief_current = False
    if brief_current:
        # A formal rework also revokes the historical approval even when the
        # script bytes happen to match. Keep the full gate projection's rule.
        boundaries = db.connection.execute(
            "SELECT target_stage,impact_json FROM rework_requests "
            "WHERE task_id=? AND status='applied' ORDER BY rowid DESC", (task_id,)
        ).fetchall()
        boundary = next((row for row in boundaries
                         if WORKFLOW_STAGES.index(row["target_stage"]) <= WORKFLOW_STAGES.index("brief_lock")), None)
        if boundary is not None:
            impact = json.loads(boundary["impact_json"] or "{}")
            prior = impact.get("prior_gate_event_ids") or {}
            prior_id = prior.get("brief") if "brief" in prior else impact.get("prior_brief_gate_event_id")
            brief_current = bool(("brief" in prior or "prior_brief_gate_event_id" in impact)
                                 and brief_event["event_id"] != prior_id)
    return brief_current


def project_gates(db, task_id: str) -> dict:
    """Project trusted workflow gates from durable evidence, never task status."""
    _require_task(db, task_id)
    brief_event = db.connection.execute(
        """
        SELECT event_id,gate_digest FROM task_gate_events
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
        SELECT event_id,rowid AS gate_order FROM task_gate_events
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
        SELECT event_id,rowid AS gate_order,details_json,gate_digest FROM task_gate_events
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
    has_candidate_versions = db.connection.execute(
        "SELECT 1 FROM candidate_versions WHERE task_id=? LIMIT 1", (task_id,)
    ).fetchone() is not None
    approval = None
    if candidate is not None:
        approval = matching_approval(
            db,
            task_id,
            candidate["qa_snapshot_id"],
            candidate["qa_report_id"],
        )

    brief_current = project_brief_locked(db, task_id, brief_event=brief_event)
    gates = {
        "brief_locked": brief_current,
        "picture_lock": bool(
            picture is not None
            and not picture["changed_since_lock"]
            and picture_event is not None
        ),
        "packaging_confirmed": bool(
            confirmed_plan is not None and packaging_event is not None
            and _packaging_preview_files_current(db, task_id, packaging_event)
            and (picture is None or (
                not picture["changed_since_lock"] and picture_event is not None
                and packaging_event["gate_order"] > picture_event["gate_order"]
            ))
        ),
        "qa_passed": bool(
            candidate is not None if has_candidate_versions
            else qa is not None and qa["status"] == "passed"
        ),
        "human_reviewed": bool(
            approval is not None and approval["decision"] == "approved"
        ),
    }
    applied_reworks = db.connection.execute(
        """SELECT target_stage,impact_json FROM rework_requests
        WHERE task_id=? AND status='applied'
        ORDER BY rowid DESC""",
        (task_id,),
    ).fetchall()
    gate_events = {
        "brief_locked": ("brief", brief_event),
        "picture_lock": ("picture_lock", picture_event),
        "packaging_confirmed": ("packaging", packaging_event),
    }
    for key, stage in (
        ("brief_locked", "brief_lock"),
        ("picture_lock", "picture_lock"),
        ("packaging_confirmed", "packaging_confirm"),
        ("qa_passed", "auto_qa"),
        ("human_reviewed", "full_review"),
    ):
        boundary = next(
            (row for row in applied_reworks
             if WORKFLOW_STAGES.index(row["target_stage"]) <= WORKFLOW_STAGES.index(stage)),
            None,
        )
        if boundary is not None:
            impact = json.loads(boundary["impact_json"] or "{}")
            prior_gates = impact.get("prior_gate_event_ids") or {}
            if key in gate_events:
                gate_type, event = gate_events[key]
                prior_id = prior_gates.get(gate_type)
                if gate_type == "brief" and gate_type not in prior_gates:
                    prior_id = impact.get("prior_brief_gate_event_id")
                has_boundary_id = gate_type in prior_gates or (
                    gate_type == "brief" and "prior_brief_gate_event_id" in impact
                )
                gates[key] = bool(
                    gates[key] and event is not None
                    and has_boundary_id
                    and event["event_id"] != prior_id
                )
            elif key == "qa_passed":
                gates[key] = bool(candidate is not None)
            else:
                gates[key] = bool(candidate is not None and gates[key])
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
    _require_task(db, task_id)
    detail_json = _canonical_json(
        {"input_digest": input_digest, "details": details}
    )
    index = WORKFLOW_STAGES.index(stage_key)
    with (nullcontext(db.connection) if db.connection.in_transaction else db.transaction()) as connection:
        _seed_stage_states(connection, task_id)
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
    if "auto_qa" in downstream:
        # Persist the veto against exact render identities: marking stages
        # complete later must never revive approvals for the previous inputs.
        connection.execute(
            "INSERT OR IGNORE INTO render_invalidations(snapshot_id,stage_key,reason) "
            "SELECT snapshot_id,?,? FROM render_snapshots WHERE task_id=? AND stage='mix'",
            (stage_key, reason, task_id),
        )
        connection.execute(
            "INSERT INTO task_render_epochs(task_id,epoch) VALUES(?,1) "
            "ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1", (task_id,),
        )
        connection.execute(
            "UPDATE publish_records SET package_status='expired' "
            "WHERE task_id=? AND package_status='current'", (task_id,),
        )
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


def _latest_publish_status(db, task_id: str, *, publish_packages=None, verify_delivery_content=True) -> str | None:
    candidate = _latest_candidate(db, task_id)
    if candidate is None or not candidate.get("candidate_id"):
        return None
    row = db.connection.execute(
        """
        SELECT record_id,task_id,candidate_id,status,cover_path FROM publish_records
        WHERE task_id=? AND candidate_id=? AND package_status='current'
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id, candidate["candidate_id"]),
    ).fetchone()
    if row is None:
        return None
    if row["status"] == "cleared":
        from pathlib import Path
        from .publish_integrity import inspect_package

        folder = Path(row["cover_path"]).resolve().parent if row["cover_path"] else None
        checked = next((item for item in (publish_packages or [])
                        if item.get("record_id") == row["record_id"]
                        and item.get("task_id") == task_id
                        and item.get("candidate_id") == candidate["candidate_id"]), None)
        if checked is not None:
            if not checked.get("package_complete"):
                return "incomplete"
        else:
            inspection = inspect_package(db, row, folder, verify_content=verify_delivery_content)
            if not inspection["files"] or inspection["missing_files"] or inspection["changed_files"]:
                return "incomplete"
    return row["status"]


def _apply_trusted_facts(db, task_id: str, states: dict[str, dict], gates: dict, *, publish_packages=None, verify_delivery_content=True) -> None:
    if gates["brief_locked"]:
        # project_gates already requires a fresh approval after script rework.
        # Resolve only that boundary; preserved downstream results stay invalid.
        brief_state = states["brief_lock"]
        detail = brief_state.get("detail")
        if (brief_state["status"] == "ready" and isinstance(detail, dict)
                and str(detail.get("reason") or "").startswith("rework:")):
            brief_state["status"] = "completed"
        _mark_completed(states, WORKFLOW_STAGES[:2])

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
        packaging_state = states["packaging_confirm"]
        detail = packaging_state.get("detail")
        if (packaging_state["status"] == "ready" and isinstance(detail, dict)
                and str(detail.get("reason") or "").startswith("rework:")):
            # project_gates requires a new event before resolving this rework veto.
            packaging_state["status"] = "completed"
        else:
            _mark_completed(states, ("packaging_confirm",))

    candidate = _latest_candidate(db, task_id)
    if candidate is not None or _trusted_mix_exists(db, task_id):
        _mark_completed(states, ("fine_render",))
    if candidate is not None:
        _mark_completed(states, ("auto_qa",))
    if gates["human_reviewed"]:
        # This gate binds a new approval to the current QA, so it resolves
        # the prior rework veto rather than inheriting a historical approval.
        states["full_review"]["status"] = "completed"

    publish_status = _latest_publish_status(db, task_id, publish_packages=publish_packages, verify_delivery_content=verify_delivery_content)
    if publish_status is not None and not gates["human_reviewed"]:
        states["publish_package"]["status"] = "invalidated"
        states["publish_package"]["detail"] = {
            "reason": "当前候选缺少有效的完整审片批准；旧发布记录仅作历史保留"
        }
    elif publish_status == "incomplete":
        states["publish_package"]["status"] = "ready"
        states["publish_package"]["detail"] = {
            "reason": "当前交付包文件缺失或变化，请重新导出；已审片的成片保持不变"
        }
    elif publish_status == "cleared":
        publish_state = states["publish_package"]
        detail = publish_state.get("detail")
        reason = str(detail.get("reason", "")) if isinstance(detail, dict) else ""
        if reason.startswith("rework:") or reason == "confirmed packaging gate replaced":
            # A current-candidate package was verified above, and the current QA
            # has a valid full-watch approval. Reconfirming packaging replaces
            # the rework reason; new delivery resolves either known boundary.
            # Unrelated invalidations and expired packages remain blocked.
            publish_state["status"] = "completed"
            publish_state["detail"] = {}
        else:
            _mark_completed(states, ("publish_package",))
    elif publish_status == "blocked" and states["publish_package"]["status"] != "invalidated":
        states["publish_package"]["status"] = "blocked"
    elif publish_status == "draft" and states["publish_package"]["status"] != "invalidated":
        states["publish_package"]["status"] = "in_progress"


    if not gates['packaging_confirmed'] and not _packaging_preview_files_current(db, task_id):
        for stage in WORKFLOW_STAGES[WORKFLOW_STAGES.index('packaging_confirm'):]:
            if stage == 'packaging_confirm' or states[stage]['status'] == 'completed':
                states[stage]['status'] = 'invalidated'
                states[stage]['detail'] = {'reason': '包装确认依赖缺失或变化，请重新生成并观看包装预览'}

def _project_run_status(db, task_id: str, current_stage: str | None, *, brief_locked=False) -> str:
    # Terminal jobs from before an applied script revision remain in history,
    # including unknown costs. They must not cover the newly confirmed stage.
    # Active/paused jobs and ambiguous timestamps are deliberately retained.
    latest_job = db.connection.execute(
        """
        SELECT j.status,COALESCE(control.state,'active') AS control_state
        FROM jobs j
        LEFT JOIN job_controls control USING(job_id)
        WHERE j.task_id=? AND (?!='brief_lock' OR j.job_type!='initial_source_check')
          AND j.job_type NOT IN ('scene_detection','visual_analysis')
          AND NOT (
            ? AND j.status IN ('failed','interrupted','cancelled','stale')
            AND COALESCE(control.state,'active') != 'paused'
            AND j.job_type IN ('recommend_storyboard','tts','transcribe')
            AND EXISTS (
              SELECT 1 FROM rework_requests r
              WHERE r.task_id=j.task_id AND r.target_stage='brief_lock'
                AND r.status='applied'
                AND julianday(j.created_at) < julianday(r.created_at)
                AND (j.started_at IS NULL OR julianday(j.started_at) < julianday(r.created_at))
                AND NOT EXISTS (
                  SELECT 1 FROM job_attempts a WHERE a.job_id=j.job_id
                    AND (julianday(a.started_at) IS NULL
                         OR julianday(a.started_at) >= julianday(r.created_at))
                )
            )
          )
        ORDER BY j.updated_at DESC, j.created_at DESC, j.rowid DESC LIMIT 1
        """,
        (task_id, current_stage or "", brief_locked),
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


def project_workflow(db, task_id: str, *, publish_packages=None, verify_delivery_content=True) -> dict:
    """Return one stable projection of durable workflow and trusted history."""
    _require_task(db, task_id)
    gates = project_gates(db, task_id)
    states = _generic_states(db, task_id)
    _apply_trusted_facts(db, task_id, states, gates, publish_packages=publish_packages, verify_delivery_content=verify_delivery_content)
    route = db.connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()["task_type"]
    holding_storyboard = False
    if route in {"ad_mix", "digital_human"}:
        digest = storyboard_digest(db, task_id)
        if digest is not None:
            detail = states["ai_storyboard"].get("detail")
            content = detail.get("details") if isinstance(detail, dict) else None
            accepted = content.get("storyboard_accepted_digest") if isinstance(content, dict) else None
            if accepted != digest:
                states["ai_storyboard"]["status"] = "ready"
                holding_storyboard = True

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
    next_action = None if current_stage is None else _STAGE_ACTIONS[current_stage]
    run_status = _project_run_status(db, task_id, current_stage, brief_locked=gates["brief_locked"])
    if holding_storyboard and current_stage == "ai_storyboard":
        next_action = "confirm_storyboard"
        if run_status == "ready":
            run_status = "awaiting_user"
    return {
        "task_id": task_id,
        "current_stage": current_stage,
        "run_status": run_status,
        "next_action": next_action,
        "gates": gates,
        "stages": stages,
    }


def apply_transcript_attention(workflow: dict, route: str, transcript: dict | None) -> dict:
    """Project pending human pairing in the first visible step, without changing gates.

    A locked script is not a listening confirmation. Do not rewind historical
    downstream work or read its old transcription just to display a task list.
    """
    if (route != "digital_human"
            or workflow.get("current_stage") not in {"material_sync", "ai_storyboard"}
            or not transcript or transcript.get("status") not in {"pending", "stale", "needs_review"}):
        return workflow
    result = {**workflow, "current_stage": "brief_lock", "next_action": "lock_brief",
              "stages": [{**stage, "status": "ready"} if stage["stage_key"] == "brief_lock"
                         else dict(stage) for stage in workflow["stages"]]}
    if workflow.get("run_status") not in {"queued", "running", "failed", "interrupted"}:
        result["run_status"] = "awaiting_user"
    return result
