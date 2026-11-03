"""Visual JSON schemas shared by explicit request and local validation paths.

Uses the same installed Draft202012 engine as model_contracts. Validation never
repairs data or echoes provider content. Existing semantic evidence checks still
run after this structural contract.
"""
from copy import deepcopy
import hashlib
import json

from jsonschema import Draft202012Validator, validators

from .visual_observations import CAMERA_MOTIONS, MOTION_KINDS
from .visual_protocol import VisualProtocolError, protocol_contract


# Source microseconds are exact JSON integers, consistent with _validate_result.
_Validator = validators.extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _checker, value: type(value) is int))


def color_schema() -> dict:
    return {"type": "object", "properties": {"colors": {"type": "array", "items": {
        "type": "string", "enum": ["red", "green", "blue", "other"]}}},
        "required": ["colors"], "additionalProperties": False}


def observation_schema() -> dict:
    properties = {
        "title": {"type": "string"}, "description": {"type": "string"},
        "cameraMotion": {"type": "string", "enum": sorted(CAMERA_MOTIONS)},
        "motionKind": {"type": "string", "enum": ["", *sorted(MOTION_KINDS)]},
        "subjectMotion": {"type": "string"}, "uncertaintyReason": {"type": "string"},
        "evidenceUs": {"type": "array", "items": {"type": "integer"}},
    }
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def visual_schema_digest() -> str:
    return hashlib.sha256(json.dumps([color_schema(), observation_schema()], sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def response_format(mode: str, name: str, schema: dict) -> dict:
    contract = protocol_contract(mode)
    if contract["schemaValidation"] == "local":
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {
        "name": name, "strict": True, "schema": deepcopy(schema)}}


def validate_visual_schema(answer, schema: dict, mode: str, *, diagnostics=None) -> None:
    facts = {**(diagnostics or {}), **protocol_contract(mode)}
    try:
        json.dumps(answer, allow_nan=False)
        error = next(_Validator(schema).iter_errors(answer), None)
        valid = error is None
        if error is not None:
            from .visual_protocol import schema_error_diagnostic
            facts['schemaError'] = schema_error_diagnostic(list(error.absolute_path), error.validator)
    except (ValueError, TypeError, OverflowError, RecursionError):
        valid = False
    if not valid:
        raise VisualProtocolError("VISUAL_SCHEMA_INVALID", "最终结果不符合完整视觉Schema；未自动降级", facts)
