"""Task-local packaging elements with stable IDs and bounded timeline placement."""

from __future__ import annotations

import json
import math
import uuid


ROLES = frozenset({"title_card", "info_card", "brand", "sfx", "sticker", "fancy_text"})
TEXT_ROLES = frozenset({"title_card", "info_card", "fancy_text"})
DEFAULT_POSITION = {
    "title_card": (0.08, 0.10, 0.84),
    "info_card": (0.08, 0.32, 0.84),
    "brand": (0.68, 0.02, 0.28),
    "sfx": (None, None, None),
    "sticker": (0.72, 0.12, 0.20),
    "fancy_text": (0.08, 0.18, 0.25),
}


def list_instances(connection, plan_id: str) -> list[dict]:
    return [dict(row) for row in connection.execute(
        "SELECT * FROM packaging_plan_instances WHERE plan_id=? "
        "ORDER BY start_seconds,created_at,instance_id", (plan_id,),
    )]


def _scenes(connection, task_id: str) -> list[dict]:
    return [dict(row) for row in connection.execute(
        "SELECT card_id,narrative_start,narrative_end FROM scene_cards WHERE task_id=? "
        "ORDER BY idx,card_id", (task_id,),
    )]


def _scene_point(scenes: list[dict], seconds: float) -> tuple[float, float]:
    index = next((i for i, scene in enumerate(scenes)
                  if seconds < scene["narrative_end"] or i == len(scenes) - 1), 0)
    scene = scenes[index]
    span = max(float(scene["narrative_end"]) - float(scene["narrative_start"]), .001)
    return index / max(len(scenes) - 1, 1), max(0.0, min(1.0,
        (seconds - float(scene["narrative_start"])) / span))


def template_instance_rules(connection, plan: dict) -> list[dict]:
    """Store relative placement and source-media windows, never task text or absolute task time."""
    scenes = _scenes(connection, plan["task_id"])
    if plan["instances"] and not scenes:
        raise ValueError("PACKAGING_TEMPLATE_SCENES_MISSING: 本任务没有可复用的镜头锚点")
    ids = {instance["instance_id"]: f"element-{index}"
           for index, instance in enumerate(plan["instances"])}
    result = []
    for instance in plan["instances"]:
        full = (instance["role"] == "brand" and instance["start_seconds"] == 0
                and instance["end_seconds"] >= scenes[-1]["narrative_end"])
        start = _scene_point(scenes, instance["start_seconds"])
        end = _scene_point(scenes, instance["end_seconds"])
        result.append({
            "key": ids[instance["instance_id"]], "role": instance["role"],
            "asset_ref": instance["asset_ref"],
            "placement": "full_timeline" if full else "scene_relative",
            "start_scene_ratio": start[0], "start_offset": start[1],
            "end_scene_ratio": end[0], "end_offset": end[1],
            "follow_key": ids.get(instance["anchor_ref"])
            if instance["anchor_kind"] == "instance" else None,
            "follow_event": bool(instance.get("follow_event", 0)),
            **({"source_length": instance["end_seconds"] - instance["start_seconds"]}
               if instance["role"] == "sfx" else {}),
            "x": instance["x"], "y": instance["y"],
            "scale": instance["scale"], "gain": instance["gain"],
            **({"source_offset": instance["source_start_seconds"]}
               if instance["role"] == "sfx" and instance.get("source_start_seconds") is not None
               else {}),
            **({"fade_in": instance["fade_in_seconds"]}
               if instance["role"] == "sfx" and instance.get("fade_in_seconds") is not None
               else {}),
            **({"fade_out": instance["fade_out_seconds"]}
               if instance["role"] == "sfx" and instance.get("fade_out_seconds") is not None
               else {}),
        })
    return result


