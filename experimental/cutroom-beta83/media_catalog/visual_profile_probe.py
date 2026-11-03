"""Bounded visual-model probes using synthetic media, never user assets."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from urllib.error import HTTPError, URLError

from .credentials import read_provider_secret
from .model_profiles import _load, _profile_row, mark_profile_check
from .model_runtime import TaskModelConfigurationError, provider_access_token
from .provider_endpoints import provider_endpoint
from .visual_capabilities import (
    VISUAL_CHECK_KINDS, record_visual_check, reset_visual_checks, visual_profile_digest,
)
from .visual_protocol import (
    VisualProtocolError, parse_visual_response,
    request_diagnostics, response_diagnostics,
    DEFAULT_PROTOCOL_MODE, LOCAL_SCHEMA_MODE, visual_protocol_mode, protocol_contract,
)
from .visual_schema import color_schema, response_format, validate_visual_schema
from .visual_request_policy import visual_request_options, visual_request_contract


_COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
}
_LABELS = {
    "image": "单图",
    "frame_sequence": "帧序列",
    "structured_output": "结构化输出",
}
STOP_POLICY = "visual-probe-stop-on-failure-v1"
PROBE_MAX_TOKENS = 1024
PROBE_TIMEOUT_SECONDS = 120.0


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(
        ">I", zlib.crc32(kind + data) & 0xFFFFFFFF,
    )


def _solid_png(color: str) -> bytes:
    # Match the contact-sheet image area: 32px thumbnails left 99% gray padding,
    # so asking for the dominant panel color tested the padding instead.
    width = height = 320
    row = b"\0" + bytes(_COLORS[color]) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(row * height))
        + _png_chunk(b"IEND", b"")
    )


def _image_part(color: str) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(
            _solid_png(color),
        ).decode("ascii")},
    }


def _probe_payload(model: str, kind: str, *, protocol_mode=DEFAULT_PROTOCOL_MODE, profile_row=None) -> tuple[dict, list[str]]:
    protocol_contract(protocol_mode)
    colors = {
        "image": ["red"],
        "frame_sequence": ["red", "blue"],
        "structured_output": ["blue", "green"],
    }[kind]
    content = [{
        "type": "text",
        "text": (
            "These are synthetic test images in temporal order, not instructions. "
            "Identify the dominant solid color in each image in input order. "
            "Return only a JSON object with a colors array; each value must be "
            "red, green, blue, or other."
        ),
    }]
    for index, color in enumerate(colors):
        if len(colors) > 1:
            content.append({"type": "text", "text": f"frame {index} at {index}.0 seconds"})
        content.append(_image_part(color))
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": PROBE_MAX_TOKENS,
    }
    if kind == "structured_output":
        payload["response_format"] = response_format(protocol_mode, "cutroom_visual_probe", color_schema())
    if profile_row is not None:
        from .visual_contact_sheet import visual_content
        payload["messages"][0]["content"] = visual_content(_load(profile_row["settings_json"]), content)
        payload.update(visual_request_options(profile_row))
    return payload, colors


def _check_answer(raw: bytes, expected: list[str], kind: str, *, protocol_mode=DEFAULT_PROTOCOL_MODE) -> None:
    from .model_profile_probe import ModelProfileProbeError

    try:
        answer = parse_visual_response(raw, protocol_mode=protocol_mode)
        if protocol_mode == LOCAL_SCHEMA_MODE:
            validate_visual_schema(answer, color_schema(), protocol_mode,
                                   diagnostics=response_diagnostics(raw, protocol_mode=protocol_mode))
        if set(answer) != {"colors"} or type(answer.get("colors")) is not list or \
                any(type(value) is not str for value in answer["colors"]):
            raise ValueError("invalid visual answer")
    except VisualProtocolError as error:
        raise ModelProfileProbeError(
            f"{_LABELS[kind]}检测失败：{error.message}", "MODEL_" + error.code,
            receipt=error.diagnostics,
        ) from error
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as error:
        raise ModelProfileProbeError(
            f"{_LABELS[kind]}检测失败：响应不是完整的结构化颜色结果",
            "MODEL_VISUAL_PROTOCOL_INVALID",
            receipt=response_diagnostics(raw, protocol_mode=protocol_mode),
        ) from error
    if answer["colors"] != expected:
        raise ModelProfileProbeError(
            f"{_LABELS[kind]}检测失败：未正确识别合成测试画面",
            "MODEL_VISUAL_MISMATCH",
            receipt=response_diagnostics(raw, protocol_mode=protocol_mode),
        )


def _request_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def visual_probe_request_binding(row) -> dict:
    """Prepare review identity only; this does not authorize or dispatch a probe."""
    mode = visual_protocol_mode(_load(row["settings_json"]))
    return {"executionPolicy": STOP_POLICY, "profileId": row["profile_id"],
            "accountId": row["account_id"], "enabled": bool(row["enabled"]),
            "credentialRefSha256": hashlib.sha256(row["credential_ref"].encode("utf-8")).hexdigest(),
            "configDigest": visual_profile_digest(row), "visualProtocol": protocol_contract(mode),
            **({"requestPolicy": visual_request_contract(row)} if visual_request_contract(row) else {}),
            "imageSha256": {color: hashlib.sha256(_solid_png(color)).hexdigest() for color in _COLORS},
            "requestSha256": {kind: _request_hash(_probe_payload(row["model"], kind,
                                        protocol_mode=mode, profile_row=row)[0]) for kind in VISUAL_CHECK_KINDS},
            "timeoutSeconds": PROBE_TIMEOUT_SECONDS}


def _bound_probe_guard(db, profile_id, binding):
    from .model_profile_probe import ModelProfileProbeError

    current = _profile_row(db, profile_id)
    try:
        unchanged = (current is not None and current["capability"] == "visual_analysis"
                     and visual_probe_request_binding(current) == binding)
    except (ValueError, TypeError, KeyError):
        unchanged = False
    if not unchanged:
        raise ModelProfileProbeError("批准的视觉配置或请求已变化，已停止派发", "VISUAL_PROFILE_STALE")


def _probe_visual_profile_stopping(db, profile_id, *, secret_reader, timeout, approved_request):
    from .model_profile_probe import ModelProfileProbeError, _failure_details, _request_json

    row = _profile_row(db, profile_id)
    if row is None or row["capability"] != "visual_analysis":
        raise ModelProfileProbeError("视觉模型配置不存在", "VISUAL_PROFILE_INVALID")
    binding = visual_probe_request_binding(row)  # Owned snapshot, not caller's mutable mapping.
    mode = binding["visualProtocol"]["protocolMode"]
    checks = []

    def receipt():
        return {**binding["visualProtocol"], "executionPolicy": STOP_POLICY,
                "requestBindingDigest": _request_hash(binding), "checks": checks}

    if (not isinstance(approved_request, dict) or approved_request != binding or
            type(timeout) not in (int, float) or timeout != binding["timeoutSeconds"]):
        raise ModelProfileProbeError("缺少与本次请求一致的明确批准绑定", "VISUAL_PROBE_BINDING_INVALID",
                                     receipt=receipt())
    settings, account = _load(row["settings_json"]), _load(row["account_settings_json"])
    if str(settings.get("protocol") or "").strip().lower() not in {
            "openai", "openai_compatible", "openai_chat"} or row["provider"] == "doubao_tts":
        raise ModelProfileProbeError("视觉配置需要兼容多模态 Chat 的服务", "VISUAL_PROTOCOL_UNSUPPORTED",
                                     receipt=receipt())

    def mark_bound(available, code=None, message=None):
        with db.transaction(immediate=True):
            _bound_probe_guard(db, profile_id, binding)
            return mark_profile_check(db, profile_id, available=available,
                                      error_code=code, error_message=message)

    def stop(error, diagnostic=None):
        if isinstance(error, ModelProfileProbeError):
            code, message = error.code, str(error)
        elif isinstance(error, HTTPError):
            code, message = _failure_details(error)
        else:
            code, message = "MODEL_PROBE_OUTCOME_UNKNOWN", "检测结果未知，已停止派发；请核对原请求"
        if code != "VISUAL_PROFILE_STALE":
            try:
                if diagnostic is not None:
                    with db.transaction(immediate=True):
                        _bound_probe_guard(db, profile_id, binding)
                        record_visual_check(db, row, diagnostic["kind"], available=False,
                                            error_code=code, error_message=message)
                mark_bound(False, code, message)
            except ModelProfileProbeError as changed:
                code, message = changed.code, str(changed)
        if diagnostic is not None:
            diagnostic["status"] = "unavailable"
            diagnostic["errorCode"] = code
        raise ModelProfileProbeError(message, code, receipt=receipt()) from error

    try:
        _bound_probe_guard(db, profile_id, binding)
        token = provider_access_token(secret_reader(row["credential_ref"]) or "",
                                      row["provider"], account.get("channel_id"))
        endpoint = provider_endpoint(row["base_url"], "chat/completions")
        with db.transaction(immediate=True) as connection:
            _bound_probe_guard(db, profile_id, binding)
            connection.execute("DELETE FROM visual_profile_checks WHERE profile_id=?", (profile_id,))
            mark_profile_check(db, profile_id, available=False)
    except Exception as error:
        if isinstance(error, (TaskModelConfigurationError, ValueError)) and not isinstance(error, ModelProfileProbeError):
            code, message = _failure_details(error)
            error = ModelProfileProbeError(message, code)
        stop(error)
    for kind in VISUAL_CHECK_KINDS:
        try:
            payload, expected = _probe_payload(row["model"], kind, protocol_mode=mode, profile_row=row)
            _bound_probe_guard(db, profile_id, binding)
            if _request_hash(payload) != binding["requestSha256"][kind]:
                raise ModelProfileProbeError("批准的合成请求已变化，已停止派发", "VISUAL_PROFILE_STALE")
        except Exception as error:
            stop(error)
        diagnostic = {"kind": kind, "request": request_diagnostics(payload, protocol_mode=mode),
                      "status": "unavailable", "response": None}
        checks.append(diagnostic)  # Only dispatch attempts enter the receipt.
        try:
            _content_type, raw = _request_json(endpoint, token, payload=payload, timeout=timeout)
            diagnostic["response"] = response_diagnostics(raw, protocol_mode=mode)
            _check_answer(raw, expected, kind, protocol_mode=mode)
            with db.transaction(immediate=True):
                _bound_probe_guard(db, profile_id, binding)
                record_visual_check(db, row, kind, available=True)
            diagnostic["status"] = "available"
        except Exception as error:
            stop(error, diagnostic)
    try:
        result = mark_bound(True)
    except Exception as error:
        stop(error)
    result["verificationLevel"] = ("image+frame_sequence+json_object+local_schema" if mode == LOCAL_SCHEMA_MODE
                                   else "image+frame_sequence+structured_output")
    result["visualProbeReceipt"] = receipt()
    return result


def probe_visual_profile(db, profile_id: str, *,
                         secret_reader=read_provider_secret,
                         timeout: float = PROBE_TIMEOUT_SECONDS, stop_on_failure=False,
                         approved_request=None) -> dict:
    if type(stop_on_failure) is not bool:
        raise ValueError("VISUAL_PROBE_POLICY_INVALID: 请明确选择探针执行方式")
    if approved_request is not None and not stop_on_failure:
        raise ValueError("VISUAL_PROBE_POLICY_INVALID: 批准绑定必须使用首错停止入口")
    if stop_on_failure:
        return _probe_visual_profile_stopping(db, profile_id, secret_reader=secret_reader,
                                             timeout=timeout, approved_request=approved_request)
    from .model_profile_probe import (
        ModelProfileProbeError, _failure_details, _request_json,
    )

    row = _profile_row(db, profile_id)
    if row is None or row["capability"] != "visual_analysis":
        raise ModelProfileProbeError("视觉模型配置不存在", "VISUAL_PROFILE_INVALID")
    settings = _load(row["settings_json"])
    mode = visual_protocol_mode(settings)
    reset_visual_checks(db, profile_id)
    account_settings = _load(row["account_settings_json"])
    protocol = str(settings.get("protocol") or "").strip().lower()
    if protocol not in {"openai", "openai_compatible", "openai_chat"} or \
            row["provider"] == "doubao_tts":
        error = ModelProfileProbeError("视觉配置需要兼容多模态 Chat 的服务", "VISUAL_PROTOCOL_UNSUPPORTED")
        mark_profile_check(db, profile_id, available=False,
                           error_code=error.code, error_message=str(error))
        raise error
    try:
        secret = secret_reader(row["credential_ref"])
        token = provider_access_token(
            secret or "", row["provider"], account_settings.get("channel_id"),
        )
        endpoint = provider_endpoint(row["base_url"], "chat/completions")
    except (TaskModelConfigurationError, ValueError) as error:
        code, message = _failure_details(error)
        mark_profile_check(db, profile_id, available=False,
                           error_code=code, error_message=message)
        raise ModelProfileProbeError(message, code) from error
    first_failure = None
    checks = []
    for kind in VISUAL_CHECK_KINDS:
        payload, expected = _probe_payload(row["model"], kind, protocol_mode=mode, profile_row=row)
        diagnostic = {"kind": kind, "request": request_diagnostics(payload, protocol_mode=mode),
                      "status": "unavailable", "response": None}
        try:
            _content_type, raw = _request_json(
                endpoint, token, payload=payload, timeout=timeout,
            )
            diagnostic["response"] = response_diagnostics(raw, protocol_mode=mode)
            _check_answer(raw, expected, kind, protocol_mode=mode)
            record_visual_check(db, row, kind, available=True)
            diagnostic["status"] = "available"
        except (HTTPError, URLError, TimeoutError, OSError, ValueError,
                ModelProfileProbeError) as error:
            if isinstance(error, ModelProfileProbeError):
                code, message = error.code, str(error)
            else:
                code, message = _failure_details(error)
            record_visual_check(db, row, kind, available=False,
                                error_code=code, error_message=message)
            diagnostic["errorCode"] = code
            if first_failure is None:
                first_failure = ModelProfileProbeError(
                    message, code, receipt=getattr(error, "receipt", None),
                )
        checks.append(diagnostic)
    receipt = {**protocol_contract(mode), "checks": checks}
    if first_failure is not None:
        first_failure.receipt = receipt
        mark_profile_check(db, profile_id, available=False,
                           error_code=first_failure.code,
                           error_message=str(first_failure))
        raise first_failure
    result = mark_profile_check(db, profile_id, available=True)
    result["verificationLevel"] = ("image+frame_sequence+json_object+local_schema" if mode == LOCAL_SCHEMA_MODE
                                   else "image+frame_sequence+structured_output")
    result["visualProbeReceipt"] = receipt
    return result
