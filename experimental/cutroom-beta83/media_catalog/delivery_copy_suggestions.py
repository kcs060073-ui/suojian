"""P11 release-copy requests and explicit candidate-bound adoption."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .candidate_versions import get_candidate, list_candidates
from .delivery_copy import (
    _insert_delivery_copy_draft, get_delivery_copy_draft,
)
from .model_contracts import ModelContext, validate_result
from .model_profiles import get_task_profile_binding_for_usage
from .reviews import matching_approval
from .task_script import read_task_script
from .tasks import get_task


def _digest(value: dict) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _record(ref: str, kind: str, version: str, text: str) -> dict:
    return {
        "ref": ref, "kind": kind, "version": version, "text": text,
        "media_uri": None, "orientation": None, "duration_us": None,
        "source_window": None,
    }


def prepare_delivery_copy_request(
    db, project_root: Path, task_id: str, candidate_id: str, request_id: str,
) -> dict:
    """Build a bounded P11 envelope only from the current approved candidate."""
    task = get_task(db, task_id)
    if task is None or task.get("task_type") not in {"ad_mix", "digital_human"}:
        raise ValueError("TASK_NOT_FOUND: 发布文字任务不存在")
    candidate = get_candidate(db, task_id, candidate_id)
    current = list_candidates(db, task_id)
    if candidate is None or not current or current[0]["candidate_id"] != candidate_id:
        raise RuntimeError("CANDIDATE_CHANGED: 当前成片版本已改变")
    if not candidate.get("qa_snapshot_id") or not candidate.get("qa_report_id"):
        raise RuntimeError("REVIEW_NOT_APPROVED: 当前成片尚未完成审片")
    approval = matching_approval(
        db, task_id, candidate["qa_snapshot_id"], candidate["qa_report_id"],
    )
    if approval is None or approval["decision"] != "approved":
        raise RuntimeError("REVIEW_NOT_APPROVED: 当前成片尚未获本人批准")

    script = read_task_script(Path(project_root), task)
    script_hash = hashlib.sha256((script or "").encode("utf-8")).hexdigest()
    recorded_hash = ((task.get("brief") or {}).get("final_script") or {}).get("sha256")
    if not script or script_hash != recorded_hash:
        raise RuntimeError("SCRIPT_CHANGED: 已确认文案文件不存在或已改变")
    if len(script) > 20_000:
        raise ValueError("P11_CONTEXT_TOO_LARGE: 本片文案超过发布建议上下文限制")

    draft = get_delivery_copy_draft(db, task_id, candidate_id)
    revision = 0 if draft is None else draft["revision"]
    records = [_record("S1", "script", script_hash, script)]
    copy_refs = []
    if draft is not None:
        copy_refs = ["C1"]
        records.append(_record(
            "C1", "copy", str(revision),
            json.dumps({
                "title": draft["title"], "description": draft["description"],
                "tags": draft["tags"],
            }, ensure_ascii=False, sort_keys=True),
        ))
    brief = task.get("brief") or {}
    orientation = (
        "landscape" if task["task_type"] == "ad_mix"
        and brief.get("aspect_ratio") == "16:9" else "portrait"
    )
    if task["task_type"] == "ad_mix" and brief.get("aspect_ratio") not in {"9:16", "16:9"}:
        raise RuntimeError("AD_ASPECT_RATIO_REQUIRED: 请先确认广告画幅")
    route = "ad" if task["task_type"] == "ad_mix" else "digital"
    digest = _digest({
        "taskId": task_id, "candidateId": candidate_id,
        "candidateInput": candidate["input_digest"],
        "candidateOutput": candidate["output_signature"],
        "qaSnapshotId": candidate["qa_snapshot_id"],
        "qaReportId": candidate["qa_report_id"],
        "scriptSha256": script_hash, "copyRevision": revision,
    })
    request = {
        "template_id": "P11", "schema_version": "1.0.0",
        "request_id": request_id, "input_digest": digest,
        "base_revision": revision, "route": route,
        "orientation": orientation, "allowed_operations": [],
        "locked_refs": [], "records": records,
        "payload": {
            "script_refs": ["S1"], "fact_refs": ["S1"],
            "existing_copy_refs": copy_refs, "user_request": None,
        },
    }
    context = ModelContext(
        template_id="P11", request_id=request_id, revision=revision,
        input_digest=digest, route=route, orientation=orientation,
        records={row["ref"]: row for row in records},
        approved_fact_refs=frozenset({"S1"}), schema_version="1.0.0",
    )
    return {"request": request, "context": context, "candidate": candidate}


def accept_delivery_copy_suggestion(
    db, project_root: Path, task_id: str, candidate_id: str, receipt_id: str,
    expected_revision: int, title_index: int, description: str, tags: list[str],
) -> dict:
    """Adopt one validated model draft after rechecking every source in one transaction."""
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            """SELECT r.*, d.receipt_id, d.response_json, d.state
               FROM model_result_drafts d
               JOIN model_contract_requests r USING(request_id)
               WHERE d.receipt_id=? AND r.task_id=? AND r.template_id='P11'""",
            (receipt_id, task_id),
        ).fetchone()
        if row is None or row["state"] != "draft":
            raise RuntimeError("SUGGESTION_UNAVAILABLE: 当前发布建议不存在或已失效")
        request = json.loads(row["request_json"])
        response = json.loads(row["response_json"])
        if response["status"] not in {"ok", "partial"} or response["result"] is None:
            raise RuntimeError("SUGGESTION_UNAVAILABLE: 当前建议尚未给出可用文字")
        current_draft = get_delivery_copy_draft(db, task_id, candidate_id)
        current_revision = 0 if current_draft is None else current_draft["revision"]
        if current_revision != expected_revision or current_revision != request["base_revision"]:
            raise RuntimeError("DELIVERY_DRAFT_CHANGED: 人工发布文字已改变")
        try:
            prepared = prepare_delivery_copy_request(
                db, project_root, task_id, candidate_id, request["request_id"],
            )
        except (RuntimeError, ValueError) as error:
            raise RuntimeError("STALE_INPUT: 当前成片或文案已改变") from error
        if prepared["request"] != request:
            raise RuntimeError("STALE_INPUT: 建议对应的输入已改变")
        validate_result(request, response, prepared["context"])
        titles = response["result"]["titles"]
        if type(title_index) is not int or not 0 <= title_index < len(titles):
            raise ValueError("SUGGESTION_TITLE_INVALID: 请选择实际返回的标题")
        binding = get_task_profile_binding_for_usage(db, task_id, "copywriting")
        if (binding is None or row["client_id"] != "provider:" + binding["profileId"]
                or not binding.get("model")):
            raise RuntimeError("SUGGESTION_SOURCE_INVALID: 模型来源无法核对")
        return _insert_delivery_copy_draft(
            connection, task_id, candidate_id,
            expected_revision=expected_revision, title=titles[title_index]["text"],
            description=description, tags=tags,
            source_kind="accepted_suggestion", source_ref=receipt_id,
            model_version=binding["model"],
        )