def resolve_template_rules(connection, task_id: str, rules: list[dict]) -> list[dict]:
    scenes = _scenes(connection, task_id)
    if rules and not scenes:
        raise ValueError("PACKAGING_TEMPLATE_SCENES_MISSING: 目标任务尚无镜头时间轴")
    ids = {rule["key"]: f"pi-{uuid.uuid4().hex[:16]}" for rule in rules}

    def point(ratio: float, offset: float) -> tuple[float, str]:
        scene = scenes[round(float(ratio) * (len(scenes) - 1))]
        return (float(scene["narrative_start"]) + float(offset) *
                (float(scene["narrative_end"]) - float(scene["narrative_start"]))), scene["card_id"]

    resolved = []
    for rule in rules:
        if rule["placement"] == "full_timeline":
            start, end = 0.0, float(scenes[-1]["narrative_end"])
            card_id = None
        else:
            start, card_id = point(rule["start_scene_ratio"], rule["start_offset"])
            end, _ = point(rule["end_scene_ratio"], rule["end_offset"])
            if end <= start:
                scene = next(scene for scene in scenes if scene["card_id"] == card_id)
                end = min(float(scenes[-1]["narrative_end"]),
                          max(start + .1, float(scene["narrative_end"])))
            scene = next(scene for scene in scenes if scene["card_id"] == card_id)
            if end > float(scene["narrative_end"]):
                card_id = None
        follow = ids.get(rule.get("follow_key"))
        if rule["role"] == "sfx":
            # Sound uses a source-media window, not the new video's time scale.
            length = rule.get("source_length")
            if length is None:
                # Historical templates have only relative placement. Cap them
                # using the real source when online; disabled offline items
                # remain drafts and are rechecked before enabling/rendering.
                from . import packaging_plan
                path = packaging_plan.resolve_manifest_asset_path("sfx", rule["asset_ref"])
                measured = (packaging_plan.audio_duration_seconds(path)
                            if path is not None and path.is_file() else None)
                length = (min(end - start, measured - (rule.get("source_offset") or 0))
                          if measured is not None else end - start)
            if (not isinstance(length, (int, float)) or isinstance(length, bool)
                    or not math.isfinite(length) or length <= 0):
                raise ValueError("PACKAGING_TEMPLATE_INVALID: 音效素材取用长度无效")
            end = min(float(scenes[-1]["narrative_end"]), start + length)
            if card_id is not None:
                scene = next(scene for scene in scenes if scene["card_id"] == card_id)
                if end > float(scene["narrative_end"]):
                    card_id = None
        resolved.append({
            "instance_id": ids[rule["key"]], "role": rule["role"],
            "asset_ref": rule["asset_ref"],
            "content_text": None,
            "anchor_kind": "instance" if follow else "scene" if card_id else "timeline",
            "anchor_ref": follow or card_id,
            "start_seconds": start, "end_seconds": end,
            "x": rule.get("x"), "y": rule.get("y"),
            "scale": rule.get("scale"), "gain": rule.get("gain"),
            "source_start_seconds": rule.get("source_offset"),
            "fade_in_seconds": rule.get("fade_in"),
            "fade_out_seconds": rule.get("fade_out"),
            "timing_rule": "explicit", "origin": "template", "revision": 1,
            "follow_event": int(bool(rule.get("follow_event", False))),
        })
    by_id = {item["instance_id"]: item for item in resolved}
    for item in resolved:
        if item["role"] == "sfx" and item["follow_event"] and item["anchor_kind"] == "instance":
            parent = by_id[item["anchor_ref"]]
            length = item["end_seconds"] - item["start_seconds"]
            item["start_seconds"] = parent["start_seconds"]
            item["end_seconds"] = min(parent["end_seconds"], parent["start_seconds"] + length)
    return resolved


def _duration(connection, task_id: str) -> float:
    row = connection.execute(
        "SELECT MAX(narrative_end) AS duration FROM scene_cards WHERE task_id=?",
        (task_id,),
    ).fetchone()
    return float(row["duration"] or 0.0)


