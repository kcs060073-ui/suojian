"""Bounded, non-destructive rough-cut edits for scene cards.

The digital-human narration file is a read-only, continuous task asset.  This
module edits only the scene-card projection that chooses which visual is shown
over that narration.  It deliberately has no timeline-track or source-file API.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import secrets
import time
import uuid

from .ad_matching import TRANSITION_HANDLE_SECONDS, _direction, display_dimensions
from .rough_packaging_impact import affected_packaging
from .scene_cards import VALID_MIX_FORMS, picture_view_digest
from .script_segments import storyboard_digest
from .task_sources import scoped_asset_location
from .timeline_contract import TimeWindow, seconds_to_us, validate_coverage
from .visual_uniqueness import validate_explicit_visual_duplicates
from .workflow import WORKFLOW_STAGES, invalidate_from_connection


_CARD_COLUMNS = (
    "card_id",
    "task_id",
    "idx",
    "narrative_text",
    "narrative_start",
    "narrative_end",
    "asset_id",
    "segment_id",
    "mix_form",
    "enter_tc",
    "exit_tc",
    "audio_policy_draft",
    "source_type",
    "system_suggestion",
    "final_decision",
    "confirm_status",
    "created_at",
    "updated_at",
    "script_segment_id",
    "shot_revision",
)
_TASK_ASSET_COLUMNS = ("task_id", "asset_id", "role", "created_at")
_DELETEABLE_MIX_FORMS = {"broll全屏", "信息卡"}
_ACTIONS = {"replace", "reorder", "trim", "delete", "split", "add_after"}
_VISUAL_PAYLOAD_COLUMNS = (
    "asset_id",
    "segment_id",
    "mix_form",
    "enter_tc",
    "exit_tc",
    "audio_policy_draft",
    "source_type",
    "system_suggestion",
    "final_decision",
    "confirm_status",
)
_TOKEN_TTL_SECONDS = 300
_TOKEN_SECRET = secrets.token_bytes(32)


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_task(connection, task_id: str) -> None:
    if connection.execute(
        "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone() is None:
        raise KeyError(f"任务不存在：{task_id}")


def _snapshot_state(connection, task_id: str) -> dict:
    _require_task(connection, task_id)
    cards = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id",
            (task_id,),
        ).fetchall()
    ]
    task_assets = [
        dict(row)
        for row in connection.execute(
            """
            SELECT task_id,asset_id,role,created_at
            FROM task_assets WHERE task_id=? ORDER BY asset_id
            """,
            (task_id,),
        ).fetchall()
    ]
    return {"cards": cards, "task_assets": task_assets}


def _card(state: dict, card_id: str) -> dict:
    for card in state["cards"]:
        if card["card_id"] == card_id:
            return card
    raise KeyError(f"场景卡片不存在：{card_id}")


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是有限数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须是有限数字") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数字")
    return number


def _normalized_edit(edit: dict) -> dict:
    if not isinstance(edit, dict):
        raise TypeError("edit 必须是对象")
    action = edit.get("action")
    if action not in _ACTIONS:
        raise ValueError(f"未知粗剪动作：{action}")
    return copy.deepcopy(edit)


def _segment(connection, segment_id: str, asset_id: str) -> dict:
    from .segment_marks import resolve_source_segment
    return resolve_source_segment(connection, asset_id, segment_id)


def _measured_duration(connection, asset_id: str) -> float | None:
    row = connection.execute(
        "SELECT result_json FROM analysis_runs WHERE asset_id=? "
        "AND stage='technical_probe' AND status='completed'",
        (asset_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        result = json.loads(row["result_json"] or "{}")
        value = result.get("duration_seconds")
        if value is None:
            value = (result.get("format") or {}).get("duration")
        duration = _finite_number(value, "素材探测时长")
    except (TypeError, ValueError, AttributeError):
        return None
    return duration if duration > 0 else None


def _narrative_window(card: dict) -> tuple[float, float]:
    start = _finite_number(card.get("narrative_start"), "叙事开始时间")
    end = _finite_number(card.get("narrative_end"), "叙事结束时间")
    if start < 0 or end <= start:
        raise ValueError("叙事时间顺序或边界无效")
    return start, end


def _task_asset_role(state: dict, asset_id: str | None) -> str | None:
    if asset_id is None:
        return None
    for item in state["task_assets"]:
        if item["asset_id"] == asset_id:
            return item["role"]
    return None


def _is_digital_anchor(state: dict, card: dict) -> bool:
    return (
        card.get("mix_form") == "数字人全屏"
        or _task_asset_role(state, card.get("asset_id")) == "digital_human"
    )


def _asset_media_kind(connection, asset_id: str) -> str:
    row = connection.execute(
        "SELECT media_kind FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"素材未入册：{asset_id}")
    return row["media_kind"]


def _required_ad_ratio(connection, task_id: str) -> str | None:
    task = connection.execute(
        "SELECT task_type,brief_json FROM tasks WHERE task_id=?", (task_id,),
    ).fetchone()
    if task is not None and task["task_type"] in {"digital_human", "数字人混剪"}:
        return "9:16"
    if task is None or task["task_type"] != "ad_mix":
        return None
    try:
        brief = json.loads(task["brief_json"] or "{}")
        required = brief.get("aspect_ratio")
    except (TypeError, ValueError, AttributeError):
        required = None
    # Reading archived tasks does not call this guard. New edits and renders
    # must not silently assume an orientation for old, incomplete briefs.
    if required not in {"9:16", "16:9"}:
        raise ValueError(
            "AD_ASPECT_RATIO_REQUIRED: 旧广告任务缺少冻结画幅；"
            "请先确认原任务画幅并重选同向素材"
        )
    return required


def _bound_main_direction_probe(connection, task_id: str):
    """Read legacy main-track geometry once per validation, without catalog writes."""
    from types import SimpleNamespace
    from .rough_media import bound_digital_source_location
    from .visual_media_samples import _source
    from .render_mix_adapter import _file_signature
    from .probe import probe_media
    from .tools import find_ffprobe, default_ffprobe_search_roots
    import os
    import subprocess

    reader = SimpleNamespace(connection=connection)
    cache = {}

    def probe(asset_id):
        if asset_id in cache:
            return cache[asset_id]
        location = bound_digital_source_location(reader, task_id, asset_id)
        if location is None:
            cache[asset_id] = None
            return None
        source, _kind, _fingerprint = _source(
            reader, asset_id, location["library_id"], location["relative_path"])
        before = _file_signature(source)
        try:
            result = probe_media(source, find_ffprobe(
                os.environ, search_roots=default_ffprobe_search_roots()))
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            raise ValueError(
                "DIGITAL_MEDIA_PROBE_FAILED: 无法检测原始口播的展示方向，请检查文件可访问后重试"
            ) from error
        if before != _file_signature(source):
            raise ValueError("DIGITAL_SOURCE_CHANGED: 原始口播在检测期间变化，请重新核对源文件后重试")
        cache[asset_id] = result
        return result

    return probe


def _validate_ad_asset_direction(connection, task_id: str, asset_id: str,
                                 media_kind: str, *, probe_missing=None) -> None:
    required = _required_ad_ratio(connection, task_id)
    if required is None:
        return
    task_type = connection.execute("SELECT task_type FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
    prefix = "AD" if task_type == "ad_mix" else "DIGITAL"
    row = connection.execute(
        "SELECT result_json FROM analysis_runs WHERE asset_id=? "
        "AND stage='technical_probe' AND status='completed' "
        "ORDER BY updated_at DESC LIMIT 1", (asset_id,),
    ).fetchone()
    try:
        probe = json.loads(row["result_json"]) if row is not None else None
        dimensions = display_dimensions(media_kind, probe)
        actual = _direction(media_kind, probe) if dimensions else None
    except (TypeError, ValueError):
        dimensions = None
        actual = None
    if dimensions is None:
        missing = probe_missing or _bound_main_direction_probe(connection, task_id)
        probe = missing(asset_id)
        dimensions = display_dimensions(media_kind, probe)
        actual = _direction(media_kind, probe) if dimensions else None
    if dimensions is None:
        raise ValueError(
            f"{prefix}_ORIENTATION_UNKNOWN: 尚未取得该素材的有效展示方向；"
            "请在素材选择中检测所选素材，完成后再替换"
        )
    if actual != required:
        raise ValueError(
            f"{prefix}_ORIENTATION_MISMATCH: 素材 {asset_id} 的展示方向"
            f"{('横屏' if actual == '16:9' else '竖屏') if actual else '方形'}与任务 {required} 不一致；请选择同向素材"
        )


def _expected_visual_role(media_kind: str, mix_form: str) -> str:
    if mix_form == "broll全屏":
        if media_kind not in ("video", "image"):
            raise ValueError("B-roll 全屏混剪形态只能使用 video 或 image 媒体")
        return "broll"
    if mix_form == "信息卡":
        if media_kind != "image":
            raise ValueError("信息卡混剪形态只能使用 image 媒体")
        return "information_card"
    if mix_form == "画中画":
        if media_kind == "video":
            return "broll"
        if media_kind == "image":
            return "information_card"
        raise ValueError("画中画混剪形态只能使用 video 或 image 媒体")
    raise ValueError("数字人口播主轴不可通过 replace 修改")


def _validate_visual_binding(
    state: dict,
    asset_id: str,
    media_kind: str,
    mix_form: str,
    *,
    allow_unregistered: bool = False,
) -> str:
    expected_role = _expected_visual_role(media_kind, mix_form)
    actual_role = _task_asset_role(state, asset_id)
    if actual_role == "digital_human":
        raise ValueError("素材角色 role=digital_human 的连续口播主输入不可作为替换画面")
    if actual_role is None:
        if allow_unregistered:
            return expected_role
        raise ValueError("画面素材缺少任务角色绑定")
    if actual_role != expected_role:
        raise ValueError(
            f"混剪形态、媒体类型与任务素材角色不匹配：需要 {expected_role}，实际 {actual_role}"
        )
    return expected_role


def _source_window(connection, state: dict, card: dict, *, check_direction: bool = True, probe_missing=None) -> tuple[float, float]:
    from .source_windows import assert_usable_source
    narrative_start, narrative_end = _narrative_window(card)
    narrative_duration = narrative_end - narrative_start
    asset_id = card.get("asset_id")
    if not asset_id:
        if _is_digital_anchor(state, card):
            return 0.0, 0.0
        raise ValueError("当前画面缺少素材绑定")
    media_kind = _asset_media_kind(connection, asset_id)
    if check_direction:
        _validate_ad_asset_direction(connection, card["task_id"], asset_id, media_kind, probe_missing=probe_missing)
    raw_start = card.get("enter_tc")
    raw_end = card.get("exit_tc")
    if raw_start is None and raw_end is None:
        source_start = source_end = 0.0
    elif raw_start is None or raw_end is None:
        raise ValueError("源时间窗必须同时提供开始和结束时间")
    else:
        source_start = _finite_number(raw_start, "源开始时间")
        source_end = _finite_number(raw_end, "源结束时间")
    if source_start < 0 or source_end < 0:
        raise ValueError("源时间窗不能为负数")

    segment = None
    if card.get("segment_id"):
        segment = _segment(connection, card["segment_id"], asset_id)

    if media_kind == "image":
        if segment is not None or source_start != 0 or source_end != 0:
            raise ValueError("图片素材的源时间窗必须稳定为 0/0，且不能绑定视频片段")
        assert_usable_source(connection, asset_id, 0, 0, image=True)
        return 0.0, 0.0
    if media_kind != "video":
        raise ValueError("受控画面只能使用 video 或 image 媒体")
    if _is_digital_anchor(state, card):
        if source_start != 0 or source_end != 0:
            raise ValueError("数字人口播主轴源时间窗固定，不可编辑")
        return 0.0, 0.0

    if source_start == 0 and source_end == 0:
        source_start = segment["start_sec"] if segment is not None else 0.0
        source_end = source_start + narrative_duration
    elif source_end <= source_start:
        raise ValueError("源时间窗结束时间必须晚于开始时间")
    if source_end - source_start + 1e-9 < narrative_duration:
        raise ValueError("源时间窗时长不足以覆盖当前叙事槽")
    task = connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (card["task_id"],)
    ).fetchone()
    required_end = source_start + narrative_duration + (
        TRANSITION_HANDLE_SECONDS if task is not None and task["task_type"] == "ad_mix" else 0.0
    )
    if segment is not None and (
        source_start < segment["start_sec"] - 1e-9
        or source_end > segment["end_sec"] + 1e-9
    ):
        raise ValueError("源时间窗越过素材片段边界")
    if segment is not None and required_end > segment["end_sec"] + 1e-9:
        raise ValueError("广告转场缺少真实源画面：源时间窗越过素材片段边界")
    # Historical segment metadata cannot authorize frames beyond the actual file.
    duration = _measured_duration(connection, asset_id)
    if duration is not None and source_end > duration + 1e-9:
        raise ValueError("源时间窗越过素材探测时长边界")
    if duration is not None and required_end > duration + 1e-9:
        raise ValueError("广告转场缺少真实源画面：源时间窗越过素材探测时长边界")
    assert_usable_source(connection, asset_id, round(source_start * 1_000_000),
                         round(max(source_end, required_end) * 1_000_000))
    return source_start, source_end


def validate_task_visual_directions(connection, task_id: str, *, probe_missing=None) -> None:
    """Validate selected footage, including a digital main track hidden by inserts.

    Packaging Logo/sticker/title text assets are intentionally not queried here.
    Historical read-only views do not call this new-work guard.
    """
    if _required_ad_ratio(connection, task_id) is None:
        return
    selected = connection.execute(
        "SELECT DISTINCT asset_id FROM scene_cards WHERE task_id=? AND asset_id IS NOT NULL "
        "UNION SELECT asset_id FROM task_assets WHERE task_id=? AND role='digital_human'",
        (task_id, task_id),
    ).fetchall()
    for row in selected:
        _validate_ad_asset_direction(connection, task_id, row["asset_id"],
                                     _asset_media_kind(connection, row["asset_id"]),
                                     probe_missing=probe_missing)


def validate_ad_source_windows(connection, task_id: str) -> None:
    """Recheck persisted ad windows, including locks made by older builds."""
    _required_ad_ratio(connection, task_id)
    state = _snapshot_state(connection, task_id)
    for card in state["cards"]:
        if card["asset_id"]:
            _source_window(connection, state, card)


def resolved_ad_source_windows(connection, task_id: str, *, include_render_handles=False) -> dict[str, dict]:
    """Expose the same validated source positions used by picture lock to renderers."""
    _required_ad_ratio(connection, task_id)
    state = _snapshot_state(connection, task_id)
    result = {}
    for card in state["cards"]:
        if not card["asset_id"]:
            continue
        start, end = _source_window(connection, state, card)
        result[card["card_id"]] = {
            "media_kind": _asset_media_kind(connection, card["asset_id"]),
            "source_start": start,
            "source_end": end,
        }
        if include_render_handles:
            task = connection.execute("SELECT task_type FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            # _source_window already checked this extra render interval against
            # the segment, measured file duration and current human exclusions.
            handle = TRANSITION_HANDLE_SECONDS if task["task_type"] == "ad_mix" and result[card["card_id"]]["media_kind"] == "video" else 0.0
            lo, hi = _narrative_window(card)
            result[card["card_id"]]["validated_transition_end"] = max(end, start + hi - lo + handle) if handle else end
    return result


def _validate_state(connection, state: dict, task_id: str | None = None,
                    *, check_uniqueness: bool = True,
                    direction_card_ids: set[str] | None = None) -> None:
    ids = [card.get("card_id") for card in state["cards"]]
    if any(not isinstance(card_id, str) or not card_id for card_id in ids):
        raise ValueError("场景卡片 ID 必须是非空字符串")
    if len(ids) != len(set(ids)):
        raise ValueError("场景卡片 ID 必须唯一")
    route_task_id = task_id or (state["cards"][0]["task_id"] if state["cards"] else None)
    route = connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (route_task_id,)
    ).fetchone() if route_task_id else None
    digital_route = route is not None and route["task_type"] == "digital_human"
    probe_missing = _bound_main_direction_probe(connection, route_task_id)
    visual_uses = []
    for card in state["cards"]:
        narrative_start, narrative_end = _narrative_window(card)
        if card.get("mix_form") not in VALID_MIX_FORMS:
            raise ValueError(f"混剪形态必须是 {VALID_MIX_FORMS} 之一")
        if _is_digital_anchor(state, card):
            if digital_route and card.get("audio_policy_draft") != "keep":
                raise ValueError("数字人口播原画面必须保留主声音")
            if card.get("asset_id"):
                kind = _asset_media_kind(connection, card["asset_id"])
                if kind != "video" or _task_asset_role(state, card["asset_id"]) != "digital_human":
                    raise ValueError("数字人口播锚点必须绑定 role=digital_human 的视频")
            _source_window(connection, state, card, check_direction=(
                direction_card_ids is None or card["card_id"] in direction_card_ids), probe_missing=probe_missing)
            continue
        asset_id = card.get("asset_id")
        if not asset_id:
            if route is not None and route["task_type"] == "ad_mix":
                continue  # Missing ad shots remain editable until the picture-lock gate.
            raise ValueError("非数字人画面必须绑定素材")
        kind = _asset_media_kind(connection, asset_id)
        if digital_route and card.get("audio_policy_draft") != "mute":
            raise ValueError("数字人口播补充画面必须静音并保留主声音")
        _validate_visual_binding(state, asset_id, kind, card["mix_form"])
        source_start, _source_end = _source_window(connection, state, card, check_direction=(
            direction_card_ids is None or card["card_id"] in direction_card_ids), probe_missing=probe_missing)
        visual_uses.append({
            "card_id": card["card_id"], "asset_id": asset_id, "kind": kind,
            "start": source_start,
            "end": source_start + (narrative_end - narrative_start) if kind == "video" else None,
        })
    if check_uniqueness:
        validate_explicit_visual_duplicates(connection, visual_uses)
    if task_id is None and state["cards"]:
        task_id = state["cards"][0]["task_id"]
    if task_id is not None:
        task = connection.execute(
            "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        try:
            active_revision = (json.loads(task["brief_json"] or "{}")
                               .get("final_script") or {}).get("sha256") if task else None
        except (ValueError, AttributeError):
            active_revision = None
        if not active_revision:
            latest = connection.execute(
                "SELECT script_revision FROM script_segments "
                "WHERE task_id=? AND script_revision<>'legacy' ORDER BY rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            active_revision = latest["script_revision"] if latest else None
        if active_revision is None:
            return
        for segment in connection.execute(
            "SELECT * FROM script_segments WHERE task_id=? AND script_revision=?",
            (task_id, active_revision),
        ):
            shots = [card for card in state["cards"]
                     if card.get("script_segment_id") == segment["segment_id"]]
            if any(card.get("narrative_text") != segment["text"] for card in shots):
                raise ValueError("SEGMENT_TEXT: 镜头文案与所属文案段不一致")
            windows = sorted(
                (TimeWindow(seconds_to_us(card["narrative_start"]),
                            seconds_to_us(card["narrative_end"])) for card in shots),
                key=lambda window: window.start_us,
            )
            validate_coverage(windows, TimeWindow(
                segment["output_start_us"], segment["output_end_us"]
            ))


def _allowed_asset(db, task_id: str, asset_id: str) -> dict:
    asset = db.connection.execute(
        "SELECT asset_id,media_kind FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if asset is None:
        raise KeyError(f"素材未入册：{asset_id}")
    location = scoped_asset_location(db, task_id, asset_id)
    if location is None:
        from .rough_media import bound_digital_source_location, _available_source
        bound = bound_digital_source_location(db, task_id, asset_id)
        if bound is not None:
            _available_source(db, asset_id, bound["library_id"], bound["relative_path"])
            location = {**bound, "source_mode": "primary"}
    if location is None:
        raise ValueError("替代素材不在本任务允许的主素材或通用素材范围内")
    if location["source_mode"] == "default_generic":
        setting = db.connection.execute(
            """
            SELECT include_default_generic FROM task_source_settings
            WHERE task_id=?
            """,
            (task_id,),
        ).fetchone()
        if setting is None or not setting["include_default_generic"]:
            raise ValueError("默认通用素材未被本任务允许")
    return {**dict(asset), **location}


def _append_task_asset(connection, state: dict, task_id: str, asset_id: str, role: str) -> bool:
    if any(item["asset_id"] == asset_id for item in state["task_assets"]):
        return False
    now = connection.execute("SELECT datetime('now')").fetchone()[0]
    state["task_assets"].append(
        {
            "task_id": task_id,
            "asset_id": asset_id,
            "role": role,
            "created_at": now,
        }
    )
    state["task_assets"].sort(key=lambda item: item["asset_id"])
    return True


def _renumber(cards: list[dict]) -> None:
    for index, card in enumerate(cards):
        card["idx"] = index


def _apply_replace(db, task_id: str, state: dict, edit: dict) -> list[str]:
    card = _card(state, str(edit.get("card_id", "")))
    task = db.connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    digital_route = task is not None and task["task_type"] == "digital_human"
    if _is_digital_anchor(state, card) and not digital_route:
        raise ValueError("数字人口播主轴是不可编辑锚点，不能替换")
    asset_id = str(edit.get("asset_id", ""))
    if not asset_id:
        raise ValueError("替换素材不能为空")
    asset = _allowed_asset(db, task_id, asset_id)
    segment_id = edit.get("segment_id")
    segment = None
    if segment_id is not None:
        segment = _segment(db.connection, str(segment_id), asset_id)
        segment_id = str(segment_id)
    selected_role = _task_asset_role(state, asset_id)
    mix_form = edit.get("mix_form", "数字人全屏" if selected_role == "digital_human"
                        else "broll全屏" if card["mix_form"] == "数字人全屏"
                        else card["mix_form"])
    if mix_form not in VALID_MIX_FORMS:
        raise ValueError(f"混剪形态必须是 {VALID_MIX_FORMS} 之一")
    restoring_original = digital_route and selected_role == "digital_human"
    if restoring_original:
        if mix_form != "数字人全屏" or asset["media_kind"] != "video" or segment is not None:
            raise ValueError("数字人口播原画面必须使用主输入、固定源时间和数字人全屏形态")
        role = "digital_human"
    else:
        role = _validate_visual_binding(
            state, asset_id, asset["media_kind"], mix_form, allow_unregistered=True
        )
    narrative_start, narrative_end = _narrative_window(card)
    duration = narrative_end - narrative_start
    explicit_start = None
    if "source_start" in edit:
        if type(edit["source_start"]) not in (int, float):
            raise ValueError("源开始时间必须是有限数字")
        explicit_start = _finite_number(edit["source_start"], "源开始时间")
        if explicit_start < 0:
            raise ValueError("源开始时间不能为负数")
    if restoring_original or asset["media_kind"] == "image":
        if explicit_start not in (None, 0.0):
            raise ValueError("图片或数字人口播原画面不能改变固定源时间")
        if segment is not None:
            raise ValueError("图片素材不能绑定视频片段")
        source_start = source_end = 0.0
    else:
        source_start = explicit_start if explicit_start is not None else (segment["start_sec"] if segment is not None else 0.0)
        source_end = source_start + duration
        if segment is not None and source_end > segment["end_sec"] + 1e-9:
            raise ValueError("替换片段时长不足以覆盖当前叙事槽")

    card["asset_id"] = asset_id
    card["segment_id"] = segment_id
    card["mix_form"] = mix_form
    card["enter_tc"] = source_start
    card["exit_tc"] = source_end
    if digital_route:
        card["audio_policy_draft"] = "keep" if restoring_original else "mute"
    card["updated_at"] = db.connection.execute("SELECT datetime('now')").fetchone()[0]
    _append_task_asset(db.connection, state, task_id, asset_id, role)
    return [card["card_id"]]


def _apply_reorder(connection, state: dict, edit: dict, now: str) -> list[str]:
    requested = edit.get("card_ids")
    if not isinstance(requested, list) or any(
        not isinstance(card_id, str) for card_id in requested
    ):
        raise ValueError("排序必须恰好包含当前每张卡片一次")
    current_ids = [card["card_id"] for card in state["cards"]]
    if (
        len(requested) != len(current_ids)
        or len(set(requested)) != len(requested)
        or set(requested) != set(current_ids)
    ):
        raise ValueError("排序必须恰好包含当前每张卡片一次")
    if requested == current_ids:
        return []
    fixed_slots = state["cards"]
    by_id = {card["card_id"]: card for card in fixed_slots}
    route = connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (fixed_slots[0]["task_id"],)
    ).fetchone() if fixed_slots else None
    digital_route = route is not None and route["task_type"] == "digital_human"
    for index, slot in enumerate(fixed_slots):
        if not digital_route and _is_digital_anchor(state, slot) and requested[index] != slot["card_id"]:
            raise ValueError("数字人口播锚点位置固定，不能重排")
        if slot.get("script_segment_id") != by_id[requested[index]].get("script_segment_id"):
            raise ValueError("镜头只能在同一文案段内调整顺序")
    requested_visuals = [
        by_id[card_id] for card_id in requested
        if digital_route or not _is_digital_anchor(state, by_id[card_id])
    ]
    target_slots = [card for card in fixed_slots
                    if digital_route or not _is_digital_anchor(state, card)]
    payloads = []
    for source in requested_visuals:
        payload = {column: copy.deepcopy(source[column]) for column in _VISUAL_PAYLOAD_COLUMNS}
        source_start, source_end = _source_window(connection, state, source)
        payload["enter_tc"] = source_start
        payload["exit_tc"] = source_end
        payloads.append(payload)
    affected = []
    for slot, payload in zip(target_slots, payloads, strict=True):
        if digital_route and payload["asset_id"] and not (
            payload["mix_form"] == "数字人全屏"
        ) and _asset_media_kind(connection, payload["asset_id"]) == "video":
            payload["exit_tc"] = payload["enter_tc"] + (
                slot["narrative_end"] - slot["narrative_start"]
            )
        if any(slot[column] != payload[column] for column in _VISUAL_PAYLOAD_COLUMNS):
            slot.update(payload)
            slot["updated_at"] = now
            affected.append(slot["card_id"])
    return affected


def _apply_trim(connection, state: dict, edit: dict, now: str) -> list[str]:
    card = _card(state, str(edit.get("card_id", "")))
    if _is_digital_anchor(state, card):
        raise ValueError("数字人口播主轴是不可编辑锚点，不能裁剪")
    enter_tc = _finite_number(edit.get("enter_tc"), "裁剪入点")
    exit_tc = _finite_number(edit.get("exit_tc"), "裁剪出点")
    if exit_tc <= enter_tc:
        raise ValueError("裁剪出点必须晚于入点")
    if not card["asset_id"] or _asset_media_kind(connection, card["asset_id"]) != "video":
        raise ValueError("当前画面没有可裁剪的视频素材")
    if card["segment_id"]:
        segment = _segment(connection, card["segment_id"], card["asset_id"])
        lower, upper = float(segment["start_sec"]), float(segment["end_sec"])
    else:
        measured = _measured_duration(connection, card["asset_id"])
        if measured is None:
            raise ValueError("当前视频缺少可核验的素材探测时长边界")
        lower, upper = 0.0, measured
    if enter_tc < lower or exit_tc > upper:
        raise ValueError("裁剪范围越过素材片段边界")
    if card["narrative_start"] is None or card["narrative_end"] is None:
        raise ValueError("当前画面缺少可核验的场景时长")
    scene_duration = float(card["narrative_end"] - card["narrative_start"])
    if round((exit_tc - enter_tc) * 1_000_000) < round(scene_duration * 1_000_000):
        raise ValueError("裁剪后的素材时长不足以覆盖当前场景")
    card["enter_tc"] = enter_tc
    card["exit_tc"] = exit_tc
    card["updated_at"] = now
    return [card["card_id"]]


def _apply_delete(connection, state: dict, edit: dict, now: str) -> list[str]:
    card = _card(state, str(edit.get("card_id", "")))
    route = connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (card["task_id"],)
    ).fetchone()
    digital_route = route is not None and route["task_type"] == "digital_human"
    if not digital_route and (
        _is_digital_anchor(state, card) or card["mix_form"] not in _DELETEABLE_MIX_FORMS
    ):
        raise ValueError("只能删除 B-roll 全屏或信息卡；连续口播主轴不可删除")
    index = state["cards"].index(card)
    group = card.get("script_segment_id")
    neighbors = [
        state["cards"][position]
        for position in (index - 1, index + 1)
        if 0 <= position < len(state["cards"])
        and state["cards"][position].get("script_segment_id") == group
        and (digital_route or not _is_digital_anchor(state, state["cards"][position]))
    ]
    if not neighbors:
        raise ValueError("SEGMENT_WITHOUT_SLOT: 本段至少保留一个可延长的镜头")
    replacement = None
    for neighbor in neighbors:
        trial = copy.deepcopy(neighbor)
        source_start, source_end = _source_window(connection, state, neighbor)
        extra = card["narrative_end"] - card["narrative_start"]
        if neighbor["narrative_end"] <= card["narrative_start"]:
            trial["narrative_end"] = card["narrative_end"]
        elif neighbor["narrative_start"] >= card["narrative_end"]:
            trial["narrative_start"] = card["narrative_start"]
        else:
            continue
        if not _is_digital_anchor(state, trial) and _asset_media_kind(connection, trial["asset_id"]) == "video":
            trial["enter_tc"], trial["exit_tc"] = source_start, source_end + extra
        try:
            _source_window(connection, state, trial)
        except ValueError:
            continue
        replacement = trial
        break
    if replacement is None:
        raise ValueError("相邻镜头素材时长不足以补足删除后的文案段")
    neighbor.update(replacement)
    neighbor["updated_at"] = now
    state["cards"] = [item for item in state["cards"] if item["card_id"] != card["card_id"]]
    _renumber(state["cards"])
    return [card["card_id"], neighbor["card_id"]]


def _new_card_id() -> str:
    return f"card-{uuid.uuid4().hex}"


def _apply_add_after(db, task_id: str, state: dict, edit: dict, now: str,
                     generated_card_ids: list[str] | None) -> list[str]:
    card = _card(state, str(edit.get("card_id", "")))
    route = db.connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    digital_original = (route is not None and route["task_type"] == "digital_human"
                        and _is_digital_anchor(state, card))
    if not (route is not None and route["task_type"] == "digital_human") and (
        _is_digital_anchor(state, card) or card["mix_form"] not in _DELETEABLE_MIX_FORMS
    ):
        raise ValueError("只能在可编辑的画面镜头后添加镜头")
    duration = _finite_number(edit.get("duration"), "新镜头时长")
    start, end = _narrative_window(card)
    if duration < 0.5 or end - start - duration < 0.5 - 1e-9:
        raise ValueError("原镜头和新镜头都至少保留 0.5 秒时长")
    new_id = (generated_card_ids or [_new_card_id()])
    if len(new_id) != 1 or new_id[0] in {item["card_id"] for item in state["cards"]}:
        raise ValueError("新增镜头 ID 无效")
    split = round(end - duration, 6)
    source_start, _ = _source_window(db.connection, state, card)
    added = copy.deepcopy(card)
    added.update(card_id=new_id[0], shot_revision=1, narrative_start=split,
                 narrative_end=end, created_at=now, updated_at=now)
    card["narrative_end"] = split
    if not digital_original and _asset_media_kind(db.connection, card["asset_id"]) == "video":
        card["enter_tc"], card["exit_tc"] = source_start, source_start + split - start
    card["updated_at"] = now
    position = state["cards"].index(card)
    state["cards"].insert(position + 1, added)
    _renumber(state["cards"])
    _apply_replace(db, task_id, state, {
        "card_id": new_id[0], "asset_id": edit.get("asset_id"),
        "segment_id": edit.get("segment_id"),
        "mix_form": edit.get("mix_form", "broll全屏"),
        **({"source_start": edit["source_start"]} if "source_start" in edit else {}),
    })
    return [card["card_id"], new_id[0]]


def _apply_split(
    connection,
    state: dict,
    edit: dict,
    now: str,
    generated_card_ids: list[str] | None,
) -> list[str]:
    card = _card(state, str(edit.get("card_id", "")))
    if _is_digital_anchor(state, card) or card["mix_form"] not in _DELETEABLE_MIX_FORMS:
        raise ValueError("只能拆分 B-roll 全屏或信息卡视觉片段；数字人口播主轴不可拆")
    playhead = _finite_number(edit.get("playhead"), "播放头")
    start, end = _narrative_window(card)
    if not start < playhead < end:
        raise ValueError("播放头必须严格位于当前画面片段内部，不能位于端点或范围外")
    ids = list(generated_card_ids or (_new_card_id(), _new_card_id()))
    if len(ids) != 2 or len(set(ids)) != 2:
        raise ValueError("拆分卡片 ID 无效")
    existing_ids = {item["card_id"] for item in state["cards"]}
    if any(card_id in existing_ids for card_id in ids):
        raise ValueError("拆分卡片 ID 已存在")

    first = copy.deepcopy(card)
    second = copy.deepcopy(card)
    source_start, source_end = _source_window(connection, state, card)
    if source_start == source_end == 0:
        source_split = 0.0
    else:
        ratio = (playhead - start) / (end - start)
        source_split = source_start + (source_end - source_start) * ratio
    first.update(
        card_id=ids[0],
        shot_revision=1,
        narrative_start=start,
        narrative_end=playhead,
        enter_tc=source_start,
        exit_tc=source_split,
        created_at=now,
        updated_at=now,
    )
    second.update(
        card_id=ids[1],
        shot_revision=1,
        narrative_start=playhead,
        narrative_end=end,
        enter_tc=source_split,
        exit_tc=source_end,
        created_at=now,
        updated_at=now,
    )
    position = state["cards"].index(card)
    state["cards"][position : position + 1] = [first, second]
    _renumber(state["cards"])
    return ids


def _planned_state(
    db,
    task_id: str,
    before: dict,
    edit: dict,
    *,
    generated_card_ids: list[str] | None = None,
) -> tuple[dict, list[str]]:
    normalized = _normalized_edit(edit)
    _required_ad_ratio(db.connection, task_id)
    # A legacy conflict must not make its own replacement impossible. Validate
    # every other invariant now, and the changed card direction after replacement.
    # Unchanged conflicts may be repaired one at a time; lock/render still check all.
    replacing = normalized["action"] == "replace"
    _validate_state(db.connection, before, task_id, check_uniqueness=False,
                    direction_card_ids=set() if replacing else None)
    after = copy.deepcopy(before)
    now = db.connection.execute("SELECT datetime('now')").fetchone()[0]
    action = normalized["action"]
    if action == "replace":
        affected = _apply_replace(db, task_id, after, normalized)
    elif action == "reorder":
        affected = _apply_reorder(db.connection, after, normalized, now)
    elif action == "trim":
        affected = _apply_trim(db.connection, after, normalized, now)
    elif action == "delete":
        affected = _apply_delete(db.connection, after, normalized, now)
    elif action == "split":
        affected = _apply_split(db.connection, after, normalized, now, generated_card_ids)
    else:
        affected = _apply_add_after(db, task_id, after, normalized, now, generated_card_ids)
    before_by_id = {card["card_id"]: card for card in before["cards"]}
    for card in after["cards"]:
        prior = before_by_id.get(card["card_id"])
        if prior is not None and any(
            card[column] != prior[column]
            for column in _CARD_COLUMNS
            if column not in {"created_at", "updated_at", "shot_revision"}
        ):
            card["shot_revision"] = prior["shot_revision"] + 1
    _validate_state(db.connection, after, task_id,
                    direction_card_ids=set(affected) if replacing else None)
    semantic_before = copy.deepcopy(before)
    semantic_after = copy.deepcopy(after)
    for state in (semantic_before, semantic_after):
        for card in state["cards"]:
            card.pop("updated_at", None)
    if _canonical_json(semantic_after) == _canonical_json(semantic_before):
        raise ValueError("粗剪操作没有产生变化")
    return after, affected


def _lock_state(connection, task_id: str) -> dict | None:
    row = connection.execute(
        "SELECT * FROM picture_lock WHERE task_id=?", (task_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _encode_token(payload: dict) -> str:
    raw = _canonical_json(payload).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(_TOKEN_SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _decode_token(token: str) -> dict:
    try:
        body, signature = token.rsplit(".", 1)
        expected = hmac.new(
            _TOKEN_SECRET, body.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + padding).decode("utf-8"))
    except (AttributeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("impact_token 不匹配或已过期") from error
    if not isinstance(payload, dict):
        raise ValueError("impact_token 不匹配或已过期")
    return payload


def _token_payload(
    task_id: str,
    edit: dict,
    before: dict,
    lock: dict,
    issued_at: int,
    generated_card_ids: list[str] | None,
) -> dict:
    return {
        "task_id": task_id,
        "edit_digest": _digest(_normalized_edit(edit)),
        "before_digest": _digest(before),
        "lock_digest": _digest(lock),
        "issued_at": issued_at,
        "generated_card_ids": generated_card_ids,
    }


def _verified_token(
    token: str,
    task_id: str,
    edit: dict,
    before: dict,
    lock: dict,
) -> dict:
    payload = _decode_token(token)
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int):
        raise ValueError("impact_token 不匹配或已过期")
    age = time.time() - issued_at
    if age < -5 or age > _TOKEN_TTL_SECONDS:
        raise ValueError("impact_token 已过期，请重新预览影响")
    expected = _token_payload(
        task_id,
        edit,
        before,
        lock,
        issued_at,
        payload.get("generated_card_ids"),
    )
    if not hmac.compare_digest(_digest(payload), _digest(expected)):
        raise ValueError("impact_token 不匹配或已过期")
    return payload


def _history_picture_state(state: dict) -> dict:
    # Feedback is audit attached to deleted cards, not the editable picture identity.
    return {key: value for key, value in state.items() if key != "removed_feedback"}


def _archive_removed_feedback(connection, task_id: str, source: dict, target: dict) -> dict:
    history = _history_picture_state(source)
    removed_ids = ({card["card_id"] for card in source["cards"]}
                   - {card["card_id"] for card in target["cards"]})
    feedback = []
    for card_id in sorted(removed_ids):
        feedback.extend(dict(row) for row in connection.execute(
            "SELECT * FROM storyboard_feedback WHERE task_id=? AND card_id=? "
            "ORDER BY created_at,feedback_id", (task_id, card_id)))
    if feedback:
        history["removed_feedback"] = feedback
    return history


def _restore_state(connection, task_id: str, state: dict, *,
                   direction_card_ids: set[str] | None = None) -> None:
    cards = state.get("cards")
    task_assets = state.get("task_assets")
    if not isinstance(cards, list) or not isinstance(task_assets, list):
        raise ValueError("粗剪历史快照损坏")
    if any(card.get("task_id") != task_id for card in cards):
        raise ValueError("粗剪历史卡片不属于当前任务")
    if any(item.get("task_id") != task_id for item in task_assets):
        raise ValueError("粗剪历史素材不属于当前任务")
    _validate_state(connection, state, task_id, direction_card_ids=direction_card_ids)

    current_ids = {row["card_id"] for row in connection.execute(
        "SELECT card_id FROM scene_cards WHERE task_id=?", (task_id,))}
    desired_ids = {card["card_id"] for card in cards}
    feedback_rows = state.get("removed_feedback", [])
    if (not isinstance(feedback_rows, list) or any(
            not isinstance(item, dict) or item.get("task_id") != task_id
            or item.get("card_id") not in desired_ids for item in feedback_rows)):
        raise ValueError("粗剪历史反馈不属于当前任务卡片")
    # Updating retained cards in place keeps their feedback and manual-choice history.
    for card_id in current_ids - desired_ids:
        connection.execute("DELETE FROM scene_cards WHERE task_id=? AND card_id=?",
                           (task_id, card_id))
    connection.execute("DELETE FROM task_assets WHERE task_id=?", (task_id,))
    placeholders = ",".join("?" for _ in _TASK_ASSET_COLUMNS)
    for item in task_assets:
        connection.execute(
            f"INSERT INTO task_assets({','.join(_TASK_ASSET_COLUMNS)}) VALUES({placeholders})",
            tuple(item[column] for column in _TASK_ASSET_COLUMNS),
        )
    placeholders = ",".join("?" for _ in _CARD_COLUMNS)
    for card in cards:
        if "script_segment_id" not in card:
            legacy_id = f"{card['card_id']}:script"
            card["script_segment_id"] = (
                legacy_id if connection.execute(
                    "SELECT 1 FROM script_segments WHERE segment_id=? AND task_id=?",
                    (legacy_id, task_id),
                ).fetchone() else None
            )
        card.setdefault("shot_revision", 1)
        if card["card_id"] in current_ids:
            columns = tuple(key for key in _CARD_COLUMNS if key not in {"card_id", "task_id"})
            connection.execute(
                f"UPDATE scene_cards SET {','.join(key + '=?' for key in columns)} "
                "WHERE task_id=? AND card_id=?",
                (*[card[key] for key in columns], task_id, card["card_id"]),
            )
        else:
            connection.execute(
                f"INSERT INTO scene_cards({','.join(_CARD_COLUMNS)}) VALUES({placeholders})",
                tuple(card[column] for column in _CARD_COLUMNS),
            )
    for feedback in feedback_rows:
        # Only a recreated card needs its archived feedback. Never replace live feedback.
        if feedback["card_id"] in current_ids:
            continue
        columns = ("feedback_id", "task_id", "card_id", "decision", "instruction",
                   "original_asset_id", "selected_asset_id", "candidates_json",
                   "created_at", "applied_at")
        connection.execute(
            f"INSERT INTO storyboard_feedback({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            tuple(feedback[key] for key in columns),
        )


def _advance_accepted_storyboard(db, task_id: str) -> None:
    """Keep accepted ad storyboard lineage current after a bounded rough-cut edit."""
    stage = db.connection.execute(
        "SELECT status,detail_json FROM task_stage_states "
        "WHERE task_id=? AND stage_key='ai_storyboard'", (task_id,),
    ).fetchone()
    if stage is None or stage["status"] != "completed":
        return
    try:
        record = json.loads(stage["detail_json"] or "{}")
    except (TypeError, ValueError):
        return
    details = record.get("details") if isinstance(record, dict) else None
    if not isinstance(details, dict) or not details.get("storyboard_accepted_digest"):
        return
    digest = storyboard_digest(db, task_id)
    if digest is None:
        return
    details.setdefault("storyboard_original_accepted_digest", details["storyboard_accepted_digest"])
    details["storyboard_accepted_digest"] = digest
    record["input_digest"] = digest
    db.connection.execute(
        "UPDATE task_stage_states SET detail_json=?,updated_at=datetime('now') "
        "WHERE task_id=? AND stage_key='ai_storyboard'",
        (_canonical_json(record), task_id),
    )


def _invalidate_locked_followups(connection, task_id: str) -> list[str]:
    if _lock_state(connection, task_id) is None:
        return []
    connection.execute(
        "UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?",
        (task_id,),
    )
    invalidate_from_connection(
        connection,
        task_id,
        "rough_cut",
        reason="rough-cut visual edit changed locked picture",
    )
    index = WORKFLOW_STAGES.index("rough_cut")
    return list(WORKFLOW_STAGES[index + 1 :])


def _require_viewed_picture(db, task_id: str, expected_digest: str | None) -> None:
    # Legacy in-process callers retain their existing contract. Public page
    # mutations require a non-empty digest before entering this transaction.
    if expected_digest is not None and picture_view_digest(db, task_id) != expected_digest:
        raise RuntimeError("STORYBOARD_CHANGED: 分镜在您查看后已变化，请刷新核对")


def _existing_locked_followups(connection, task_id: str) -> list[str]:
    """Describe existing work only; mutation still invalidates all dependencies."""
    # A lock implies that picture order/confirmation already exists. Stage state
    # alone does not prove a packaging plan, rendered video or review exists.
    existing = {"picture_order", "picture_lock"}
    outputs = {
        "packaging_confirm": "SELECT 1 FROM packaging_plans WHERE task_id=? LIMIT 1",
        "fine_render": (
            "SELECT 1 FROM render_snapshots WHERE task_id=? "
            "AND stage IN ('mix','ad') AND output_path IS NOT NULL "
            "AND output_path<>'' LIMIT 1"
        ),
        "auto_qa": "SELECT 1 FROM qa_reports WHERE task_id=? LIMIT 1",
        "full_review": (
            "SELECT 1 FROM review_approvals WHERE task_id=? "
            "UNION ALL SELECT 1 FROM review_watch_sessions "
            "WHERE task_id=? AND covered_seconds>0 LIMIT 1"
        ),
        "publish_package": "SELECT 1 FROM publish_records WHERE task_id=? LIMIT 1",
    }
    for stage, query in outputs.items():
        params = (task_id, task_id) if stage == "full_review" else (task_id,)
        if connection.execute(query, params).fetchone() is not None:
            existing.add(stage)
    return [stage for stage in WORKFLOW_STAGES if stage in existing]


def preview_edit_impact(db, task_id: str, edit: dict, *, expected_digest: str | None = None) -> dict:
    """Validate one bounded edit and return a short-lived confirmation token."""
    _require_viewed_picture(db, task_id, expected_digest)
    before = _snapshot_state(db.connection, task_id)
    lock = _lock_state(db.connection, task_id)
    generated_ids = ([_new_card_id(), _new_card_id()] if edit.get("action") == "split"
                     else [_new_card_id()] if edit.get("action") == "add_after" else None)
    after, affected = _planned_state(
        db,
        task_id,
        before,
        edit,
        generated_card_ids=generated_ids,
    )
    issued_at = int(time.time())
    payload = None
    token = None
    if lock is not None:
        payload = _token_payload(
            task_id, edit, before, lock, issued_at, generated_ids
        )
        token = _encode_token(payload)
    invalidated = (
        _existing_locked_followups(db.connection, task_id)
        if lock is not None
        else []
    )
    return {
        "task_id": task_id,
        "action": edit["action"],
        "locked": lock is not None,
        "affected_card_ids": affected,
        "changed_since_lock": lock is not None,
        "affected_packaging": affected_packaging(db.connection, task_id, before, after),
        "invalidated_stages": invalidated,
        "impact_digest": _digest({"before": before, "after": after, "edit": edit}),
        "impact_token": token,
        "expires_at": issued_at + _TOKEN_TTL_SECONDS,
    }


def apply_edit(
    db,
    task_id: str,
    edit: dict,
    *,
    impact_token: str | None = None,
    expected_digest: str | None = None,
) -> dict:
    """Apply one visual-only edit and its audit/invalidation in one transaction."""
    normalized = _normalized_edit(edit)
    with db.transaction(immediate=True) as connection:
        _require_viewed_picture(db, task_id, expected_digest)
        before = _snapshot_state(connection, task_id)
        lock = _lock_state(connection, task_id)
        generated_ids = None
        if lock is not None:
            if impact_token is None:
                raise RuntimeError(
                    "IMPACT_PREVIEW_REQUIRED: 画面锁定后必须先预览编辑影响"
                )
            payload = _verified_token(
                impact_token, task_id, normalized, before, lock
            )
            generated_ids = payload.get("generated_card_ids")
        after, affected = _planned_state(
            db,
            task_id,
            before,
            normalized,
            generated_card_ids=generated_ids,
        )
        history_before = _archive_removed_feedback(connection, task_id, before, after)
        _restore_state(
            connection, task_id, after,
            direction_card_ids=set(affected) if normalized["action"] == "replace" else None,
        )
        _advance_accepted_storyboard(db, task_id)
        invalidated = _invalidate_locked_followups(connection, task_id)
        connection.execute(
            """
            UPDATE rough_cut_edits
            SET status='pending',updated_at=datetime('now')
            WHERE task_id=? AND status='reverted'
            """,
            (task_id,),
        )
        edit_id = f"edit-{uuid.uuid4().hex}"
        impact_digest = _digest(
            {"before": before, "after": after, "edit": normalized}
        )
        connection.execute(
            """
            INSERT INTO rough_cut_edits(
              edit_id,task_id,before_json,after_json,impact_digest,status
            ) VALUES(?,?,?,?,?,'applied')
            """,
            (
                edit_id,
                task_id,
                _canonical_json(history_before),
                _canonical_json(after),
                impact_digest,
            ),
        )
        connection.execute(
            "UPDATE tasks SET updated_at=datetime('now') WHERE task_id=?",
            (task_id,),
        )
    return {
        "edit_id": edit_id,
        "task_id": task_id,
        "action": normalized["action"],
        "status": "applied",
        "affected_card_ids": affected,
        "invalidated_stages": invalidated,
        "cards": _snapshot_state(db.connection, task_id)["cards"],
    }


def _history_state(value: str) -> dict:
    try:
        state = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("粗剪历史快照损坏") from error
    if not isinstance(state, dict):
        raise ValueError("粗剪历史快照损坏")
    return state


def undo_last_edit(db, task_id: str, *, expected_digest: str | None = None) -> dict:
    """Restore the newest applied edit whose after-state is still current."""
    with db.transaction(immediate=True) as connection:
        _require_viewed_picture(db, task_id, expected_digest)
        current = _snapshot_state(connection, task_id)
        rows = connection.execute(
            """
            SELECT rowid,* FROM rough_cut_edits
            WHERE task_id=? AND status='applied' ORDER BY rowid DESC
            """,
            (task_id,),
        ).fetchall()
        chosen = None
        before = None
        for row in rows:
            candidate_after = _history_state(row["after_json"])
            if _canonical_json(_history_picture_state(candidate_after)) == _canonical_json(current):
                chosen = row
                before = _history_state(row["before_json"])
                break
        if chosen is None or before is None:
            raise RuntimeError("没有可撤销的粗剪操作，或当前画面已在历史外变化")
        history_after = _archive_removed_feedback(connection, task_id, current, before)
        _restore_state(connection, task_id, before)
        _advance_accepted_storyboard(db, task_id)
        invalidated = _invalidate_locked_followups(connection, task_id)
        connection.execute(
            """
            UPDATE rough_cut_edits
            SET status='reverted',after_json=?,updated_at=datetime('now') WHERE edit_id=?
            """,
            (_canonical_json(history_after), chosen["edit_id"]),
        )
        connection.execute(
            "UPDATE tasks SET updated_at=datetime('now') WHERE task_id=?",
            (task_id,),
        )
    return {
        "edit_id": chosen["edit_id"],
        "task_id": task_id,
        "status": "reverted",
        "invalidated_stages": invalidated,
        "cards": _snapshot_state(db.connection, task_id)["cards"],
    }


def redo_last_edit(db, task_id: str, *, expected_digest: str | None = None) -> dict:
    """Reapply the newest reverted branch whose before-state is current."""
    with db.transaction(immediate=True) as connection:
        _require_viewed_picture(db, task_id, expected_digest)
        current = _snapshot_state(connection, task_id)
        rows = connection.execute(
            """
            SELECT rowid,* FROM rough_cut_edits
            WHERE task_id=? AND status='reverted' ORDER BY rowid DESC
            """,
            (task_id,),
        ).fetchall()
        chosen = None
        after = None
        for row in rows:
            candidate_before = _history_state(row["before_json"])
            if _canonical_json(_history_picture_state(candidate_before)) == _canonical_json(current):
                chosen = row
                after = _history_state(row["after_json"])
                break
        if chosen is None or after is None:
            raise RuntimeError("没有可重做的粗剪操作，或当前画面已在历史外变化")
        history_before = _archive_removed_feedback(connection, task_id, current, after)
        _restore_state(connection, task_id, after)
        _advance_accepted_storyboard(db, task_id)
        invalidated = _invalidate_locked_followups(connection, task_id)
        connection.execute(
            """
            UPDATE rough_cut_edits
            SET status='applied',before_json=?,updated_at=datetime('now') WHERE edit_id=?
            """,
            (_canonical_json(history_before), chosen["edit_id"]),
        )
        connection.execute(
            "UPDATE tasks SET updated_at=datetime('now') WHERE task_id=?",
            (task_id,),
        )
    return {
        "edit_id": chosen["edit_id"],
        "task_id": task_id,
        "status": "applied",
        "invalidated_stages": invalidated,
        "cards": _snapshot_state(db.connection, task_id)["cards"],
    }
