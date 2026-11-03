"""Transactional model suggestion inbox, shared by future provider/Agent adapters.

The caller supplies its authenticated client identity and a server-side resolver.
Receipts are immutable audit records, not proof the suggestion is still current.
No function here approves user gates, changes confirmed content, or starts jobs.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json
import math
import re
import time
import uuid

from .model_contracts import ModelContext, ModelContractError, validate_envelope, validate_result
from .timeline_contract import TimeWindow, PlaybackRate


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _error(code, message):
    raise ModelContractError(code, message)


def _clock(value):
    if value is None:
        return time.time()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        _error("INVALID_EXPIRY", "请求有效期无效")
    return value


def _context_json(context):
    value = asdict(context)
    for key in ("allowed_operations", "locked_refs", "approved_fact_refs", "editable_refs", "candidate_refs"):
        value[key] = sorted(value[key])
    return _json(value)


def _context_from_json(value):
    data = json.loads(value)
    for key in ("allowed_operations", "locked_refs", "approved_fact_refs", "editable_refs", "candidate_refs"):
        data[key] = frozenset(data.get(key, []))
    data["anchor_windows"] = {key: TimeWindow(**window) for key, window in data["anchor_windows"].items()}
    data.setdefault("schema_version", "1.0.0")
    if data.get("main_audio_rate") is not None:
        data["main_audio_rate"] = PlaybackRate(**data["main_audio_rate"])
    return ModelContext(**data)


def _request_receipt(row):
    return {"request_id": row["request_id"], "task_id": row["task_id"],
            "template_id": row["template_id"], "expires_at": row["expires_at"]}


def _draft_receipt(row):
    return {"request_id": row["request_id"], "receipt_id": row["receipt_id"],
            "state": row["state"], "validation_code": row["validation_code"]}


def _write_scope(db, connection=None):
    if connection is None:
        return db.transaction(immediate=True)
    if connection is not db.connection or not connection.in_transaction or db._transaction_context_depth <= 0:
        raise RuntimeError("MODEL_TRANSACTION_REQUIRED: 必须复用当前受控写事务")
    return nullcontext(connection)


def register_request(db, *, task_id, request, context: ModelContext, client_id,
                     prompt_version, prompt_sha256, expires_at, now=None, connection=None):
    validate_envelope(request, context)
    if not isinstance(client_id, str) or not client_id.strip():
        _error("REQUEST_OWNER_REQUIRED", "必须绑定已认证的请求来源")
    if not isinstance(prompt_version, str) or not prompt_version.strip() or not isinstance(prompt_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", prompt_sha256):
        _error("PROMPT_BINDING_REQUIRED", "必须冻结提示词版本和内容摘要")
    now = _clock(now)
    expiry = _clock(expires_at)
    request_json = _json(request)
    context_json = _context_json(context)
    identity = _digest([task_id, client_id, request_json, context_json, prompt_version, prompt_sha256])
    with _write_scope(db, connection) as connection:
        task = connection.execute("SELECT task_type FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            _error("TASK_NOT_FOUND", "任务不存在")
        if context.route != "library" and {"ad": "ad_mix", "digital": "digital_human"}.get(context.route) != task["task_type"]:
            _error("REQUEST_MISMATCH", "模型请求路线与任务不一致")
        old = connection.execute("SELECT * FROM model_contract_requests WHERE request_id=?", (request["request_id"],)).fetchone()
        if old is not None:
            if old["registration_digest"] != identity:
                _error("REQUEST_REPLAY_MISMATCH", "同一请求编号不能登记不同内容")
            return _request_receipt(old)
        if expires_at is None or expiry <= now:
            _error("INVALID_EXPIRY", "请求有效期必须晚于创建时间")
        connection.execute("""INSERT INTO model_contract_requests
            (request_id,task_id,client_id,template_id,schema_version,prompt_version,prompt_sha256,
             base_revision,input_digest,request_json,context_json,registration_digest,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            request["request_id"], task_id, client_id, context.template_id, request["schema_version"],
            prompt_version, prompt_sha256, context.revision, context.input_digest,
            request_json, context_json, identity, expiry))
        row = connection.execute("SELECT * FROM model_contract_requests WHERE request_id=?", (request["request_id"],)).fetchone()
        return _request_receipt(row)


def submit_result(db, *, request_id, response, client_id, resolve_context, now=None, connection=None):
    """Resolve current authoritative state inside the same write transaction as insertion."""
    now = _clock(now)
    with _write_scope(db, connection) as connection:
        request_row = connection.execute("SELECT * FROM model_contract_requests WHERE request_id=?", (request_id,)).fetchone()
        if request_row is None:
            _error("REQUEST_NOT_FOUND", "模型请求不存在")
        if request_row["client_id"] != client_id:
            _error("REQUEST_OWNER_MISMATCH", "不能提交其他来源的请求结果")
        try:
            response_json = _json(response)
            response_digest = _digest(response)
        except (ValueError, TypeError, OverflowError):
            _error("INVALID_SCHEMA", "模型返回的数据不是有效JSON")
        old = connection.execute("SELECT * FROM model_result_drafts WHERE request_id=?", (request_id,)).fetchone()
        if old is not None:
            if old["response_digest"] != response_digest:
                _error("REQUEST_REPLAY_MISMATCH", "同一请求已收到不同结果，不能覆盖")
            return _draft_receipt(old)
        request = json.loads(request_row["request_json"])
        # Even stale results must be valid against their original authorized snapshot.
        validate_result(request, response, _context_from_json(request_row["context_json"]))
        state, code = "draft", None
        if now >= request_row["expires_at"]:
            state, code = "stale", "STALE_INPUT"
        else:
            context = resolve_context(db, request_row["task_id"], request)
            try:
                validate_result(request, response, context)
            except ModelContractError as error:
                if error.code not in {"STALE_INPUT", "STALE_REFERENCE", "UNAUTHORIZED_REFERENCE", "LOCKED_TARGET_REQUIRES_REVIEW", "REQUEST_MISMATCH", "OPERATION_NOT_ALLOWED", "FACT_EVIDENCE_MISSING"}:
                    raise
                state, code = "stale", error.code
        receipt_id = "model-receipt-" + uuid.uuid4().hex
        connection.execute("""INSERT INTO model_result_drafts
            (request_id,receipt_id,response_json,response_digest,state,validation_code)
            VALUES(?,?,?,?,?,?)""", (request_id,receipt_id,response_json,response_digest,state,code))
        row = connection.execute("SELECT * FROM model_result_drafts WHERE request_id=?", (request_id,)).fetchone()
        return _draft_receipt(row)