def _validate_asset(role: str, asset_ref: str, route: str, db=None) -> None:
    from . import packaging_plan

    category = "title_card" if role in {"title_card", "info_card", "brand"} else role
    item = packaging_plan.find_item(category, asset_ref, db)
    if item is None:
        raise ValueError(f"PACKAGING_MANIFEST_UNKNOWN: 素材未登记：{role}/{asset_ref}")
    if role == "brand" and item.get("kind") != "watermark":
        raise ValueError("PACKAGING_ROLE_MISMATCH: 品牌元素必须使用水印素材")
    if role in {"title_card", "info_card"} and item.get("kind") == "watermark":
        raise ValueError("PACKAGING_ROLE_MISMATCH: 文字卡不能使用品牌水印")
    if item.get("license") != "commercial-approved":
        raise ValueError("PACKAGING_LICENSE_BLOCKED: 素材未获商业使用许可")
    routes = item.get("default_for") or []
    if routes and route not in routes:
        raise ValueError("PACKAGING_NOT_APPLICABLE: 素材不适用于当前路线")
    path = packaging_plan.resolve_manifest_asset_path(category, asset_ref, db)
    if path is not None and not path.is_file():
        raise ValueError("PACKAGING_FILE_MISSING: 包装素材文件缺失")
    if path is not None and path.is_file():
        try:
            if path.stat().st_size == 0:
                raise ValueError("PACKAGING_FILE_UNREADABLE: 包装素材为空文件")
        except OSError as exc:
            raise ValueError("PACKAGING_FILE_UNREADABLE: 包装素材不可读取") from exc
    if role == "sfx" and path is not None and not packaging_plan._audio_file_readable(path):
        raise ValueError("PACKAGING_FILE_UNREADABLE: 音效不可解码")


