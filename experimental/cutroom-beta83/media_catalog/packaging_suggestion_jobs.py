"""Explicit P09 jobs; no library-order fallback and no implicit paid retries."""
import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.error import URLError

from .packaging_recommendations import prepare_packaging_request, materialize_packaging_suggestion
from .model_profile_probe import _request_json
from .model_profiles import get_task_profile_binding_for_usage
from .model_requests import register_request, submit_result
from .model_runtime import task_model_environment
from .provider_endpoints import provider_endpoint
from .tasks import get_task
from .workbench_worker import ProviderOutcomeUnknown, enqueue_job


def prompt_text(project_root):
    return (Path(project_root)/"docs/ux/final-2026-09-24/readiness/prompts/P09.txt").read_text(encoding="utf-8")


def execution_prompt(project_root, version):
    base=prompt_text(project_root)
    from .packaging_execution_contract import VERSION,prompt_body
    return prompt_body(base) if version.endswith("/"+VERSION) else base


def approved_p09_prompt(project_root, *, execution_confirmation=None, for_confirmation=False):
    folder = Path(project_root) / "docs/ux/final-2026-09-24/readiness"
    try:
        registry = json.loads((folder / "contracts/prompt-registry.json").read_text(encoding="utf-8"))
        item = next((x for x in registry if x["id"] == "P09"), None)
        if item is None:
            raise RuntimeError("P09_REVIEW_REQUIRED: 智能包装提示词待审核，可继续手动设置")
        value = prompt_text(project_root).replace("\r\n", "\n").encode()
        hashes = json.loads((folder / "contracts/prompt-hashes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("P09_REVIEW_REQUIRED: 智能包装提示词审核记录不可用") from error
    sha = hashlib.sha256(value).hexdigest()
    if hashes.get("P09.txt") not in {sha, hashlib.sha256(value.replace(b"\n", b"\r\n")).hexdigest()} or item.get("schema_version") != "1.0.0":
        raise RuntimeError("P09_PROMPT_CHANGED: 包装提示词或输出合同已改变")
    from .packaging_execution_contract import VERSION, prompt_sha256
    effective=(item["prompt_version"]+"/"+VERSION,prompt_sha256(prompt_text(project_root)))
    if execution_confirmation is not None:
        shown=(execution_confirmation.get("prompt_version"),execution_confirmation.get("prompt_sha256"))
        if shown == effective:return effective
        if shown != (item["prompt_version"],sha):
            raise RuntimeError("P09_PROMPT_CHANGED: 页面展示的包装协议已变化，请刷新后重新确认本次调用")
        # Previously authorized requests keep their exact old prompt. New UI uses effective.
        return item["prompt_version"],sha
    if for_confirmation:
        return effective
    if item.get("status") != "approved":
        # Runtime user receipt approves exact frozen bytes; the prototype stays immutable.
        try:
            approval = json.loads((Path(project_root) / "runtime/workbench/approvals/P09.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            approval = {}
        expected = {"template_id": "P09", "prompt_version": item["prompt_version"],
                    "schema_version": "1.0.0", "prompt_sha256": sha,
                    "status": "approved", "approved_by": "user"}
        if not isinstance(approval, dict) or any(approval.get(key) != value for key, value in expected.items()) or not approval.get("approved_at"):
            raise RuntimeError("P09_REVIEW_REQUIRED: 智能包装提示词待审核，可继续手动设置")
    return item["prompt_version"], sha


def queue_packaging_suggestion(db, project_root, task_id, *, style, request_id, confirm_paid_call, base_plan_id=None, expected_visual_profile_id=None, expected_visual_digest=None, execution_mode=None, expected_prompt_version=None, expected_prompt_sha256=None, requested_roles=None):
    from .packaging_recommendations import validate_requested_roles
    validate_requested_roles(requested_roles)
    confirmation=None if expected_prompt_version is None and expected_prompt_sha256 is None else {"prompt_version":expected_prompt_version,"prompt_sha256":expected_prompt_sha256}
    if confirm_paid_call is not True:
        raise ValueError("PAID_CONFIRMATION_REQUIRED: 请确认本次包装建议调用可能收费")
    version,sha=approved_p09_prompt(project_root,execution_confirmation=confirmation)
    task=get_task(db,task_id)
    if task is None:raise KeyError("TASK_NOT_FOUND: 任务不存在")
    if execution_mode not in {None,"agent","builtin_visual"}:
        raise ValueError("PACKAGING_EXECUTION_MODE_INVALID: 包装建议执行方式无效")
    if task.get("orchestration_mode") == "agent" and execution_mode != "builtin_visual":
        if requested_roles is not None:
            raise ValueError("PACKAGING_SCOPED_AGENT_UNSUPPORTED: 纯文字Agent暂不支持单类追加，请使用整套建议或手动追加")
        from .packaging_agent_requests import queue_agent_request
        return queue_agent_request(db, project_root, task_id, request_id, style, base_plan_id, (version,sha))
    prepared=prepare_packaging_request(db,project_root,task_id,request_id,style,base_plan_id=base_plan_id,requested_roles=requested_roles)
    visual_mode = bool(prepared["visual"]["frames"])
    if execution_mode == "builtin_visual" and not visual_mode:
        raise RuntimeError("PACKAGING_VISUAL_INPUT_REQUIRED: 本次内置视觉建议没有可用源帧")
    if visual_mode:
        from .packaging_visual import verified_binding
        binding = verified_binding(db, task_id)
        if ((expected_visual_profile_id is not None and binding["profileId"] != expected_visual_profile_id)
                or (expected_visual_digest is not None and binding.get("visualDigest") != expected_visual_digest)):
            raise RuntimeError("MODEL_BINDING_CHANGED: 视觉模型已改变，请刷新后重新确认费用")
    else:
        binding=get_task_profile_binding_for_usage(db,task_id,"copywriting")
    if not binding or not binding.get("model"):
        raise RuntimeError("MODEL_BINDING_MISSING: 本任务尚未绑定文案模型")
    registration=register_request(db,task_id=task_id,request=prepared["request"],context=prepared["context"],
        client_id="provider:"+binding["profileId"],prompt_version=version,prompt_sha256=sha,expires_at=time.time()+3600)
    authorization = ({"kind":"technical_model_execution","task_id":task_id,"request_id":request_id,
        "prompt_version":version,"prompt_sha256":sha,"profile_id":binding["profileId"],
        "profile_digest":binding["configDigest"],"visual_digest":binding.get("visualDigest"),
        "input_digest":prepared["request"]["input_digest"],"confirmed_by":"explicit_user_action"} if confirmation else None)
    if authorization is not None and visual_mode:
        from .packaging_agent_requests import response_schema
        from .packaging_execution_contract import system_prompt_sha256
        authorization["effective_system_sha256"]=system_prompt_sha256(execution_prompt(project_root,version),response_schema(prepared))
    job_id=enqueue_job(db,job_id="p09-"+uuid.uuid4().hex,job_type="packaging_suggestion",task_id=task_id,
        payload={"task_id":task_id,"request_id":request_id,"style":style,"execution_mode":execution_mode,"queued_at":registration["expires_at"]-3600,
        "base_plan_id":base_plan_id,"requested_roles":requested_roles,"visual_mode":visual_mode,"visual_digest":binding.get("visualDigest"),"profile_id":binding["profileId"],"profile_digest":binding["configDigest"],"model_version":binding["model"],
        "prompt_version":version,"prompt_sha256":sha,"execution_authorization":authorization},cost_status="approved",provider_request_id=f"p09:{task_id}:{request_id}")
    return {"jobId":job_id,"requestId":request_id}


def run_packaging_suggestion(db, job, project_root):
    p=job.payload;task_id=job.task_id
    if not task_id or p.get("task_id")!=task_id:raise ValueError("TASK_ID_MISMATCH: 包装作业任务不一致")
    authorization=p.get("execution_authorization")
    if authorization is not None:
        required={"kind":"technical_model_execution","task_id":task_id,"request_id":p["request_id"],
            "prompt_version":p["prompt_version"],"prompt_sha256":p["prompt_sha256"],
            "profile_id":p["profile_id"],"profile_digest":p["profile_digest"],
            "visual_digest":p.get("visual_digest"),"confirmed_by":"explicit_user_action"}
        if not isinstance(authorization,dict) or any(authorization.get(k)!=v for k,v in required.items()):
            raise RuntimeError("P09_EXECUTION_AUTHORIZATION_CHANGED: 本次执行许可与任务或模型绑定不一致")
    if approved_p09_prompt(project_root,execution_confirmation=authorization)!=(p["prompt_version"],p["prompt_sha256"]):
        raise RuntimeError("P09_PROMPT_CHANGED: 包装提示词已改变")
    if p.get("visual_mode"):
        from .packaging_visual import verified_binding
        binding = verified_binding(db, task_id)
        if binding.get("visualDigest") != p.get("visual_digest"):
            raise RuntimeError("MODEL_BINDING_CHANGED: 视觉协议配置已改变")
    else:
        binding=get_task_profile_binding_for_usage(db,task_id,"copywriting")
    if not binding or (binding["profileId"],binding["configDigest"])!=(p["profile_id"],p["profile_digest"]):
        raise RuntimeError("MODEL_BINDING_CHANGED: 包装建议模型已改变")
    row=db.connection.execute("SELECT request_json,client_id,expires_at FROM model_contract_requests WHERE request_id=? AND task_id=?",(p["request_id"],task_id)).fetchone()
    if row is None or row["client_id"]!="provider:"+binding["profileId"]:
        raise RuntimeError("MODEL_REQUEST_MISSING: 包装请求不存在")
    if time.time() >= row["expires_at"]:
        raise RuntimeError("MODEL_REQUEST_EXPIRED: 包装请求已过期，未调用模型；请重新生成")
    request=json.loads(row["request_json"])
    if authorization is not None and authorization.get("input_digest")!=request["input_digest"]:
        raise RuntimeError("P09_EXECUTION_AUTHORIZATION_CHANGED: 本次执行许可与输入不一致")
    frozen_prompt=execution_prompt(project_root,p["prompt_version"])
    if authorization is not None:
        from .packaging_execution_contract import VERSION,system_prompt_sha256
        if p["prompt_version"].endswith("/"+VERSION) and system_prompt_sha256(frozen_prompt)!=p["prompt_sha256"]:
            raise RuntimeError("P09_PROMPT_CHANGED: 实际发送协议与本次许可不一致")
    prepared=prepare_packaging_request(db,project_root,task_id,p["request_id"],p["style"],base_plan_id=p.get("base_plan_id"),requested_roles=p.get("requested_roles"))
    if prepared["request"]!=request:raise RuntimeError("STALE_INPUT: 文案、镜头或人工包装已改变")
    if p.get("visual_mode"):
        from .packaging_visual import request_visual_suggestion
        from .packaging_agent_requests import response_schema
        schema=response_schema(prepared)
        if authorization is not None and authorization.get("effective_system_sha256"):
            from .packaging_execution_contract import system_prompt_sha256
            if system_prompt_sha256(frozen_prompt,schema)!=authorization["effective_system_sha256"]:
                raise RuntimeError("P09_PROMPT_CHANGED: 实际视觉执行合同与本次许可不一致")
        response = request_visual_suggestion(db, binding, prepared, frozen_prompt, schema, task_id=task_id)
    else:
        env,snapshot=task_model_environment(db,task_id,"copywriting")
        if (snapshot["profileId"],snapshot["configDigest"])!=(p["profile_id"],p["profile_digest"]):
            raise RuntimeError("MODEL_BINDING_CHANGED: 模型运行快照改变")
        # The registered schema is supplied as a serialization contract, not model authority.
        schemas=json.loads((Path(__file__).parent/"contracts/model-schemas-v1.json").read_text(encoding="utf-8"))
        provider_payload={"model":env["ARK_MODEL"],"messages":[
            {"role":"system","content":frozen_prompt},
            {"role":"user","content":json.dumps({"request":request,"response_schema":{"$ref":"#/$defs/P09Output","$defs":{k:v for k,v in schemas["$defs"].items() if k in {"P09Output","text","anchor","issue"}}}},ensure_ascii=False,sort_keys=True)}],
            "max_tokens":4000,"response_format":{"type":"json_object"}}
        try:
            _,wire=_request_json(provider_endpoint(env["ARK_BASE_URL"],"chat/completions"),env["ARK_API_KEY"],payload=provider_payload,timeout=60.0)
        except (TimeoutError,URLError) as error:
            raise ProviderOutcomeUnknown("包装建议请求可能已被接收，请核对原请求；不会自动重发",provider_state="p09_network_unknown") from error
        try:
            envelope=json.loads(wire);choice=envelope["choices"][0]
            if choice.get("finish_reason")!="stop" or choice["message"].get("refusal"):raise ValueError("incomplete")
            response=json.loads(choice["message"]["content"])
        except (KeyError,ValueError,TypeError,IndexError) as error:
            raise RuntimeError("MODEL_RESPONSE_INVALID: 未返回完整包装建议JSON") from error
    # Keep the received result before local validation/transactions, so a local
    # rejection can be diagnosed without another paid provider request.
    from hashlib import sha256
    audit_dir = Path(db.path).parent / "packaging-responses"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / (sha256(p["request_id"].encode()).hexdigest() + ".json")
    temporary = audit_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"requestId": p["request_id"], "taskId": task_id,
        "inputDigest": request["input_digest"], "response": response},
        ensure_ascii=False), encoding="utf-8")
    temporary.replace(audit_path)
    # Complete response is known. Deterministic semantic rejection is not an
    # unknown network outcome, and it must not spend minutes inside a write lock.
    if response.get("status") in {"ok","partial"}:
        from .packaging_recommendations import _resolve
        from .workbench_worker import ProviderOutcomeConfirmedFailure
        try:_resolve(prepared,response)
        except (ValueError,RuntimeError) as error:
            raise ProviderOutcomeConfirmedFailure(str(error),provider_state="p09_semantic_rejected",cost_status="incurred") from error
    try:
        # Warm content-bound local probes after the network call and before the write
        # lock. The full authoritative context is still rebuilt under the lock below.
        prepare_packaging_request(db,project_root,task_id,p["request_id"],p["style"],base_plan_id=p.get("base_plan_id"),requested_roles=p.get("requested_roles"))
        with db.transaction(immediate=True) as conn:
            if p.get("visual_mode"):
                from .packaging_visual import assert_visual_binding
                try:
                    assert_visual_binding(db, task_id, binding)
                except Exception as error:
                    from .workbench_worker import ProviderOutcomeConfirmedFailure
                    raise ProviderOutcomeConfirmedFailure(str(error), provider_state="binding_changed", cost_status="incurred") from error
            current=prepare_packaging_request(db,project_root,task_id,p["request_id"],p["style"],base_plan_id=p.get("base_plan_id"),requested_roles=p.get("requested_roles"))
            receipt=submit_result(db,request_id=p["request_id"],response=response,client_id=row["client_id"],connection=conn,
                resolve_context=lambda *_:current["context"])
            result={"requestId":p["request_id"],"receiptId":receipt["receipt_id"],"state":receipt["state"]}
            if receipt["state"]=="draft" and response["status"] in {"ok","partial"}:
                from .packaging_recommendations import _materialize_verified_suggestion
                result.update(_materialize_verified_suggestion(db,task_id,receipt["receipt_id"],p["style"],current,connection=conn))
    except (ValueError, RuntimeError) as error:
        from .workbench_worker import ProviderOutcomeConfirmedFailure
        if isinstance(error, ProviderOutcomeConfirmedFailure):
            raise
        raise ProviderOutcomeConfirmedFailure(str(error),
            provider_state="p09_local_rejected", cost_status="incurred") from error
    return {"cursor":"packaging_suggestion:complete","progress":{"current":1,"total":1},"phase_label":"本片包装建议已返回","result":result}


def recommendation_metadata(db, plan_id):
    connection=getattr(db,"connection",db)
    row=connection.execute("SELECT l.receipt_id,r.prompt_version,r.client_id,r.input_digest,r.request_json,d.response_json FROM packaging_recommendation_links l JOIN model_result_drafts d USING(receipt_id) JOIN model_contract_requests r USING(request_id) WHERE l.plan_id=?",(plan_id,)).fetchone()
    if row is None:return None
    response=json.loads(row["response_json"])
    request=json.loads(row["request_json"])
    timeline=json.loads(next(x["text"] for x in request["records"] if x["ref"] == "L1"))
    visual=timeline.get("visual_evidence", {"status":"insufficient_evidence", "frames":[], "limitations":["旧建议没有真实画面输入"]})
    images={x["ref"]:x.get("media_uri") for x in request["records"] if x["ref"].startswith("frame:")}
    visual={**visual,"frames":[{**f,"media_uri":images.get(f["ref"])} for f in visual["frames"]]}
    return {"visualEvidence":visual,"evidenceRefs":response.get("evidence_refs", []),"receiptId":row["receipt_id"],"promptVersion":row["prompt_version"],"inputDigest":row["input_digest"],
        "reasons":[{"kind":x["kind"],"assetRef":x["asset_ref"],"reason":x["reason"]} for x in response["result"]["instances"]],
        "omittedReasons":response["result"]["omitted_reasons"],"unknowns":response["unknowns"],"clarifications":response["clarifications"],"issues":response["result"]["issues"]}


def packaging_suggestion_availability(db, project_root, task_id):
    availability = _new_suggestion_availability(db, project_root, task_id)
    state = packaging_suggestion_state(db, task_id)
    if state.get("savedDraftAvailable"):
        availability["savedDraft"] = {key: state[key] for key in
            ("planId", "receiptId", "inputDigest", "requestId", "jobId")}
    return availability


def _new_suggestion_availability(db, project_root, task_id):
    try:
        version,sha=approved_p09_prompt(project_root,for_confirmation=True)
        prompt={"promptVersion":version,"promptSha256":sha}
        task = get_task(db, task_id)
        from .packaging_visual import _windows, verified_binding
        windows, _, _ = _windows(db, task) if task else ([], [], 0)
        if windows:
            binding=verified_binding(db, task_id)
            return {**prompt,"available": True, "visual": True, "executionMode":"builtin_visual", "visualProfileId":binding["profileId"],
                "visualDigest":binding.get("visualDigest"), "modelName":binding.get("displayName") or binding["model"],
                "model":binding["model"], "message": "将发送最多8张锁定源帧生成本片视觉包装建议。"}
        if task and task.get("orchestration_mode") == "agent":
            return {**prompt,"available": True, "mode": "agent", "message": "没有可用源帧，本次仅提供文字依据。"}
        binding = get_task_profile_binding_for_usage(db, task_id, "copywriting")
        if not binding or not binding.get("model"):
            return {**prompt,"available": False, "message": "请先为本任务绑定文案模型。"}
    except (RuntimeError, ValueError) as error:
        code = str(error).split(":", 1)[0]
        if code in {"PACKAGING_VISUAL_UNAVAILABLE", "MODEL_BINDING_CHANGED"}:
            from .packaging_visual import visual_recovery
            return {"available": False, **visual_recovery(db, task_id)}
        return {"available": False, "code": code, "message": str(error).split(": ", 1)[-1]}
    return {**prompt,"available": True, "message": None}


def packaging_suggestion_state(db, task_id):
    task = get_task(db, task_id)
    row=db.connection.execute("SELECT j.job_id,j.status,j.error_summary,p.outcome_status,p.payload_json FROM jobs j JOIN job_payloads p USING(job_id) WHERE j.task_id=? AND j.job_type='packaging_suggestion' ORDER BY j.rowid DESC LIMIT 1",(task_id,)).fetchone()
    payload=json.loads(row["payload_json"]) if row else {}
    if task and task.get("orchestration_mode") == "agent":
        from .packaging_agent_requests import agent_request_state
        agent=db.connection.execute("SELECT expires_at FROM packaging_agent_requests WHERE task_id=? ORDER BY rowid DESC LIMIT 1",(task_id,)).fetchone()
        if (payload.get("execution_mode") != "builtin_visual"
                or (agent and agent["expires_at"]-3600 > payload.get("queued_at",0))):
            return agent_request_state(db,task_id)
    if row is None:return {"status":"not_requested"}
    link=db.connection.execute("SELECT l.plan_id FROM packaging_recommendation_links l JOIN model_result_drafts d USING(receipt_id) WHERE d.request_id=?",(payload["request_id"],)).fetchone()
    receipt = db.connection.execute("SELECT state,response_json FROM model_result_drafts WHERE request_id=?", (payload["request_id"],)).fetchone()
    response = json.loads(receipt["response_json"]) if receipt else {}
    status = "outcome_unknown" if row["outcome_status"] == "unknown" and row["status"] != "cancelled" else row["status"]
    if status == "completed":
        if receipt and receipt["state"] == "stale": status = "stale"
        elif response.get("status") == "needs_input": status = "needs_input"
        elif not link: status = "failed"
        elif response.get("status") == "partial": status = "partial"
    saved = _saved_suggestion_draft(db, task_id, payload)
    return {"status":status, "startedAt":float(payload.get("queued_at",0))*1000 or None, "outcomeStatus":row["outcome_status"], "savedDraftAvailable":bool(saved), **(saved or {}), "unknowns":response.get("unknowns", []),
        "clarifications":response.get("clarifications", []), "issues":(response.get("result") or {}).get("issues", []),
        "jobId":row["job_id"],"planId":saved["planId"] if saved else None,
        "problem":row["error_summary"],"requestId":payload["request_id"]}


def _saved_suggestion_draft(db, task_id, payload):
    """Expose a persisted draft without reclassifying or retrying an unknown call."""
    row=db.connection.execute("""SELECT l.plan_id,d.receipt_id,r.input_digest,r.request_json,d.response_json
        FROM packaging_recommendation_links l
        JOIN model_result_drafts d USING(receipt_id)
        JOIN model_contract_requests r USING(request_id)
        JOIN packaging_plans p ON p.plan_id=l.plan_id
        WHERE r.request_id=? AND r.task_id=? AND l.task_id=? AND p.task_id=?
          AND r.template_id='P09' AND d.state='draft' AND p.status='draft'""",
        (payload.get("request_id"),task_id,task_id,task_id)).fetchone()
    if row is None:return None
    try:
        request=json.loads(row["request_json"])
        response=json.loads(row["response_json"])
        expected=(payload.get("execution_authorization") or {}).get("input_digest")
        if (request.get("request_id") != payload.get("request_id")
                or request.get("input_digest") != row["input_digest"]
                or (expected is not None and expected != row["input_digest"])
                or response.get("status") not in {"ok","partial"}):
            return None
    except (TypeError,ValueError):return None
    return {"planId":row["plan_id"],"receiptId":row["receipt_id"],"inputDigest":row["input_digest"]}
