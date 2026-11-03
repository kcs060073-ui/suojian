"""Explicit, idempotent entry to P11 release-copy model work."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.error import URLError

from .delivery_copy_suggestions import prepare_delivery_copy_request
from .model_profile_probe import _request_json
from .model_profiles import get_task_profile_binding_for_usage
from .model_requests import register_request, submit_result
from .model_runtime import task_model_environment
from .provider_endpoints import provider_endpoint
from .tasks import get_task
from .workbench_worker import ProviderOutcomeUnknown, enqueue_job


def approved_p11_prompt(project_root: Path) -> tuple[str, str]:
    """Keep the review-v2 draft inactive until its registry records approval."""
    folder = Path(project_root) / "docs/ux/final-2026-09-24/readiness"
    try:
        registry = json.loads((folder / "contracts/prompt-registry.json").read_text(
            encoding="utf-8",
        ))
    except (OSError, ValueError) as error:
        raise RuntimeError("P11_REVIEW_REQUIRED: 发布文字提示词审核记录不可用") from error
    item = next((entry for entry in registry if entry["id"] == "P11"), None)
    if item is None or item.get("status") != "approved":
        raise RuntimeError("P11_REVIEW_REQUIRED: 发布文字提示词尚待审核")
    try:
        prompt = (folder / "prompts/P11.txt").read_bytes().replace(b"\r\n", b"\n")
        hashes = json.loads((folder / "contracts/prompt-hashes.json").read_text(
            encoding="utf-8",
        ))
    except (OSError, ValueError) as error:
        raise RuntimeError("P11_REVIEW_REQUIRED: 发布文字提示词或摘要不可用") from error
    digest = hashlib.sha256(prompt).hexdigest()
    if hashes.get("P11.txt") != digest or item.get("schema_version") != "1.0.0":
        raise RuntimeError("P11_PROMPT_CHANGED: 发布文字提示词或契约版本已改变")
    return item["prompt_version"], digest


def delivery_copy_availability(project_root: Path, orchestration_mode: str) -> dict:
    """Expose the same release gate before showing a paid action."""
    if orchestration_mode != "standalone":
        return {"enabled": False, "reason": "当前模式暂不支持生成发布文案，可手动编辑。"}
    try:
        approved_p11_prompt(project_root)
    except RuntimeError:
        return {"enabled": False, "reason": "本版本暂未开放 AI 发布文案，可手动编辑；不影响成片导出。"}
    return {"enabled": True, "reason": ""}


def queue_delivery_copy_suggestion(
    db, project_root: Path, task_id: str, candidate_id: str, *,
    request_id: str, confirm_paid_call: bool,
) -> dict:
    """Only an explicit approved click queues one potentially paid P11 request."""
    prompt_version, prompt_sha256 = approved_p11_prompt(project_root)
    task = get_task(db, task_id)
    if task is None:
        raise KeyError("TASK_NOT_FOUND: 任务不存在")
    if task.get("orchestration_mode") != "standalone":
        raise RuntimeError("P11_AGENT_CONTEXT_APPROVAL_REQUIRED: Agent 发布文字上下文尚未获准传送")
    prepared = prepare_delivery_copy_request(
        db, project_root, task_id, candidate_id, request_id,
    )
    request = prepared["request"]
    if confirm_paid_call is not True:
        raise ValueError("PAID_CONFIRMATION_REQUIRED: 请先确认本次模型调用可能收费")
    binding = get_task_profile_binding_for_usage(db, task_id, "copywriting")
    if binding is None or not binding.get("model"):
        raise RuntimeError("MODEL_BINDING_MISSING: 当前任务未配置文案模型")
    client_id = "provider:" + binding["profileId"]
    register_request(
        db, task_id=task_id, request=request, context=prepared["context"],
        client_id=client_id, prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        expires_at=time.time() + 3600,
    )
    job_id = enqueue_job(
        db, job_id="p11-" + uuid.uuid4().hex,
        job_type="delivery_copy_suggestion", task_id=task_id,
        payload={
            "task_id": task_id, "candidate_id": candidate_id,
            "request_id": request_id, "profile_id": binding["profileId"],
            "profile_digest": binding["configDigest"],
            "model_version": binding["model"],
            "prompt_version": prompt_version, "prompt_sha256": prompt_sha256,
        },
        cost_status="approved",
        provider_request_id=f"p11:{task_id}:{request_id}",
    )
    return {"jobId": job_id, "requestId": request_id}


def saved_delivery_copy_result(connection, task_id: str, payload: dict) -> dict | None:
    """Recover only a validated receipt belonging to this frozen P11 request."""
    row = connection.execute(
        """SELECT d.receipt_id,d.state FROM model_result_drafts d
           JOIN model_contract_requests r USING(request_id)
           WHERE r.request_id=? AND r.task_id=? AND r.template_id='P11'
             AND r.client_id=? AND r.prompt_version=? AND r.prompt_sha256=?""",
        (payload.get("request_id"), task_id,
         "provider:" + str(payload.get("profile_id", "")),
         payload.get("prompt_version"), payload.get("prompt_sha256")),
    ).fetchone()
    if row is None or payload.get("task_id") != task_id:
        return None
    return {
        "cursor": "delivery_copy_suggestion:complete",
        "progress": {"current": 1, "total": 1},
        "phase_label": "发布文字建议已返回",
        "result": {"requestId": payload["request_id"],
                   "receiptId": row["receipt_id"], "state": row["state"]},
    }


def run_delivery_copy_suggestion(db, job, project_root: Path) -> dict:
    """Run a frozen P11 request once; retain unknown provider outcomes for review."""
    payload = job.payload
    task_id = job.task_id
    if not task_id or payload.get("task_id") != task_id:
        raise ValueError("TASK_ID_MISMATCH: 发布文字作业任务不一致")
    saved = saved_delivery_copy_result(db.connection, task_id, payload)
    if saved is not None:
        return saved
    request_id = payload["request_id"]
    candidate_id = payload["candidate_id"]
    prompt_version, prompt_sha256 = approved_p11_prompt(project_root)
    if (prompt_version, prompt_sha256) != (
        payload["prompt_version"], payload["prompt_sha256"],
    ):
        raise RuntimeError("P11_PROMPT_CHANGED: 发布文字提示词版本已改变")
    binding = get_task_profile_binding_for_usage(db, task_id, "copywriting")
    if binding is None or (
        binding["profileId"], binding["configDigest"],
    ) != (payload["profile_id"], payload["profile_digest"]):
        raise RuntimeError("MODEL_BINDING_CHANGED: 当前任务文案模型绑定已改变")
    row = db.connection.execute(
        "SELECT request_json,client_id FROM model_contract_requests WHERE request_id=? AND task_id=?",
        (request_id, task_id),
    ).fetchone()
    if row is None or row["client_id"] != "provider:" + binding["profileId"]:
        raise RuntimeError("MODEL_REQUEST_MISSING: 发布文字模型请求不存在")
    request = json.loads(row["request_json"])
    prepared = prepare_delivery_copy_request(
        db, project_root, task_id, candidate_id, request_id,
    )
    if prepared["request"] != request:
        raise RuntimeError("STALE_INPUT: 发布文字请求的候选或稿件已改变")

    environment, snapshot = task_model_environment(
        db, task_id, "copywriting",
    )
    if (
        snapshot["profileId"], snapshot["configDigest"],
    ) != (payload["profile_id"], payload["profile_digest"]):
        raise RuntimeError("MODEL_BINDING_CHANGED: 文案模型运行快照已改变")
    prompt = (
        Path(project_root) / "docs/ux/final-2026-09-24/readiness/prompts/P11.txt"
    ).read_text(encoding="utf-8")
    provider_payload = {
        "model": environment["ARK_MODEL"],
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(
                request, ensure_ascii=False, sort_keys=True,
            )},
        ],
        "max_tokens": 1000,
        "response_format": {"type": "json_object"},
    }
    try:
        _, wire = _request_json(
            provider_endpoint(environment["ARK_BASE_URL"], "chat/completions"),
            environment["ARK_API_KEY"], payload=provider_payload, timeout=45.0,
        )
    except (TimeoutError, URLError) as error:
        raise ProviderOutcomeUnknown(
            "发布文字请求可能已被供应商接收，需按原请求核对结果",
            provider_state="p11_network_unknown",
        ) from error
    try:
        envelope = json.loads(wire)
        choice = envelope["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
            raise ValueError("incomplete")
        response = json.loads(choice["message"]["content"])
    except (KeyError, ValueError, TypeError, IndexError) as error:
        raise RuntimeError("MODEL_RESPONSE_INVALID: 供应商未返回完整 P11 JSON") from error
    receipt = submit_result(
        db, request_id=request_id, response=response,
        client_id=row["client_id"],
        resolve_context=lambda current_db, current_task, _request:
            prepare_delivery_copy_request(
                current_db, project_root, current_task, candidate_id, request_id,
            )["context"],
    )
    return {
        "cursor": "delivery_copy_suggestion:complete",
        "progress": {"current": 1, "total": 1},
        "phase_label": "发布文字建议已返回",
        "result": {
            "requestId": request_id, "receiptId": receipt["receipt_id"],
            "state": receipt["state"],
        },
    }


def latest_delivery_copy_suggestion(
    db, project_root: Path, task_id: str, candidate_id: str,
) -> dict:
    """Project the newest P11 job for one candidate without starting work."""
    job = db.connection.execute(
        """SELECT j.job_id,j.status,j.error_summary,p.outcome_status,
                  p.payload_json
           FROM jobs j JOIN job_payloads p USING(job_id)
           WHERE j.task_id=? AND j.job_type='delivery_copy_suggestion'
             AND json_extract(p.payload_json,'$.candidate_id')=?
           ORDER BY j.rowid DESC LIMIT 1""",
        (task_id, candidate_id),
    ).fetchone()
    if job is None:
        return {"status": "not_requested", "jobId": None, "receiptId": None}
    payload = json.loads(job["payload_json"])
    model_version = payload.get("model_version")
    result = {
        "status": (
            "outcome_unknown" if job["outcome_status"] == "unknown"
            else job["status"]
        ),
        "jobId": job["job_id"],
        "requestId": payload["request_id"],
        "receiptId": None,
        "promptVersion": payload["prompt_version"],
        "modelProfileId": payload["profile_id"],
        "modelVersion": model_version,
        "problem": job["error_summary"],
    }
    row = db.connection.execute(
        """SELECT d.receipt_id,d.response_json,d.state,r.request_json
           FROM model_result_drafts d
           JOIN model_contract_requests r USING(request_id)
           WHERE d.request_id=? AND r.task_id=?""",
        (payload["request_id"], task_id),
    ).fetchone()
    if row is None:
        return result
    response = json.loads(row["response_json"])
    state = row["state"]
    if state == "draft":
        try:
            current = prepare_delivery_copy_request(
                db, project_root, task_id, candidate_id, payload["request_id"],
            )
            if current["request"] != json.loads(row["request_json"]):
                state = "stale"
        except (RuntimeError, ValueError):
            state = "stale"
    return {
        **result,
        "status": state,
        "receiptId": row["receipt_id"],
        "result": response["result"],
        "unknowns": response["unknowns"],
        "clarifications": response["clarifications"],
    }