def _validate_instance(connection, plan: dict, value: dict, *, require_text: bool = True) -> dict:
    role = value.get("role")
    if role not in ROLES:
        raise ValueError("PACKAGING_ROLE_INVALID: 元素类别无效")
    asset_ref = value.get("asset_ref")
    if not isinstance(asset_ref, str) or not asset_ref.strip():
        raise ValueError("PACKAGING_ASSET_REF_INVALID: 必须选择包装素材")
    _validate_asset(role, asset_ref, plan["route"], connection)
    start, end = value.get("start_seconds"), value.get("end_seconds")
    duration = _duration(connection, plan["task_id"])
    if not all(isinstance(number, (int, float)) and not isinstance(number, bool)
               and math.isfinite(number) for number in (start, end)):
        raise ValueError("PACKAGING_TIME_INVALID: 必须指定有限的开始和结束秒数")
    if not 0 <= start < end <= duration:
        raise ValueError("PACKAGING_TIME_OUT_OF_RANGE: 元素时间必须位于当前成片内")
    text = value.get("content_text")
    if role in TEXT_ROLES:
        if not require_text and (text is None or (isinstance(text, str) and not text.strip())):
            text = None
        else:
            if not isinstance(text, str) or not text.strip() or len(text) > 300:
                raise ValueError("PACKAGING_TEXT_INVALID: 文字内容不能为空或超过300字")
            text = text.strip()
    else:
        text = None
    anchor_kind = value.get("anchor_kind", "timeline")
    anchor_ref = value.get("anchor_ref")
    if anchor_kind == "scene":
        found = connection.execute(
            "SELECT narrative_start,narrative_end FROM scene_cards WHERE task_id=? AND card_id=?",
            (plan["task_id"], anchor_ref),
        ).fetchone()
        if found is None:
            raise ValueError("PACKAGING_ANCHOR_INVALID: 镜头锚点不属于当前任务")
        if start < found["narrative_start"] or end > found["narrative_end"]:
            raise ValueError("PACKAGING_ANCHOR_RANGE: 元素时间超出绑定镜头")
    elif anchor_kind == "instance":
        found = connection.execute(
            "SELECT start_seconds,end_seconds FROM packaging_plan_instances "
            "WHERE plan_id=? AND instance_id=?",
            (plan["plan_id"], anchor_ref),
        ).fetchone()
        if found is None or anchor_ref == value.get("instance_id"):
            raise ValueError("PACKAGING_ANCHOR_INVALID: 元素锚点无效")
        if start < found["start_seconds"] or end > found["end_seconds"]:
            raise ValueError("PACKAGING_ANCHOR_RANGE: 元素时间超出所跟随的包装元素")
    elif anchor_kind != "timeline" or anchor_ref is not None:
        raise ValueError("PACKAGING_ANCHOR_INVALID: 时间线锚点不能携带对象ID")
    x, y, scale = (value.get(key) for key in ("x", "y", "scale"))
    if role != "sfx":
        if not all(isinstance(number, (int, float)) and not isinstance(number, bool)
                   and math.isfinite(number) for number in (x, y, scale)):
            raise ValueError("PACKAGING_POSITION_INVALID: 位置和大小必须是有限数字")
        if not (0.02 <= x <= 0.96 and 0.02 <= y <= 0.96
                and 0 < scale <= 0.94 and x + scale <= 0.98):
            raise ValueError("PACKAGING_SAFE_AREA: 元素超出画面安全区")
    else:
        x = y = scale = None
    gain = value.get("gain")
    audio_keys = ("source_start_seconds", "fade_in_seconds", "fade_out_seconds")
    audio_edits = {key: value.get(key) for key in audio_keys}
    if role == "sfx":
        if (not isinstance(gain, (int, float)) or isinstance(gain, bool)
                or not math.isfinite(gain) or not 0 <= gain <= 1):
            raise ValueError("PACKAGING_GAIN_INVALID: 音效音量须在0到1之间")
        from . import packaging_plan

        path = packaging_plan.resolve_manifest_asset_path("sfx", asset_ref)
        source_duration = (packaging_plan.audio_duration_seconds(path)
                           if path is not None else None)
        if source_duration is None:
            raise ValueError("PACKAGING_SFX_DURATION_UNKNOWN: 无法读取音效源文件时长")
        source_start = audio_edits["source_start_seconds"]
        if source_start is not None and (not isinstance(source_start, (int, float))
                or isinstance(source_start, bool) or not math.isfinite(source_start)
                or source_start < 0):
            raise ValueError("PACKAGING_SFX_SOURCE_START_INVALID: 素材内起点须为非负有限秒数")
        if (source_start or 0) + end - start > source_duration + 0.01:
            raise ValueError("PACKAGING_SFX_SOURCE_SHORT: 音效区间长于源文件，请缩短或更换素材")
        for key in ("fade_in_seconds", "fade_out_seconds"):
            seconds = audio_edits[key]
            if seconds is not None and (not isinstance(seconds, (int, float))
                    or isinstance(seconds, bool) or not math.isfinite(seconds)
                    or seconds < 0):
                raise ValueError("PACKAGING_SFX_FADE_INVALID: 淡入淡出须为非负有限秒数")
        fade_in = audio_edits["fade_in_seconds"] or 0
        fade_out = (audio_edits["fade_out_seconds"]
                    if audio_edits["fade_out_seconds"] is not None else min(.08, end - start))
        if fade_in + fade_out > end - start + 1e-9:
            raise ValueError("PACKAGING_SFX_FADE_INVALID: 淡入淡出总长不能超过取用片段")
    else:
        gain = None
        if any(edit is not None for edit in audio_edits.values()):
            raise ValueError("PACKAGING_SFX_FIELDS_INVALID: 非音效元素不能设置音频裁剪")
    return {
        "role": role, "asset_ref": asset_ref, "content_text": text,
        "anchor_kind": anchor_kind, "anchor_ref": anchor_ref,
        "start_seconds": float(start), "end_seconds": float(end),
        "x": x, "y": y, "scale": scale, "gain": gain,
        "content_layout_json": json.dumps(_content_layout(value, text), ensure_ascii=False, sort_keys=True),
        **audio_edits,
    }


def _content_layout(value, text):
    raw = value.get("content_layout_json") or "{}"
    layout = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(layout, dict): raise ValueError("PACKAGING_LAYOUT_INVALID: 分层必须是对象")
    if not layout: return {}
    if not text or layout.get("text") != text: return {}
    from .packaging_refined import validate_content_layout
    return validate_content_layout(layout, text)


