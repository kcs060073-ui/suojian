"""User-requested P09 handoff; authenticated MCP identity owns each claimed request."""
import json
import time
from pathlib import Path

from .model_requests import register_request, submit_result, _context_from_json
from .packaging_recommendations import prepare_packaging_request, materialize_packaging_suggestion


def response_schema(prepared=None):
    schemas = json.loads((Path(__file__).parent / "contracts/model-schemas-v1.json").read_text(encoding="utf-8"))
    schema={"$ref": "#/$defs/P09Output", "$defs": {key: value for key, value in schemas["$defs"].items()
        if key in {"P09Output", "text", "anchor", "issue"}}}
    if prepared is not None:
        from .packaging_execution_contract import constrain_schema
        schema=constrain_schema(schema,prepared)
    return schema


def queue_agent_request(db, project_root, task_id, request_id, style, base_plan_id, prompt_binding):
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ValueError("REQUEST_ID_INVALID: 包装请求编号无效")
    with db.transaction(immediate=True) as conn:
        old = conn.execute("SELECT * FROM packaging_agent_requests WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if (old["task_id"], old["style"], old["base_plan_id"]) != (task_id, style, base_plan_id):
                raise ValueError("REQUEST_REPLAY_MISMATCH: 请求编号已绑定其他输入")
            return {"requestId": request_id, "status": old["status"], "mode": "agent"}
        prepared = prepare_packaging_request(db, project_root, task_id, request_id, style, base_plan_id=base_plan_id)
        if prepared["visual"]["frames"]:
            raise RuntimeError("PACKAGING_VISUAL_AGENT_UNSUPPORTED: Agent文本交接不支持已验证画面输入，请使用内置视觉建议路径")
        # A newly requested draft supersedes in-flight suggestions, never existing plans.
        conn.execute("UPDATE packaging_agent_requests SET status='stale',updated_at=datetime('now') "
                     "WHERE task_id=? AND status IN ('waiting_agent','claimed')", (task_id,))
        conn.execute("INSERT INTO packaging_agent_requests(request_id,task_id,base_plan_id,style,input_digest,"
                     "prompt_version,prompt_sha256,status,expires_at) VALUES(?,?,?,?,?,?,?,'waiting_agent',?)",
                     (request_id, task_id, base_plan_id, style, prepared["request"]["input_digest"],
                      *prompt_binding, time.time() + 3600))
    return {"requestId": request_id, "status": "waiting_agent", "mode": "agent"}


def _row(db, task_id, request_id):
    row = db.connection.execute("SELECT * FROM packaging_agent_requests WHERE request_id=? AND task_id=?",
                                (request_id, task_id)).fetchone()
    if row is None:
        raise ValueError("REQUEST_NOT_FOUND: 请先在包装页发起本片建议")
    return row


def _owner(row, client_id):
    if client_id not in {"codex", "workbuddy", "doubao_work"}:
        raise ValueError("REQUEST_OWNER_MISMATCH: 未知的Agent客户端")
    if row["claimed_by"] is not None and row["claimed_by"] != client_id:
        raise ValueError("REQUEST_OWNER_MISMATCH: 请求已由其他Agent客户端领取")


def _prepare_current(db, project_root, row):
    return prepare_packaging_request(db, project_root, row["task_id"], row["request_id"], row["style"],
                                     base_plan_id=row["base_plan_id"])


def _mark_stale(conn, request_id):
    conn.execute("UPDATE packaging_agent_requests SET status='stale',updated_at=datetime('now') WHERE request_id=?", (request_id,))


def claim_agent_request(db, project_root, task_id, request_id, client_id):
    from .packaging_suggestion_jobs import approved_p09_prompt, execution_prompt
    with db.transaction(immediate=True) as conn:
        row = _row(db, task_id, request_id)
        binding = approved_p09_prompt(project_root,execution_confirmation={
            "prompt_version":row["prompt_version"],"prompt_sha256":row["prompt_sha256"]})
        _owner(row, client_id)
        if row["status"] not in {"waiting_agent", "claimed"}:
            return {"requestId": request_id, "status": row["status"]}
        if time.time() >= row["expires_at"] or binding != (row["prompt_version"], row["prompt_sha256"]):
            _mark_stale(conn, request_id)
            return {"requestId": request_id, "status": "stale"}
        try:
            prepared = _prepare_current(db, project_root, row)
        except (ValueError, RuntimeError):
            _mark_stale(conn, request_id)
            return {"requestId": request_id, "status": "stale"}
        if prepared["request"]["input_digest"] != row["input_digest"]:
            _mark_stale(conn, request_id)
            return {"requestId": request_id, "status": "stale"}
        register_request(db, task_id=task_id, request=prepared["request"], context=prepared["context"],
                         client_id="agent:" + client_id, prompt_version=row["prompt_version"],
                         prompt_sha256=row["prompt_sha256"], expires_at=row["expires_at"], connection=conn)
        conn.execute("UPDATE packaging_agent_requests SET status='claimed',claimed_by=?,updated_at=datetime('now') WHERE request_id=?",
                     (client_id, request_id))
        return {"requestId": request_id, "status": "claimed", "request": prepared["request"],
                "prompt": execution_prompt(project_root,row["prompt_version"]), "promptVersion": row["prompt_version"],
                "promptSha256": row["prompt_sha256"], "responseSchema": response_schema(prepared)}


def submit_agent_result(db, project_root, task_id, request_id, response, client_id):
    from .packaging_suggestion_jobs import approved_p09_prompt
    with db.transaction(immediate=True) as conn:
        row = _row(db, task_id, request_id)
        binding = approved_p09_prompt(project_root,execution_confirmation={
            "prompt_version":row["prompt_version"],"prompt_sha256":row["prompt_sha256"]})
        _owner(row, client_id)
        if row["claimed_by"] is None:
            raise ValueError("REQUEST_NOT_CLAIMED: 请先领取当前请求")
        registered = conn.execute("SELECT * FROM model_contract_requests WHERE request_id=?", (request_id,)).fetchone()
        if registered is None:
            raise ValueError("REQUEST_NOT_FOUND: 包装请求登记不存在")
        def current_context(*_):
            if row["status"] not in {"stale", "cancelled"} and binding == (row["prompt_version"], row["prompt_sha256"]):
                try:
                    return _prepare_current(db, project_root, row)["context"]
                except (ValueError, RuntimeError):
                    pass
            from dataclasses import replace
            return replace(_context_from_json(registered["context_json"]), input_digest="stale")
        receipt = submit_result(db, request_id=request_id, response=response, client_id="agent:" + client_id,
                                resolve_context=current_context, connection=conn)
        result = {"requestId": request_id, "receiptId": receipt["receipt_id"], "state": receipt["state"]}
        status = "stale" if receipt["state"] == "stale" else response["status"]
        if receipt["state"] == "draft" and response["status"] in {"ok", "partial"}:
            result.update(materialize_packaging_suggestion(db, project_root, task_id, receipt["receipt_id"], row["style"], connection=conn))
            status = "completed" if response["status"] == "ok" else "partial"
        elif status not in {"stale", "needs_input"}:
            raise ValueError("MODEL_RESPONSE_INVALID: 包装结果状态无效")
        conn.execute("UPDATE packaging_agent_requests SET status=?,updated_at=datetime('now') WHERE request_id=?", (status, request_id))
        return result


def agent_request_state(db, task_id):
    row = db.connection.execute("SELECT a.*,d.receipt_id,d.response_json,l.plan_id FROM packaging_agent_requests a "
        "LEFT JOIN model_result_drafts d USING(request_id) LEFT JOIN packaging_recommendation_links l USING(receipt_id) "
        "WHERE a.task_id=? ORDER BY a.rowid DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return {"status": "not_requested", "mode": "agent"}
    status = row["status"]
    if status in {"waiting_agent", "claimed"} and time.time() >= row["expires_at"]:
        status = "stale"
    response = json.loads(row["response_json"]) if row["response_json"] else {}
    return {"status": status, "mode": "agent", "requestId": row["request_id"], "planId": row["plan_id"],
            "claimedBy": row["claimed_by"], "unknowns": response.get("unknowns", []),
            "clarifications": response.get("clarifications", []), "issues": (response.get("result") or {}).get("issues", [])}


def cancel_agent_request(db, task_id, request_id):
    with db.transaction(immediate=True) as conn:
        row = _row(db, task_id, request_id)
        if row["status"] in {"waiting_agent", "claimed"}:
            conn.execute("UPDATE packaging_agent_requests SET status='cancelled',updated_at=datetime('now') WHERE request_id=?", (request_id,))
            return {"requestId": request_id, "status": "cancelled"}
        return {"requestId": request_id, "status": row["status"]}
