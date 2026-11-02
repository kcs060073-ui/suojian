"""Two-stage digital-human render, QA, review and publish orchestration."""

import json

from pathlib import Path

from .candidate_versions import record_candidate_qa
from .publish import build_publish_package
from .qa import run_qa
from .qa_evidence import qa_evidence_complete
from .render_mix_adapter import (
    compile_render_mix_job, RenderMixJobError, run_render_mix,
    verify_catalog_snapshot, verify_dependency_snapshot,
)
from .reviews import matching_approval
from .snapshots import (
    file_content_signature,
    latest_review_candidate,
    latest_retryable_render_snapshot,
    record_qa_review_snapshot,
    record_render_dependency_snapshot,
    snapshot,
)
from .workflow import project_gates


def gate_status(db, task_id: str) -> dict:
    return project_gates(db, task_id)


def _result(task_id: str, mode: str, status: str, gates: dict, **extra) -> dict:
    result = {
        "task_id": task_id,
        "mode": mode,
        "status": status,
        "gates": gates,
        "go": False,
    }
    result.update(extra)
    return result


def _same_path(left, right) -> bool:
    if not left or not right:
        return False
    return Path(left).resolve() == Path(right).resolve()


def _render_qa_evidence(db, render_snapshot: dict) -> dict:
    dependency = render_snapshot.get("dependency_snapshot")
    ass_path = render_snapshot.get("ass_path")
    if dependency is None or ass_path is None:
        row = db.connection.execute(
            "SELECT params_json FROM render_snapshots WHERE snapshot_id=?",
            (render_snapshot["mix_snapshot_id"],),
        ).fetchone()
        if row is not None:
            try:
                params = json.loads(row["params_json"])
            except (TypeError, json.JSONDecodeError):
                params = {}
            dependency = dependency or params.get("dependency_snapshot")
            ass_path = ass_path or params.get("ass_path")
    return {
        "dependency_snapshot": dependency,
        "subtitle_path": ass_path,
        "shot_authorization": (
            dependency.get("shot_authorization", {})
            if isinstance(dependency, dict) else {}
        ),
    }


def _assemble_imported_video(db, task_id: str, video_path) -> dict:
    video = Path(video_path).resolve()
    if not video.is_file():
        raise FileNotFoundError(f"导入成片不存在：{video}")
    qa_result = run_qa(db, task_id, str(video))
    snapshot(
        db,
        task_id,
        "qa",
        {"mode": "imported_video_qa", "qa_report_id": qa_result["report_id"]},
        str(video),
    )
    return _result(
        task_id,
        "imported_video_qa",
        "imported_video_qa",
        gate_status(db, task_id),
        qa={
            "report_id": qa_result["report_id"],
            "status": qa_result["status"],
            "blocking": qa_result["blocking_count"],
        },
        output_path=str(video),
        message="外部已有视频已完成导入 QA；该模式不证明素材 ID 到成片的端到端闭环。",
    )