def _editable_plan(connection, plan_id: str, expected_revision: int) -> dict:
    from .packaging_plan import _require_editable

    plan = dict(_require_editable(connection, plan_id))
    if plan["schema_version"] != 2:
        raise RuntimeError("PACKAGING_LEGACY_PLAN: 旧方案请先复制为新实例方案")
    if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
            or expected_revision != plan["instance_revision"]):
        raise RuntimeError("PACKAGING_REVISION_CONFLICT: 方案已被其他操作修改，请刷新")
    return plan


def _insert(connection, plan_id: str, value: dict, *, origin: str,
            follow_event: bool = False) -> str:
    instance_id = f"pi-{uuid.uuid4().hex[:16]}"
    connection.execute(
        "INSERT INTO packaging_plan_instances("
        "instance_id,plan_id,role,asset_ref,content_text,anchor_kind,anchor_ref,"
        "start_seconds,end_seconds,x,y,scale,gain,source_start_seconds,"
        "fade_in_seconds,fade_out_seconds,timing_rule,origin,follow_event) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'explicit',?,?)",
        (instance_id, plan_id, value["role"], value["asset_ref"],
         value["content_text"], value["anchor_kind"], value["anchor_ref"],
         value["start_seconds"], value["end_seconds"], value["x"], value["y"],
         value["scale"], value["gain"], value.get("source_start_seconds"),
         value.get("fade_in_seconds"), value.get("fade_out_seconds"),
         origin, int(follow_event)),
    )
    connection.execute("UPDATE packaging_plan_instances SET content_layout_json=? WHERE instance_id=?",
        (value.get("content_layout_json") or "{}", instance_id))
    return instance_id


def add_instance(db, plan_id: str, value: dict, *, expected_revision: int) -> dict:
    with db.transaction(immediate=True) as connection:
        plan = _editable_plan(connection, plan_id, expected_revision)
        validated = _validate_instance(connection, plan, value)
        category = connection.execute(
            "SELECT enabled FROM packaging_plan_items WHERE plan_id=? "
            "AND slot=? AND is_backup=0 ORDER BY item_id LIMIT 1",
            (plan_id, validated["role"]),
        ).fetchone()
        if category is not None and not category["enabled"]:
            raise RuntimeError("PACKAGING_CATEGORY_CLOSED: 请先恢复此元素类别")
        if category is None:
            connection.execute(
                "INSERT INTO packaging_plan_items(plan_id,slot,asset_ref) "
                "VALUES(?,?,?)",
                (plan_id, validated["role"], validated["asset_ref"]),
            )
        instance_id = _insert(connection, plan_id, validated, origin="manual")
        connection.execute(
            "UPDATE packaging_plans SET instance_revision=instance_revision+1 WHERE plan_id=?",
            (plan_id,),
        )
    return {"instance": next(item for item in list_instances(db.connection, plan_id)
                              if item["instance_id"] == instance_id),
            "instanceRevision": expected_revision + 1}


