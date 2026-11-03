"""Delivery-only actions, independent of the renderer dependency fingerprint."""
import json
from pathlib import Path
from .ad_actions import _task_context, _confirmed_packaging
from .snapshots import latest_review_candidate
from .workflow import project_gates, complete_stage
from .task_sources import require_bound_source_readiness
from .publish import build_publish_package as create_publish_package
from .workbench_actions import _render_input_digest, _qa_rework_input_matches, _canonical_digest, _action_result


def build_ad_publish_package(db, job, project_root, runner):
    """Build the fixed release package from the approved ad review candidate."""
    require_bound_source_readiness(db, job.task_id)

    from .render_mix_adapter import verify_dependency_snapshot

    _ = runner
    root = Path(project_root).resolve()
    _task_root, task, _brief, _script, _digest = _task_context(db, job, root)
    candidate = latest_review_candidate(db, task["task_id"])
    if candidate is None or candidate.get("producer") != "assemble-ad-v1":
        raise RuntimeError("REVIEW_CANDIDATE_MISSING: 没有可信的广告审片候选")
    expected_candidate_id = str(
        job.payload.get("candidate_id") or candidate.get("candidate_id") or ""
    )
    if candidate.get("candidate_id") != expected_candidate_id:
        raise RuntimeError("PUBLISH_CANDIDATE_STALE: 发布任务绑定的候选已不是当前版本")
    dependencies = candidate.get("dependency_snapshot") or {}
    input_matches = dependencies.get("action_input_digest") == _render_input_digest(db, task["task_id"])
    if not input_matches:
        completed_rework = db.connection.execute(
            "SELECT r.rework_id,r.target_stage,r.source_candidate_id,r.result_candidate_id,"
            "c.rework_request_id AS candidate_rework_id FROM rework_requests r "
            "LEFT JOIN candidate_versions c ON c.candidate_id=? AND c.task_id=r.task_id "
            "WHERE r.task_id=? AND r.status='applied' "
            "ORDER BY r.updated_at DESC,r.rowid DESC LIMIT 1",
            (candidate["candidate_id"], task["task_id"]),
        ).fetchone()
        # Completing this candidate can change which applied request sorts first.
        # Accept that bookkeeping change only for this candidate's own rework (or
        # its QA-only recheck), while comparing every current input and epoch.
        input_matches = bool(completed_rework
            and completed_rework["result_candidate_id"] == candidate["candidate_id"]
            and (
                completed_rework["candidate_rework_id"] == completed_rework["rework_id"]
                or (completed_rework["target_stage"] == "auto_qa"
                    and completed_rework["source_candidate_id"] == candidate["candidate_id"])
            )
            and _qa_rework_input_matches(db, task["task_id"], dependencies.get("action_input_digest")))
    if not project_gates(db, task["task_id"])["picture_lock"] or not input_matches:
        raise RuntimeError("AD_RENDER_INPUT_CHANGED: 广告画面或包装已变化，必须重新渲染并审片")
    packaging, _bgm = _confirmed_packaging(db, task["task_id"], root)
    if dependencies.get("packaging") != packaging:
        raise RuntimeError("AD_RENDER_INPUT_CHANGED: 已确认包装与广告候选不一致，必须重新渲染并审片")
    # Runtime identities describe how the already-approved bytes were produced.
    # Export does not invoke that renderer. Only explicitly recorded runtime
    # entries are excluded; unclassified inputs and packaging files remain
    # protected, as do candidate/QA/approval signatures in create_publish_package.
    delivery_dependencies = dict(dependencies)
    # Render snapshots append runtime records after content records. Remove
    # only one occurrence per runtime record, from the end: one physical file
    # may also be a selected packaging input whose content must stay protected.
    content_entries = list(dependencies.get("files", []))
    for runtime_entry in reversed(dependencies.get("runtime_files") or []):
        for index in range(len(content_entries) - 1, -1, -1):
            if content_entries[index] == runtime_entry:
                del content_entries[index]
                break
    delivery_dependencies["files"] = content_entries
    if not verify_dependency_snapshot(delivery_dependencies):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 审片后广告依赖发生变化")
    row = db.connection.execute(
        "SELECT report_json FROM qa_reports WHERE report_id=? AND task_id=?",
        (candidate["qa_report_id"], task["task_id"]),
    ).fetchone()
    if row is None:
        raise RuntimeError("QA_REPORT_MISSING: 广告候选缺少 QA 报告")
    report = json.loads(row["report_json"])
    result = create_publish_package(
        db,
        task["task_id"],
        job.payload.get("platform"),
        job.payload.get("title"),
        job.payload.get("description"),
        list(job.payload.get("tags") or []),
        cover_choice=str(job.payload.get("cover_choice") or "candidate-1"),
        external_cover_path=job.payload.get("external_cover_path"),
        expected_external_cover_signature=job.payload.get("expected_external_cover_signature"),
        delivery_type=str(job.payload.get("delivery_type") or "standard"),
        output_root=job.payload.get("output_root"),
        trusted_roots=[root, Path(candidate["output_path"]).resolve().parent],
        video_path=candidate["output_path"],
        qa_report={"report_id": report["report_id"]},
        candidate_id=expected_candidate_id,
        retry_key=job.job_id,
    )
    complete_stage(
        db, task["task_id"], "publish_package",
        input_digest=_canonical_digest({
            "qa_snapshot_id": candidate["qa_snapshot_id"],
            "record_id": result["record_id"],
            "publish": {
                **({"platform": job.payload["platform"]} if job.payload.get("platform") is not None else {}),
                "title": job.payload.get("title"),
                "description": job.payload.get("description"),
                "tags": list(job.payload.get("tags") or []),
                "cover_choice": str(job.payload.get("cover_choice") or "candidate-1"),
                "external_cover_path": job.payload.get("external_cover_path"),
                "delivery_type": str(job.payload.get("delivery_type") or "standard"),
            },
        }),
        details={"record_id": result["record_id"]},
    )
    return _action_result(
        "build_publish_package:complete", "广告发布包生成完成",
        {**result, "reused": result.get("reused", False)},
    )
