"""Validate model suggestions against server-owned context, never model authority.

This layer returns a draft only. Application must revalidate current context in
its write transaction; successful validation is not an approval or a render.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .timeline_contract import TimeWindow, PlaybackRate, TimelineContractError, output_window, validate_coverage, validate_source_window


class ModelContractError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _reject(code, message):
    raise ModelContractError(code, message)


@dataclass
class ModelContext:
    """Construct inside the domain service from current authorized state, not HTTP data."""
    template_id: str
    request_id: str
    revision: int
    input_digest: str
    route: str
    orientation: str
    records: dict
    allowed_operations: frozenset = frozenset()
    locked_refs: frozenset = frozenset()
    approved_fact_refs: frozenset = frozenset()
    anchor_windows: dict = field(default_factory=dict)
    media_kinds: dict = field(default_factory=dict)
    schema_version: str = "1.0.1"
    editable_refs: frozenset = frozenset()
    candidate_refs: frozenset = frozenset()
    presenter_ref: str | None = None
    main_audio_rate: PlaybackRate | None = None


@lru_cache(maxsize=2)
def _schema(version):
    filenames = {"1.0.0": "model-schemas-v1.json", "1.0.1": "model-schemas-v1.0.1.json"}
    if version not in filenames:
        _reject("INVALID_SCHEMA", "不支持的模型合同版本")
    value = json.loads((Path(__file__).parent / "contracts" / filenames[version]).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(value)
    return value


@lru_cache(maxsize=68)
def _validator(template_id, side, schema_version):
    definitions = _schema(schema_version)["$defs"]
    name = template_id + side
    if side not in {"Input", "Output"} or name not in definitions:
        _reject("INVALID_SCHEMA", "未知模型任务或Schema")
    return Draft202012Validator({"$ref": "#/$defs/" + name, "$defs": definitions})


def validate_schema(value, template_id, side, schema_version="1.0.0"):
    try:
        # Refuse NaN/Infinity even in future free JSON fields. Errors never echo input.
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        _reject("INVALID_SCHEMA", "结果不是有效JSON数据")
    error = next(_validator(template_id, side, schema_version).iter_errors(value), None)
    if error is not None:
        _reject("INVALID_SCHEMA", "模型数据不符合已登记的输入输出结构")


_REF_KINDS = {
    "asset": {"asset"}, "segment": {"segment"}, "script": {"script"},
    "asr": {"asr"}, "gap": {"gap"}, "timing": {"timing"},
    "timeline": {"timeline"}, "dictionary": {"dictionary"},
    "capability": {"capability"}, "measurement": {"measurement"},
    "manual_annotation": {"annotation"}, "library_status": {"library_status"},
    "duplicate_relation": {"duplicate_relation"}, "authorization": {"authorization"},
    "schema": {"schema"}, "invalid_output": {"invalid_output"},
    "validation_error": {"validation_error"}, "qa": {"qa"},
    "fact": {"script", "fact"}, "existing_copy": {"copy"},
    "packaging": {"packaging"}, "packaging_asset": {"asset", "packaging"},
    "source_overlay": {"annotation", "evidence"},
}


def _reference(ref, records, kinds=None):
    record = records.get(ref)
    if record is None:
        _reject("UNKNOWN_REFERENCE", "模型引用了本次上下文以外的对象")
    if kinds is not None and record["kind"] not in kinds:
        _reject("REFERENCE_KIND_MISMATCH", "引用对象的类型不符合用途")
    return record


def _references(value, records):
    if isinstance(value, dict):
        if set(("kind", "target_ref", "edge")) <= value.keys():
            _reference(value["target_ref"], records, {value["kind"]})
        for key, item in value.items():
            if key.endswith("_refs"):
                for ref in item:
                    _reference(ref, records, _REF_KINDS.get(key[:-5]))
            elif key.endswith("_ref") and item is not None:
                kinds = _REF_KINDS.get(key[:-4])
                if key == "asset_ref" and value.get("kind") in {"subtitle", "bgm", "opening", "info", "sfx", "sticker", "fancy_text", "brand"}:
                    kinds = {"asset", "packaging"}
                _reference(item, records, kinds)
            else:
                _references(item, records)
    elif isinstance(value, list):
        for item in value:
            _references(item, records)


def validate_envelope(request, context: ModelContext):
    validate_schema(request, context.template_id, "Input", context.schema_version)
    if request["request_id"] != context.request_id or request["route"] != context.route or request["orientation"] != context.orientation:
        _reject("REQUEST_MISMATCH", "模型请求与当前任务不对应")
    if request["base_revision"] != context.revision or request["input_digest"] != context.input_digest:
        _reject("STALE_INPUT", "输入已变更，请按当前版本重新生成建议")
    if set(request["allowed_operations"]) != set(context.allowed_operations):
        _reject("OPERATION_NOT_ALLOWED", "请求不能改变服务端允许的操作")
    if set(request["locked_refs"]) != set(context.locked_refs):
        _reject("LOCKED_TARGET_REQUIRES_REVIEW", "人工锁定对象已变化")
    records = {}
    for record in request["records"]:
        ref = record["ref"]
        if ref in records:
            _reject("DUPLICATE_RECORD_ID", "上下文对象编号重复")
        current = context.records.get(ref)
        if current is None:
            _reject("UNAUTHORIZED_REFERENCE", "对象不在当前授权范围")
        if current != record:
            _reject("STALE_REFERENCE", "对象版本或元数据已经变化")
        records[ref] = record
    _references(request["payload"], records)
    for ref in request["locked_refs"]:
        _reference(ref, records)
    return records


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _anchor(anchor, context, *, end=False):
    window = context.anchor_windows.get(anchor["target_ref"])
    if not isinstance(window, TimeWindow):
        _reject("ANCHOR_TIME_UNKNOWN", "锚点尚无真实时间，不能编造秒数")
    if anchor["edge"] == "end" or (anchor["edge"] == "span" and end):
        return window.end_us
    return window.start_us


def _source_window(value, records, context):
    asset = _reference(value["asset_ref"], records, {"asset"})
    segment = _reference(value["segment_ref"], records, {"segment"})
    parent = segment["source_window"]
    if not parent or parent["asset_ref"] != value["asset_ref"]:
        _reject("SOURCE_REFERENCE_MISMATCH", "片段不属于所引用的源素材")
    kind = context.media_kinds.get(value["asset_ref"])
    if kind is None:
        _reject("MEDIA_KIND_UNKNOWN", "缺少服务端确认的素材类型")
    window = TimeWindow(value["start_us"], value["end_us"])
    validate_source_window(window, media_kind=kind, duration_us=asset["duration_us"],
        allowed=TimeWindow(parent["start_us"], parent["end_us"]))
    return asset


def _selected_source(shot, request, records, context, output):
    candidate = _reference(shot["candidate_ref"], records, {"asset", "segment"})
    if shot["candidate_ref"] not in request["payload"]["candidate_refs"]:
        _reject("CANDIDATE_NOT_ALLOWED", "镜头不在本次可选候选中")
    if candidate["kind"] == "segment":
        parent = candidate["source_window"]
        if parent is None:
            _reject("SOURCE_WINDOW_REQUIRED", "视频片段缺少源区间")
        asset_ref = parent["asset_ref"]
    else:
        asset_ref = candidate["ref"]
    asset = _reference(asset_ref, records, {"asset"})
    if asset["orientation"] != context.orientation:
        _reject("ORIENTATION_CONFLICT", "取用素材与本片画幅方向不一致")
    kind = context.media_kinds.get(asset_ref)
    if kind not in {"image", "video"}:
        _reject("MEDIA_KIND_UNKNOWN", "候选尚无确认的画面素材类型")
    source = shot["source_window"]
    if source is not None:
        if source["asset_ref"] != asset_ref or (candidate["kind"] == "segment" and source["segment_ref"] != candidate["ref"]):
            _reject("SOURCE_REFERENCE_MISMATCH", "取用区间与所选素材片段不一致")
        window = TimeWindow(source["start_us"], source["end_us"])
    else:
        window = None
    validate_source_window(window, media_kind=kind, duration_us=asset["duration_us"])
    if window is not None and window.duration_us < output.duration_us:
        _reject("SOURCE_TOO_SHORT", "片段不足以覆盖镜头，不能循环或拉长凑时长")


def _pool(request, context):
    return set(request["payload"].get("candidate_refs", context.candidate_refs))


def _candidate_choice(node, request, records, context):
    asset = _reference(node["asset_ref"], records, {"asset"})
    segment_ref = node["segment_ref"]
    if node["asset_ref"] not in _pool(request, context) and segment_ref not in _pool(request, context):
        _reject("CANDIDATE_NOT_ALLOWED", "候选不在本次允许的素材池中")
    if asset["orientation"] != context.orientation:
        _reject("ORIENTATION_CONFLICT", "候选画幅方向与本片不一致")
    kind = context.media_kinds.get(node["asset_ref"])
    if kind not in {"image", "video"}:
        _reject("MEDIA_KIND_UNKNOWN", "候选尚无服务端确认的类型")
    if kind == "image":
        if segment_ref is not None or node["window"] is not None:
            _reject("IMAGE_HAS_SOURCE_WINDOW", "照片不应伪造视频片段或源入出点")
        return
    if segment_ref is None or node["window"] is None:
        _reject("SOURCE_WINDOW_REQUIRED", "视频推荐必须明确可用片段区间")
    if node["window"]["asset_ref"] != node["asset_ref"] or node["window"]["segment_ref"] != segment_ref:
        _reject("SOURCE_REFERENCE_MISMATCH", "候选与取用区间不对应")
    _source_window(node["window"], records, context)


_OPERATION_TARGET_KINDS = {
    "replace_shot": {"shot"}, "remove_shot": {"shot"}, "trim_shot": {"shot"},
    "add_shot": {"script", "shot"}, "reorder_in_segment": {"script", "shot"},
    "add_element": {"script", "shot", "timeline", "packaging"},
    "remove_element": {"packaging"}, "update_element": {"packaging"},
}


def _operation(operation, request, records, context):
    target = _reference(operation["target_ref"], records, _OPERATION_TARGET_KINDS[operation["kind"]])
    requested_targets = request["payload"].get("target_refs", context.editable_refs)
    if operation["target_ref"] not in context.editable_refs or operation["target_ref"] not in requested_targets:
        _reject("TARGET_NOT_ALLOWED", "该对象未被授权执行本次修改")
    for ref in operation["order_refs"]:
        _reference(ref, records, {"shot"})
        if ref not in context.editable_refs or ref in context.locked_refs:
            _reject("TARGET_NOT_ALLOWED", "重排涉及未允许修改的镜头")
    if operation["kind"] in {"replace_shot", "add_shot"}:
        candidate = _reference(operation["candidate_ref"], records, {"asset", "segment"}) if operation["candidate_ref"] else None
        if candidate is None:
            _reject("SOURCE_WITHOUT_REFERENCE", "替换或增加镜头必须指定真实候选")
        source = operation["source_window"]
        if candidate["kind"] == "asset":
            asset_ref, segment_ref = candidate["ref"], source["segment_ref"] if source else None
        else:
            if not candidate["source_window"]:
                _reject("SOURCE_WINDOW_REQUIRED", "片段缺少实际源区间")
            asset_ref, segment_ref = candidate["source_window"]["asset_ref"], candidate["ref"]
        _candidate_choice({"asset_ref":asset_ref, "segment_ref":segment_ref, "window":source}, request, records, context)
        if operation["kind"] == "replace_shot":
            output = context.anchor_windows.get(target["ref"])
            if output is None:
                _reject("ANCHOR_TIME_UNKNOWN", "原镜头时长尚未确定")
            if source and source["end_us"] - source["start_us"] < output.duration_us:
                _reject("SOURCE_TOO_SHORT", "替换素材不足以覆盖原镜头")
    elif operation["candidate_ref"] is not None:
        _reference(operation["candidate_ref"], records, {"asset", "packaging"})
        if operation["candidate_ref"] not in _pool(request, context):
            _reject("CANDIDATE_NOT_ALLOWED", "包装素材不在允许候选中")


def _presenter(shot, records, context, output):
    if not context.presenter_ref or shot["candidate_ref"] not in {None, context.presenter_ref}:
        _reject("PRESENTER_SOURCE_REQUIRED", "数字人镜头必须绑定本片真实底片")
    asset = _reference(context.presenter_ref, records, {"asset"})
    if context.media_kinds.get(context.presenter_ref) != "video" or asset["duration_us"] is None:
        _reject("PRESENTER_SOURCE_REQUIRED", "底片尚无有效视频时长")
    if asset["orientation"] != context.orientation:
        _reject("ORIENTATION_CONFLICT", "底片与本片画幅方向不一致")
    if not isinstance(context.main_audio_rate, PlaybackRate):
        _reject("PLAYBACK_RATE_UNKNOWN", "底片尚未确认实际播放速度")
    if output.end_us > context.main_audio_rate.output_us(asset["duration_us"]):
        _reject("SOURCE_OUT_OF_BOUNDS", "数字人镜头超过底片换算后的时间")
    source = shot["source_window"]
    if source is not None:
        if source["asset_ref"] != context.presenter_ref:
            _reject("SOURCE_REFERENCE_MISMATCH", "不能用其他素材冒充数字人底片")
        if output_window(TimeWindow(source["start_us"],source["end_us"]), context.main_audio_rate) != output:
            _reject("PRESENTER_TIME_MISMATCH", "底片源时间与当前主声音不同步")


def validate_result(request, response, context: ModelContext):
    records = validate_envelope(request, context)
    validate_schema(response, context.template_id, "Output", context.schema_version)
    result = response["result"]
    if response["status"] == "ok" and result is None:
        _reject("OK_WITHOUT_RESULT", "成功响应缺少实际建议")
    if response["status"] == "needs_input" and not (response["unknowns"] or response["clarifications"]):
        _reject("MISSING_CLARIFICATION", "需补充输入时必须说明缺少什么")
    _references(response, records)
    try:
        for node in _walk(result):
            operations = node.get("operations", node.get("proposed_operations", []))
            if operations and response["status"] == "needs_input":
                _reject("AMBIGUOUS_REQUEST_HAS_OPERATIONS", "尚待澄清时不能返回可执行修改")
            for operation in operations:
                if operation["kind"] not in context.allowed_operations:
                    _reject("OPERATION_NOT_ALLOWED", "模型建议了未允许的操作")
                if operation["target_ref"] in context.locked_refs:
                    _reject("LOCKED_TARGET_REQUIRES_REVIEW", "建议涉及已锁定内容，需重新核对")
                _operation(operation, request, records, context)
            if {"asset_ref", "segment_ref", "window", "reason", "limitations"} <= node.keys():
                _candidate_choice(node, request, records, context)
            if {"asset_ref", "segment_ref", "start_us", "end_us"} <= node.keys():
                asset = _source_window(node, records, context)
                if context.template_id in {"P05", "P06", "P07", "P13"} and context.media_kinds.get(node["asset_ref"]) in {"image", "video"} and asset["orientation"] != context.orientation:
                    _reject("ORIENTATION_CONFLICT", "取用素材与本片画幅方向不一致")
            if "fact_refs" in node:
                if node.get("text") and not node["fact_refs"]:
                    _reject("FACT_EVIDENCE_MISSING", "生成文字必须有已批准的事实依据")
                if not set(node["fact_refs"]) <= context.approved_fact_refs:
                    _reject("FACT_EVIDENCE_MISSING", "事实引用尚未批准")
        if context.template_id == "P06" and result is not None:
            groups = result["groups"]
            scripts = request["payload"]["script_refs"]
            if len(groups) != len(scripts) or {x["script_ref"] for x in groups} != set(scripts):
                _reject("SCRIPT_COVERAGE_MISMATCH", "分镜必须对应本次全部文案段落")
            for group in groups:
                windows = []
                for shot in group["shots"]:
                    if shot["kind"] == "gap" and (shot["candidate_ref"] or shot["source_window"]):
                        _reject("GAP_HAS_FAKE_MEDIA", "素材缺口不能伪装成已匹配镜头")
                    if shot["kind"] == "presenter" and context.route != "digital":
                        _reject("AD_HAS_PRESENTER_FALLBACK", "信息流不能使用数字人底片兜底")
                    if shot["kind"] == "source" and not shot["candidate_ref"]:
                        _reject("SOURCE_WITHOUT_REFERENCE", "镜头缺少真实素材引用")
                    output = TimeWindow(_anchor(shot["start_anchor"], context), _anchor(shot["end_anchor"], context, end=True))
                    if shot["kind"] == "source":
                        _selected_source(shot, request, records, context, output)
                    elif shot["kind"] == "presenter":
                        _presenter(shot, records, context, output)
                    windows.append(output)
                expected = context.anchor_windows.get(group["script_ref"])
                if expected is None:
                    _reject("ANCHOR_TIME_UNKNOWN", "文案段落尚无真实时长")
                validate_coverage(windows, expected)
        if context.template_id == "P14" and result is not None:
            for suggestion in result["suggestions"]:
                text = records[suggestion["script_ref"]]["text"] or ""
                start, end = suggestion["start_char"], suggestion["end_char"]
                if end <= start or end > len(text) or text[start:end] != suggestion["original_text"]:
                    _reject("SCRIPT_SPAN_MISMATCH", "读法建议与原文字词位置不一致")
    except TimelineContractError as error:
        _reject(error.code, "模型建议的时间区间无效，请重新核对")
    return deepcopy(response)