def _refresh_following_instances(connection, plan: dict, previous: dict,
                                 updated: dict) -> None:
    """Move model sounds with their event; preserve and recheck manual placements."""
    if (previous["start_seconds"], previous["end_seconds"]) == (
            updated["start_seconds"], updated["end_seconds"]):
        return
    queue = [(previous, updated)]
    visited = {previous["instance_id"]}
    while queue:
        _, parent = queue.pop(0)
        children = connection.execute(
            "SELECT * FROM packaging_plan_instances WHERE plan_id=? "
            "AND anchor_kind='instance' AND anchor_ref=? ORDER BY instance_id",
            (plan["plan_id"], parent["instance_id"]),
        ).fetchall()
        for row in children:
            child = dict(row)
            if child["instance_id"] in visited:
                raise ValueError("PACKAGING_ANCHOR_CYCLE: 包装元素不能循环跟随")
            visited.add(child["instance_id"])
            category = connection.execute(
                "SELECT enabled FROM packaging_plan_items WHERE plan_id=? "
                "AND slot=? AND is_backup=0 ORDER BY item_id LIMIT 1",
                (plan["plan_id"], child["role"]),
            ).fetchone()
            active = category is None or bool(category["enabled"])
            if (child["role"] == "sfx" and child["follow_event"]
                    and parent["role"] == "title_card"):
                from . import packaging_plan

                if active:
                    source = packaging_plan.resolve_manifest_asset_path(
                        "sfx", child["asset_ref"])
                    source_duration = (packaging_plan.audio_duration_seconds(source)
                                       if source is not None else None)
                    if source_duration is None:
                        raise ValueError("PACKAGING_SFX_DURATION_UNKNOWN: 无法读取音效源文件时长")
                else:
                    # Closed optional media may be offline. Retain its last valid
                    # span until it is reopened and the real source can be checked.
                    try:
                        source_duration = float(child["end_seconds"] - child["start_seconds"])
                    except TypeError:
                        continue
                start = float(parent["start_seconds"])
                available_duration = (source_duration
                                      - float(child.get("source_start_seconds") or 0)
                                      if active else source_duration)
                end = round(min(float(parent["end_seconds"]),
                                start + available_duration), 3)
                if end <= start and not active:
                    continue
                validated = (_validate_instance(connection, plan, {
                    **child, "start_seconds": start, "end_seconds": end,
                }) if active else {**child, "start_seconds": start, "end_seconds": end})
                changed = (child["start_seconds"], child["end_seconds"]) != (start, end)
                if changed:
                    connection.execute(
                        "UPDATE packaging_plan_instances SET start_seconds=?,end_seconds=?,"
                        "revision=revision+1,updated_at=datetime('now') "
                        "WHERE plan_id=? AND instance_id=?",
                        (validated["start_seconds"], validated["end_seconds"],
                         plan["plan_id"], child["instance_id"]),
                    )
                    queue.append((child, {**child, **validated}))
                continue
            if active:
                _validate_instance(connection, plan, child)


def update_instance(db, plan_id: str, instance_id: str, changes: dict,
                    *, expected_revision: int) -> dict:
    allowed = {"asset_ref", "content_text", "anchor_kind", "anchor_ref",
               "start_seconds", "end_seconds", "x", "y", "scale", "gain",
               "source_start_seconds", "fade_in_seconds", "fade_out_seconds"}
    if not changes or any(key not in allowed for key in changes):
        raise ValueError("PACKAGING_INSTANCE_FIELDS_INVALID: 修改字段无效")
    with db.transaction(immediate=True) as connection:
        plan = _editable_plan(connection, plan_id, expected_revision)
        old = connection.execute(
            "SELECT * FROM packaging_plan_instances WHERE plan_id=? AND instance_id=?",
            (plan_id, instance_id),
        ).fetchone()
        if old is None:
            raise KeyError("PACKAGING_INSTANCE_NOT_FOUND: 元素不存在")
        if old["timing_rule"] != "explicit":
            raise RuntimeError("PACKAGING_LEGACY_INSTANCE: 历史元素不能原地改写")
        payload = {**dict(old), **changes}
        if payload["asset_ref"] != old["asset_ref"]: payload["content_layout_json"] = "{}"
        validated = _validate_instance(connection, plan, payload)
        if all(validated[key] == old[key] for key in allowed):
            return {"instance": dict(old), "instanceRevision": expected_revision}
        follow_event = int(bool(old["follow_event"]) and all(
            validated[key] == old[key]
            for key in ("anchor_kind", "anchor_ref", "start_seconds", "end_seconds")
        ))
        connection.execute(
            "UPDATE packaging_plan_instances SET asset_ref=?,content_text=?,anchor_kind=?,"
            "anchor_ref=?,start_seconds=?,end_seconds=?,x=?,y=?,scale=?,gain=?,"
            "source_start_seconds=?,fade_in_seconds=?,fade_out_seconds=?,follow_event=?,"
            "origin='manual',revision=revision+1,updated_at=datetime('now') "
            "WHERE plan_id=? AND instance_id=?",
            (validated["asset_ref"], validated["content_text"], validated["anchor_kind"],
             validated["anchor_ref"], validated["start_seconds"], validated["end_seconds"],
             validated["x"], validated["y"], validated["scale"], validated["gain"],
             validated["source_start_seconds"], validated["fade_in_seconds"],
             validated["fade_out_seconds"],
             follow_event,
             plan_id, instance_id),
        )
        connection.execute("UPDATE packaging_plan_instances SET content_layout_json=? WHERE instance_id=?",
            (validated["content_layout_json"], instance_id))
        _refresh_following_instances(connection, plan, dict(old),
                                     {**dict(old), **validated})
        connection.execute(
            "UPDATE packaging_plans SET instance_revision=instance_revision+1 WHERE plan_id=?",
            (plan_id,),
        )
    return {"instance": next(item for item in list_instances(db.connection, plan_id)
                              if item["instance_id"] == instance_id),
            "instanceRevision": expected_revision + 1}


