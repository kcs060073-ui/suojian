"""Strict Chat response adapter shared by probes and visual observations.

Only final text is normalized. No JSON repair, reasoning extraction, requests,
retry, or inference of provider capabilities happens here.
"""

import json
import re

PROTOCOL_VERSION = "visual-chat-final-json-v3"
DEFAULT_PROTOCOL_MODE = "server_json_schema"
LOCAL_SCHEMA_MODE = "json_object_local_schema"
SCHEMA_VERSION = "visual-result-schema-v1"
_FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls", "function_call"}


class VisualProtocolError(ValueError):
    def __init__(self, code, message, diagnostics):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.diagnostics = diagnostics


def visual_protocol_mode(settings=None) -> str:
    """Only explicit supported choices; absence preserves the strict request."""
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("VISUAL_PROTOCOL_MODE_INVALID: 视觉协议设置必须是对象")
    mode = settings.get("visual_protocol_mode", DEFAULT_PROTOCOL_MODE)
    if not isinstance(mode, str) or mode not in (DEFAULT_PROTOCOL_MODE, LOCAL_SCHEMA_MODE):
        raise ValueError("VISUAL_PROTOCOL_MODE_INVALID: 请选择明确支持的视觉协议模式")
    return mode


def protocol_contract(mode=DEFAULT_PROTOCOL_MODE) -> dict:
    mode = visual_protocol_mode({"visual_protocol_mode": mode})
    return {"protocolVersion": PROTOCOL_VERSION, "protocolMode": mode,
            "schemaValidation": "local" if mode == LOCAL_SCHEMA_MODE else "server",
            "schemaVersion": SCHEMA_VERSION}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non-finite JSON number")


def _loads(value):
    return json.loads(value, object_pairs_hook=_object, parse_constant=_invalid_constant)


def request_diagnostics(payload: dict, *, protocol_mode=DEFAULT_PROTOCOL_MODE) -> dict:
    """Describe our outbound shape without copying any prompt or media bytes."""
    parts = [part for message in payload.get("messages", [])
             for part in message.get("content", []) if isinstance(part, dict)]
    format_kind = payload.get("response_format", {}).get("type")
    return {**protocol_contract(protocol_mode),
            "imageCount": sum(part.get("type") == "image_url" for part in parts),
            "textPartCount": sum(part.get("type") == "text" for part in parts),
            "maxTokens": payload.get("max_tokens") if type(payload.get("max_tokens")) is int else None,
            "responseFormat": format_kind if format_kind in ("json_schema", "json_object") else "none",
            **({"reasoningEffort":"low", "thinkingType":"enabled"} if payload.get("reasoning_effort")=="low" and payload.get("thinking")=={"type":"enabled"} else {})}


def response_diagnostics(raw, *, protocol_mode=DEFAULT_PROTOCOL_MODE) -> dict:
    """Allowlisted shape only: never retain content, reasoning, IDs or keys."""
    facts = {**protocol_contract(protocol_mode), "envelopeKind": "invalid",
             "choiceCount": 0, "finishReason": "missing", "contentKind": "missing",
             "hasReasoning": False, "hasRefusal": False, "hasToolCalls": False,
             "contentChars": None, "usage": {}}
    try:
        envelope = _loads(raw)
    except (ValueError, TypeError, RecursionError):
        return facts
    if not isinstance(envelope, dict):
        return facts
    facts["envelopeKind"] = "object"
    facts["usage"] = safe_visual_diagnostics({"usage": envelope.get("usage")}).get("usage", {})
    choices = envelope.get("choices")
    if not isinstance(choices, list):
        return facts
    facts["choiceCount"] = len(choices)
    if not choices or not isinstance(choices[0], dict):
        return facts
    choice = choices[0]
    finish = choice.get("finish_reason")
    facts["finishReason"] = finish if isinstance(finish, str) and finish in _FINISH_REASONS else (
        "missing" if finish is None else "other")
    message = choice.get("message")
    if not isinstance(message, dict):
        return facts
    content = message.get("content")
    if isinstance(content, str):
        facts["contentChars"] = len(content)
    elif isinstance(content, list) and all(isinstance(p, dict) and p.get("type")=="text" and isinstance(p.get("text"), str) for p in content):
        facts["contentChars"] = sum(len(p["text"]) for p in content)
    facts.update(contentKind=("text" if isinstance(content, str) else
                              "parts" if isinstance(content, list) else
                              "missing" if content is None else "other"),
                 hasReasoning=bool(message.get("reasoning_content")),
                 hasRefusal=bool(message.get("refusal")),
                 hasToolCalls=bool(message.get("tool_calls") or message.get("function_call")))
    return facts


