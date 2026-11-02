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

from .scene_cards import VALID_MIX_FORMS
from .task_sources import scoped_asset_location
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
)
_TASK_ASSET_COLUMNS = ("task_id", "asset_id", "role", "created_at")
_DELETEABLE_MIX_FORMS = {"broll全屏", "信息卡"}
_ACTIONS = {"replace", "reorder", "trim", "delete", "split"}
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
    row = connection.execute(
        """
        SELECT segment_id,asset_id,start_sec,end_sec
        FROM segments WHERE segment_id=?
        """,
        (segment_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"素材片段不存在：{segment_id}")
    result = dict(row)
    if result["asset_id"] != asset_id:
        raise ValueError("素材片段不属于替代素材")
    start = _finite_number(result["start_sec"], "素材片段开始时间")
    end = _finite_number(result["end_sec"], "素材片段结束时间")
    if start < 0 or end <= start:
        raise ValueError("素材片段时间顺序或边界无效")
    result["start_sec"] = start
    result["end_sec"] = end
    return result


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


def _source_window(connection, state: dict, card: dict) -> tuple[float, float]:
    narrative_start, narrative_end = _narrative_window(card)
    narrative_duration = narrative_end - narrative_start
    asset_id = card.get("asset_id")
    if not asset_id:
        if _is_digital_anchor(state, card):
            return 0.0, 0.0
        raise ValueError("当前画面缺少素材绑定")
    media_kind = _asset_media_kind(connection, asset_id)
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
    if segment is not None and (
        source_start < segment["start_sec"] - 1e-9
        or source_end > segment["end_sec"] + 1e-9
    ):
        raise ValueError("源时间窗越过素材片段边界")
    return source_start, source_end


def _validate_state(connection, state: dict) -> None:
    ids = [card.get("card_id") for card in state["cards"]]
    if any(not isinstance(card_id, str) or not card_id for card_id in ids):
        raise ValueError("场景卡片 ID 必须是非空字符串")
    if len(ids) != len(set(ids)):
        raise ValueError("场景卡片 ID 必须唯一")
    for card in state["cards"]:
        _narrative_window(card)
        if card.get("mix_form") not in VALID_MIX_FORMS:
            raise ValueError(f"混剪形态必须是 {VALID_MIX_FORMS} 之一")
        if _is_digital_anchor(state, card):
            if card.get("asset_id"):
                kind = _asset_media_kind(connection, card["asset_id"])
                if kind != "video" or _task_asset_role(state, card["asset_id"]) != "digital_human":
                    raise ValueError("数字人口播锚点必须绑定 role=digital_human 的视频")
            _source_window(connection, state, card)
            continue
        asset_id = card.get("asset_id")
        if not asset_id:
            raise ValueError("非数字人画面必须绑定素材")
        kind = _asset_media_kind(connection, asset_id)
        _validate_visual_binding(state, asset_id, kind, card["mix_form"])
        _source_window(connection, state, card)


def _allowed_asset(db, task_id: str, asset_id: str) -> dict:
    asset = db.connection.execute(
        "SELECT asset_id,media_kind FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if asset is None:
        raise KeyError(f"素材未入册：{asset_id}")
    location = scoped_asset_location(db, task_id, asset_id)
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
    if _is_digital_anchor(state, card):
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
    mix_form = edit.get("mix_form", card["mix_form"])
    if mix_form not in VALID_MIX_FORMS:
        raise ValueError(f"混剪形态必须是 {VALID_MIX_FORMS} 之一")
    role = _validate_visual_binding(
        state, asset_id, asset["media_kind"], mix_form, allow_unregistered=True
    )
    narrative_start, narrative_end = _narrative_window(card)
    duration = narrative_end - narrative_start
    if asset["media_kind"] == "image":
        if segment is not None:
            raise ValueError("图片素材不能绑定视频片段")
        source_start = source_end = 0.0
    else:
        source_start = segment["start_sec"] if segment is not None else 0.0
        source_end = source_start + duration
        if segment is not None and source_end > segment["end_sec"] + 1e-9:
            raise ValueError("替换片段时长不足以覆盖当前叙事槽")

    card["asset_id"] = asset_id
    card["segment_id"] = segment_id
    card["mix_form"] = mix_form
    card["enter_tc"] = source_start
    card["exit_tc"] = source_end
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
    fixed_slots = state["cards"]
    by_id = {card["card_id"]: card for card in fixed_slots}
    for index, slot in enumerate(fixed_slots):
        if _is_digital_anchor(state, slot) and requested[index] != slot["card_id"]:
            raise ValueError("数字人口播锚点位置固定，不能重排")
    requested_visuals = [
        by_id[card_id] for card_id in requested
        if not _is_digital_anchor(state, by_id[card_id])
    ]
    target_slots = [card for card in fixed_slots if not _is_digital_anchor(state, card)]
    payloads = []
    for source in requested_visuals:
        payload = {column: copy.deepcopy(source[column]) for column in _VISUAL_PAYLOAD_COLUMNS}
        source_start, source_end = _source_window(connection, state, source)
        payload["enter_tc"] = source_start
        payload["exit_tc"] = source_end
        payloads.append(payload)
    affected = []
    for slot, payload in zip(target_slots, payloads, strict=True):
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
    if not card["segment_id"] or not card["asset_id"]:
        raise ValueError("当前画面没有可核验的素材片段边界")
    segment = _segment(connection, card["segment_id"], card["asset_id"])
    if enter_tc < float(segment["start_sec"]) or exit_tc > float(segment["end_sec"]):
        raise ValueError("裁剪范围越过素材片段边界")
    if card["narrative_start"] is None or card["narrative_end"] is None:
        raise ValueError("当前画面缺少可核验的场景时长")
    scene_duration = float(card["narrative_end"] - card["narrative_start"])
    if exit_tc - enter_tc < scene_duration:
        raise ValueError("裁剪后的素材时长不足以覆盖当前场景")
    card["enter_tc"] = enter_tc
    card["exit_tc"] = exit_tc
    card["updated_at"] = now
    return [card["card_id"]]


def _apply_delete(state: dict, edit: dict, now: str) -> list[str]:
    card = _card(state, str(edit.get("card_id", "")))
    if _is_digital_anchor(state, card) or card["mix_form"] not in _DELETEABLE_MIX_FORMS:
        raise ValueError("只能删除 B-roll 全屏或信息卡；连续口播主轴不可删除")
    state["cards"] = [item for item in state["cards"] if item["card_id"] != card["card_id"]]
    _renumber(state["cards"])
    for item in state["cards"]:
        item["updated_at"] = now
    return [card["card_id"]]


def _new_card_id() -> str:
    return f"card-{uuid.uuid4().hex}"


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
        narrative_start=start,
        narrative_end=playhead,
        enter_tc=source_start,
        exit_tc=source_split,
        created_at=now,
        updated_at=now,
    )
    second.update(
        card_id=ids[1],
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
    _validate_state(db.connection, before)
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
        affected = _apply_delete(after, normalized, now)
    else:
        affected = _apply_split(db.connection, after, normalized, now, generated_card_ids)
    _validate_state(db.connection, after)
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


def _restore_state(connection, task_id: str, state: dict) -> None:
    cards = state.get("cards")
    task_assets = state.get("task_assets")
    if not isinstance(cards, list) or not isinstance(task_assets, list):
        raise ValueError("粗剪历史快照损坏")
    if any(card.get("task_id") != task_id for card in cards):
        raise ValueError("粗剪历史卡片不属于当前任务")
    if any(item.get("task_id") != task_id for item in task_assets):
        raise ValueError("粗剪历史素材不属于当前任务")
    _validate_state(connection, state)

    connection.execute("DELETE FROM scene_cards WHERE task_id=?", (task_id,))
    connection.execute("DELETE FROM task_assets WHERE task_id=?", (task_id,))
    placeholders = ",".join("?" for _ in _TASK_ASSET_COLUMNS)
    for item in task_assets:
        connection.execute(
            f"INSERT INTO task_assets({','.join(_TASK_ASSET_COLUMNS)}) VALUES({placeholders})",
            tuple(item[column] for column in _TASK_ASSET_COLUMNS),
        )
    placeholders = ",".join("?" for _ in _CARD_COLUMNS)
    for card in cards:
        connection.execute(
            f"INSERT INTO scene_cards({','.join(_CARD_COLUMNS)}) VALUES({placeholders})",
            tuple(card[column] for column in _CARD_COLUMNS),
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


def preview_edit_impact(db, task_id: str, edit: dict) -> dict:
    """Validate one bounded edit and return a short-lived confirmation token."""
    before = _snapshot_state(db.connection, task_id)
    lock = _lock_state(db.connection, task_id)
    generated_ids = [_new_card_id(), _new_card_id()] if edit.get("action") == "split" else None
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
        list(WORKFLOW_STAGES[WORKFLOW_STAGES.index("rough_cut") + 1 :])
        if lock is not None
        else []
    )
    return {
        "task_id": task_id,
        "action": edit["action"],
        "locked": lock is not None,
        "affected_card_ids": affected,
        "changed_since_lock": lock is not None,
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
) -> dict:
    """Apply one visual-only edit and its audit/invalidation in one transaction."""
    normalized = _normalized_edit(edit)
    with db.transaction(immediate=True) as connection:
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
        _restore_state(connection, task_id, after)
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
                _canonical_json(before),
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


def undo_last_edit(db, task_id: str) -> dict:
    """Restore the newest applied edit whose after-state is still current."""
    with db.transaction(immediate=True) as connection:
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
            if _canonical_json(candidate_after) == _canonical_json(current):
                chosen = row
                before = _history_state(row["before_json"])
                break
        if chosen is None or before is None:
            raise RuntimeError("没有可撤销的粗剪操作，或当前画面已在历史外变化")
        _restore_state(connection, task_id, before)
        invalidated = _invalidate_locked_followups(connection, task_id)
        connection.execute(
            """
            UPDATE rough_cut_edits
            SET status='reverted',updated_at=datetime('now') WHERE edit_id=?
            """,
            (chosen["edit_id"],),
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


def redo_last_edit(db, task_id: str) -> dict:
    """Reapply the newest reverted branch whose before-state is current."""
    with db.transaction(immediate=True) as connection:
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
            if _canonical_json(candidate_before) == _canonical_json(current):
                chosen = row
                after = _history_state(row["after_json"])
                break
        if chosen is None or after is None:
            raise RuntimeError("没有可重做的粗剪操作，或当前画面已在历史外变化")
        _restore_state(connection, task_id, after)
        invalidated = _invalidate_locked_followups(connection, task_id)
        connection.execute(
            """
            UPDATE rough_cut_edits
            SET status='applied',updated_at=datetime('now') WHERE edit_id=?
            """,
            (chosen["edit_id"],),
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