def delete_instance(db, plan_id: str, instance_id: str, *, expected_revision: int) -> dict:
    with db.transaction(immediate=True) as connection:
        _editable_plan(connection, plan_id, expected_revision)
        dependents = connection.execute(
            "SELECT 1 FROM packaging_plan_instances WHERE plan_id=? AND "
            "anchor_kind='instance' AND anchor_ref=? LIMIT 1", (plan_id, instance_id),
        ).fetchone()
        if dependents is not None:
            raise RuntimeError("PACKAGING_INSTANCE_IN_USE: 先调整跟随此元素的音效或其他元素")
        changed = connection.execute(
            "DELETE FROM packaging_plan_instances WHERE plan_id=? AND instance_id=?",
            (plan_id, instance_id),
        ).rowcount
        if changed != 1:
            raise KeyError("PACKAGING_INSTANCE_NOT_FOUND: 元素不存在")
        connection.execute(
            "UPDATE packaging_plans SET instance_revision=instance_revision+1 WHERE plan_id=?",
            (plan_id,),
        )
    return {"deletedInstanceId": instance_id, "instanceRevision": expected_revision + 1}


def insert_default_for_slot(connection, plan: dict, slot: str, asset_ref: str,
                            *, recommendation: dict | None = None) -> None:
    """Create a task-specific recommendation tied to actual scene intervals."""
    if plan["schema_version"] != 2 or slot not in ROLES:
        return
    scenes = connection.execute(
        "SELECT card_id,narrative_text,narrative_start,narrative_end FROM scene_cards "
        "WHERE task_id=? ORDER BY idx,card_id", (plan["task_id"],),
    ).fetchall()
    if not scenes:
        return
    evidence_roles = TEXT_ROLES | {"sfx", "sticker"}
    if slot in evidence_roles and not recommendation:
        return
    if slot in evidence_roles:
        x, y, scale = DEFAULT_POSITION[slot]
        validated = _validate_instance(connection, plan, {
            "role": slot, "asset_ref": asset_ref,
            "content_text": (recommendation["content_text"]
                             if slot in TEXT_ROLES else None),
            "anchor_kind": recommendation["anchor_kind"],
            "anchor_ref": recommendation["anchor_ref"],
            "start_seconds": recommendation["start_seconds"],
            "end_seconds": recommendation["end_seconds"],
            "x": x, "y": y, "scale": scale,
            "gain": recommendation.get("gain", 0.35) if slot == "sfx" else None,
        })
        _insert(connection, plan["plan_id"], validated, origin="recommendation",
                follow_event=(slot == "sfx" and recommendation["anchor_kind"] == "instance"))
        connection.execute(
            "UPDATE packaging_plans SET instance_revision=instance_revision+1 WHERE plan_id=?",
            (plan["plan_id"],),
        )
        return
    if slot != "brand":
        return
    duration = max(float(scene["narrative_end"]) for scene in scenes)
    if duration <= 0:
        return
    x, y, scale = DEFAULT_POSITION[slot]
    _insert(connection, plan["plan_id"], {
        "role": slot, "asset_ref": asset_ref, "content_text": None,
        "anchor_kind": "timeline", "anchor_ref": None,
        "start_seconds": 0.0, "end_seconds": duration,
        "x": x, "y": y, "scale": scale, "gain": None,
    }, origin="recommendation")
    connection.execute(
        "UPDATE packaging_plans SET instance_revision=instance_revision+1 WHERE plan_id=?",
        (plan["plan_id"],),
    )