def parse_visual_response(raw, *, protocol_mode=DEFAULT_PROTOCOL_MODE) -> dict:
    facts = response_diagnostics(raw, protocol_mode=protocol_mode)

    def reject(code="VISUAL_PROTOCOL_INVALID", message="响应不是完整的结构化结果"):
        raise VisualProtocolError(code, message, facts)

    try:
        envelope = _loads(raw)
        if not isinstance(envelope, dict):
            reject()
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            reject()
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            reject()
        finish = choice.get("finish_reason")
        if message.get("refusal") or finish == "content_filter":
            reject("VISUAL_RESPONSE_REFUSED", "服务拒绝返回测试结果；未自动重试")
        if finish != "stop":
            reason = "输出达到上限，结果被截断" if finish == "length" else "服务未正常结束输出"
            reject("VISUAL_RESPONSE_INCOMPLETE", reason + "；未自动重试")
        if message.get("tool_calls") or message.get("function_call"):
            reject()
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") != "text" or
                   not isinstance(part.get("text"), str) for part in content):
                reject()
            content = "".join(part["text"] for part in content)
        if not isinstance(content, str) or not content.strip():
            if message.get("reasoning_content"):
                reject("VISUAL_FINAL_RESULT_MISSING", "仅收到推理内容，缺少最终结果；未自动重试")
            reject()
        # Accept one whole-document transport wrapper, never extract or repair JSON.
        fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", content.strip(), re.IGNORECASE)
        answer = _loads(fenced.group(1) if fenced else content)
        if not isinstance(answer, dict):
            reject()
        return answer
    except VisualProtocolError:
        raise
    except (ValueError, TypeError, RecursionError):
        reject()


_SCHEMA_PATH_FIELDS = frozenset(('segments', 'title', 'description', 'cameraMotion', 'motionKind',
    'subjectMotion', 'uncertaintyReason', 'evidenceUs', 'sourceStartUs', 'sourceEndUs',
    'firstFrame', 'lastFrame', 'evidenceFrames', 'colors', 'conclusion', 'unknowns',
    'issues', 'target_ref', 'category', 'severity', 'evidence_refs', 'recommendation', 'position_seconds',
    'template_id', 'status', 'result', 'instances', 'kind', 'asset_ref', 'content', 'text',
    'fact_refs', 'start_anchor', 'end_anchor', 'edge', 'position', 'reason',
    'omitted_reasons', 'clarifications'))
_SCHEMA_VALIDATORS = frozenset(('type', 'required', 'enum', 'additionalProperties',
    'minItems', 'maxItems', 'uniqueItems', 'minimum', 'maximum', 'minLength', 'maxLength',
    'const', 'anyOf', 'oneOf', 'contains', 'minContains', 'maxContains'))


def schema_error_diagnostic(parts, validator):
    # Only known contract names and bounded indices, never unknown object keys or values.
    path = '$'
    for part in parts[:8]:
        if type(part) is int and 0 <= part <= 16:
            path += f'[{part}]'
        elif isinstance(part, str) and part in _SCHEMA_PATH_FIELDS:
            path += '.' + part
        else:
            break
    return {'path': path, 'validator': validator if validator in _SCHEMA_VALIDATORS else 'other'}


def schema_validation_diagnostic(answer, schema):
    """Explain a rejected response using contract paths, never provider content.

    Called only after strict validation failed. This changes diagnostics, not
    acceptance, and resolves object/null unions to the relevant nested error.
    """
    from jsonschema.exceptions import best_match
    from .visual_schema import _Validator
    error = best_match(_Validator(schema).iter_errors(answer))
    return schema_error_diagnostic(list(error.absolute_path), error.validator) if error else {}


def safe_visual_diagnostics(value):
    """Bounded allowlist; no provider text, reasoning body, IDs or credentials."""
    if not isinstance(value, dict):
        return {}
    result = {}
    if type(value.get('schemaCorrectionAttempts')) is int and value['schemaCorrectionAttempts'] == 1:
        result['schemaCorrectionAttempts'] = 1
    if value.get('correctionOutcome') in ('unknown', 'rejected'):
        result['correctionOutcome'] = value['correctionOutcome']
    schema_error = value.get('schemaError')
    if isinstance(schema_error, dict):
        path = schema_error.get('path')
        validator = schema_error.get('validator')
        tokens = r'(?:\.(?:' + '|'.join(sorted(_SCHEMA_PATH_FIELDS)) + r')|\[(?:[0-9]|1[0-6])\])'
        if (isinstance(path, str) and len(path) <= 200 and re.fullmatch(r'\$' + tokens + r'{0,8}', path)
                and isinstance(validator, str) and validator in _SCHEMA_VALIDATORS | {'other'}):
            result['schemaError'] = {'path': path, 'validator': validator}
    enums = {'finishReason': _FINISH_REASONS | {'missing','other'}}
    for key, choices in enums.items():
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    if type(value.get('hasReasoning')) is bool:
        result['hasReasoning'] = value['hasReasoning']
    if value.get('contentChars') is None and 'contentChars' in value:
        result['contentChars'] = None
    elif type(value.get('contentChars')) is int and 0 <= value['contentChars'] <= 2 * 1024 * 1024:
        result['contentChars'] = value['contentChars']
    usage = value.get('usage')
    if isinstance(usage, dict):
        allowed = {}
        for key in ('prompt_tokens','completion_tokens','total_tokens'):
            if type(usage.get(key)) is int and 0 <= usage[key] <= 1_000_000_000:
                allowed[key] = usage[key]
        details = usage.get('completion_tokens_details')
        if isinstance(details, dict) and type(details.get('reasoning_tokens')) is int and 0 <= details['reasoning_tokens'] <= 1_000_000_000:
            allowed['completion_tokens_details'] = {'reasoning_tokens':details['reasoning_tokens']}
        result['usage'] = allowed
    return result


class VisualResponse(dict):
    def __init__(self, answer, diagnostics):
        super().__init__(answer)
        self.diagnostics = safe_visual_diagnostics(diagnostics)