def _assemble_reviewed(
    db,
    task_id: str,
    *,
    output_path,
    ass_path,
    work_dir,
    platform: str,
    title: str | None,
    description: str | None,
    tags: list[str] | None,
    cover_choice: str,
    external_cover_path: str | Path | None,
    delivery_type: str,
    component_overrides: dict | None,
    expected_candidate_id: str | None,
) -> dict:
    gates = gate_status(db, task_id)
    candidate = latest_review_candidate(db, task_id)
    blocked_by = []
    if candidate is None:
        blocked_by.append("render_snapshot_missing")
        return _result(
            task_id,
            "render",
            "review_binding_invalid",
            gates,
            blocked_by=blocked_by,
            message="缺少绑定渲染输出、依赖和 QA 的审片快照，不能生成发布包。",
        )
    if expected_candidate_id is not None and candidate.get("candidate_id") != expected_candidate_id:
        raise RuntimeError("PUBLISH_CANDIDATE_STALE: 发布任务绑定的候选已不是当前版本")

    candidate_output = Path(candidate["output_path"]).resolve()
    requested_output = candidate_output if output_path is None else Path(output_path).resolve()
    if requested_output != candidate_output or not _same_path(
        candidate["qa_output_path"], candidate_output
    ):
        blocked_by.append("output_snapshot_mismatch")
    if not candidate_output.is_file():
        blocked_by.append("render_output_missing")
    else:
        current_output_signature = file_content_signature(candidate_output)
        if (
            current_output_signature != candidate["output_signature"]
            or candidate["qa_output_signature"] != candidate["output_signature"]
        ):
            blocked_by.append("render_output_changed")

    qa_report = None
    latest_qa = db.connection.execute(
        """
        SELECT report_id, video_path, status, report_json
        FROM qa_reports WHERE task_id=? ORDER BY rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if latest_qa is None or latest_qa["report_id"] != candidate["qa_report_id"]:
        blocked_by.append("qa_report_mismatch")
    else:
        if latest_qa["status"] != "passed":
            blocked_by.append("qa_passed")
        if not _same_path(latest_qa["video_path"], candidate_output):
            blocked_by.append("qa_output_mismatch")
        try:
            qa_report = json.loads(latest_qa["report_json"])
        except (TypeError, json.JSONDecodeError):
            blocked_by.append("qa_report_invalid")
        else:
            if (
                not isinstance(qa_report, dict)
                or qa_report.get("report_id") != latest_qa["report_id"]
                or qa_report.get("status") != latest_qa["status"]
                or not _same_path(qa_report.get("video_path"), candidate_output)
            ):
                blocked_by.append("qa_report_invalid")
    candidate_snapshot = candidate.get("dependency_snapshot") or {}
    if not verify_dependency_snapshot(candidate_snapshot):
        blocked_by.append("dependency_snapshot_changed")
    else:
        try:
            catalog_matches = verify_catalog_snapshot(
                db, task_id, candidate_output, candidate_snapshot,
            )
        except RenderMixJobError:
            catalog_matches = False
        if not catalog_matches:
            blocked_by.append("catalog_snapshot_changed")
    approval = matching_approval(
        db, task_id, candidate["qa_snapshot_id"], candidate["qa_report_id"]
    )
    if approval is None or approval["decision"] != "approved":
        blocked_by.append("review_approval_missing")

    if blocked_by:
        return _result(
            task_id,
            "render",
            "review_binding_invalid",
            gates,
            blocked_by=list(dict.fromkeys(blocked_by)),
            output_path=str(candidate_output),
            message="人工审片状态与渲染/QA/依赖快照不一致，不能生成发布包。",
        )

    package = build_publish_package(
        db,
        task_id,
        platform,
        title=title,
        description=description,
        tags=tags,
        cover_choice=cover_choice,
        external_cover_path=external_cover_path,
        delivery_type=delivery_type,
        component_overrides=component_overrides,
        trusted_roots=[work_dir, candidate_output.parent],
        video_path=candidate_output,
        qa_report=qa_report,
        candidate_id=candidate["candidate_id"],
    )
    result = _result(
        task_id,
        "render",
        "published" if package["status"] == "cleared" else "publish_blocked",
        gates,
        output_path=str(candidate_output),
        publish=package,
        message=(
            "已基于同一渲染、依赖快照和 QA 生成发布包。"
            if package["status"] == "cleared"
            else "发布包已生成，但合规检查阻断，不能发布。"
        ),
    )
    result["go"] = package["status"] == "cleared"
    return result


def _finish_render_qa(db, task_id: str, render_snapshot: dict) -> dict:
    """Run or rerun QA for immutable rendered bytes without rerendering them."""
    rendered = Path(render_snapshot["output_path"]).resolve()
    qa_result = run_qa(
        db,
        task_id,
        str(rendered),
        evidence=_render_qa_evidence(db, render_snapshot),
    )
    gates = gate_status(db, task_id)
    qa_summary = {
        "report_id": qa_result["report_id"],
        "status": qa_result["status"],
        "blocking": qa_result["blocking_count"],
    }
    evidence_complete = qa_evidence_complete(qa_result)
    if qa_result["status"] != "passed" or not evidence_complete:
        candidate = record_candidate_qa(
            db,
            task_id,
            render_snapshot,
            qa_result,
            eligible_for_review=False,
        )
        blocked_by = ["qa_passed"] if qa_result["status"] != "passed" else []
        if not evidence_complete:
            blocked_by.append("qa_video_evidence")
        return _result(
            task_id, "render", "qa_blocked", gates, blocked_by=blocked_by,
            qa=qa_summary, snapshots=render_snapshot, candidate=candidate,
            output_path=str(rendered),
            message="渲染完成但 QA 存在阻断，不能进入人工审片。",
        )
    candidate = record_qa_review_snapshot(db, task_id, render_snapshot, qa_result)
    candidate_version = record_candidate_qa(
        db,
        task_id,
        render_snapshot,
        qa_result,
        qa_snapshot_id=candidate["qa_snapshot_id"],
    )
    return _result(
        task_id, "render", "awaiting_human_review", gates,
        blocked_by=["human_reviewed"], qa=qa_summary,
        snapshots={**candidate, **candidate_version}, candidate=candidate_version,
        output_path=str(rendered),
        message="渲染和 QA 已完成，等待人工完整审片并提交绑定快照的审片审批。",
    )

def assemble(
    db,
    task_id: str,
    video_path: str | Path | None = None,
    platform: str = "默认",
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    *,
    cover_choice: str = "candidate-1",
    external_cover_path: str | Path | None = None,
    delivery_type: str = "standard",
    component_overrides: dict | None = None,
    render: bool = False,
    ass_path: str | Path | None = None,
    output_path: str | Path | None = None,
    work_dir: str | Path | None = None,
    runner=None,
    render_env: dict | None = None,
    subtitle_sources: dict | None = None,
    subtitle_runner=None,
    candidate_id: str | None = None,
) -> dict:
    """Run import-only QA or the two-stage catalog-backed render workflow."""
    if video_path is not None and render:
        raise ValueError("--video 与 --render 不能同时使用")
    if video_path is not None:
        return _assemble_imported_video(db, task_id, video_path)
    if not render:
        return _result(
            task_id,
            "none",
            "render_required",
            gate_status(db, task_id),
            blocked_by=["render_required"],
            message="请选择 --render 从素材 ID 生成成片，或 --video 仅导入已有视频做 QA。",
        )

    task = db.connection.execute(
        "SELECT status FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    work_root = Path(work_dir or Path(__file__).resolve().parents[1]).resolve()
    candidate = latest_review_candidate(db, task_id)
    if candidate is not None and (
        output_path is None or _same_path(output_path, candidate["output_path"])
    ):
        return _assemble_reviewed(
            db,
            task_id,
            output_path=output_path,
            ass_path=ass_path,
            work_dir=work_root,
            platform=platform,
            title=title,
            description=description,
            tags=tags,
            cover_choice=cover_choice,
            external_cover_path=external_cover_path,
            delivery_type=delivery_type,
            component_overrides=component_overrides,
            expected_candidate_id=candidate_id,
        )

    retry_snapshot = latest_retryable_render_snapshot(db, task_id)
    if retry_snapshot is not None:
        retry_output = Path(retry_snapshot["output_path"]).resolve()
        requested_output = retry_output if output_path is None else Path(output_path).resolve()
        if requested_output == retry_output and retry_output.is_file():
            if file_content_signature(retry_output) != retry_snapshot["output_signature"]:
                raise RuntimeError("RENDER_OUTPUT_CHANGED: QA 重试前成片内容已变化")
            if not verify_catalog_snapshot(
                db, task_id, retry_output, retry_snapshot["dependency_snapshot"],
            ):
                raise RuntimeError("CATALOG_SNAPSHOT_CHANGED: QA 重试前任务门禁或选择已变化")
            if not verify_dependency_snapshot(retry_snapshot["dependency_snapshot"]):
                raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: QA 重试前依赖内容已变化")
            return _finish_render_qa(db, task_id, retry_snapshot)

    if output_path is None:
        raise ValueError("首次 --render 必须显式提供 --output")
    job = compile_render_mix_job(
        db,
        task_id,
        work_root,
        Path(output_path),
        None if ass_path is None else Path(ass_path),
        subtitle_sources=subtitle_sources,
        subtitle_runner=subtitle_runner,
    )
    if not verify_dependency_snapshot(job["dependency_snapshot"]):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 渲染前依赖内容已变化")
    if runner is None:
        rendered = run_render_mix(job, env=render_env)
    else:
        rendered = run_render_mix(job, runner=runner, env=render_env)
    if not verify_dependency_snapshot(job["dependency_snapshot"]):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 渲染期间依赖内容发生变化")
    render_snapshot = record_render_dependency_snapshot(db, task_id, job)
    return _finish_render_qa(db, task_id, render_snapshot)
