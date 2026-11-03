"""Compile validated packaging selections into renderer-neutral mix layers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import packaging_plan
from .packaging_plan import active_instances, active_items
from .packaging_instances import ROLES
from .snapshots import file_content_signature


@dataclass(frozen=True)
class PackagingTimingContext:
    duration_seconds: float
    title: str
    keyword: str
    voice_mode: str


@dataclass(frozen=True)
class CompiledPackaging:
    snapshot: dict
    plan_fields: dict
    coverage: list[dict]


def _cue(
    duration: float, preferred_start: float, length: float
) -> tuple[float, float]:
    bounded_length = min(length, duration)
    start = min(preferred_start, max(0.0, duration - bounded_length))
    return start, min(duration, start + bounded_length)


def _coverage(
    slot: str, status: str, reason_code: str | None = None
) -> dict:
    return {"slot": slot, "status": status, "reasonCode": reason_code}


def _selected_items(plan: dict) -> dict[str, dict]:
    selected: dict[str, dict] = {}
    for item in active_items(plan):
        if not item["is_backup"] and item["slot"] not in selected:
            selected[item["slot"]] = item
    return selected


def _compile_item(item: dict, db=None) -> dict:
    slot = item["slot"]
    category = "title_card" if slot in {"brand", "info_card"} else slot
    manifest = packaging_plan.find_item(category, item["asset_ref"], db)
    if manifest is None:  # validate_plan_for_render normally catches this first
        raise RuntimeError(
            f"PACKAGING_MANIFEST_UNKNOWN: manifest 未登记包装项：{slot}/{item['asset_ref']}"
        )
    if manifest.get("_personal"):
        manifest = {k: v for k, v in manifest.items() if k != "name"}
    path = packaging_plan.resolve_manifest_asset_path(category, item["asset_ref"], db)
    compiled = {
        "slot": slot,
        "asset_ref": item["asset_ref"],
        "asset_version": item.get("asset_version"),
        "is_backup": bool(item["is_backup"]),
        "rank": item["rank"],
        "license": manifest.get("license", "unknown"),
        "file": None if path is None else str(path),
        "manifest": manifest,
    }
    if path is not None:
        compiled["file_signature"] = file_content_signature(path)
    return compiled


def compile_packaging_layers(
    db, plan_id: str, context: PackagingTimingContext
) -> CompiledPackaging:
    if context.duration_seconds <= 0:
        raise ValueError("PACKAGING_DURATION_INVALID: 包装时长必须大于 0")
    plan = packaging_plan.validate_plan_for_render(db, plan_id)
    from .packaging_layout import inspect_packaging_layout, assert_packaging_layout
    layout = inspect_packaging_layout(db, plan)
    assert_packaging_layout(layout)
    selected = _selected_items(plan)
    compiled_items = [_compile_item(item, db) for item in active_items(plan)]
    compiled_primary = {
        item["slot"]: item
        for item in compiled_items
        if not item["is_backup"]
    }
    fields: dict = {}
    coverage: list[dict] = []
    instance_mode = int(plan.get("schema_version", 1)) >= 2
    frozen_settings = instance_mode and int(plan.get("settings_schema_version", 0)) >= 1
    if frozen_settings:
        fields["subtitleHighlight"] = bool(plan.get("subtitle_highlight", 1))
        from .packaging_appearance import render_appearance
        if render_appearance(plan, db):
            fields["subtitleAppearance"] = render_appearance(plan, db)

    title_start, title_end = _cue(context.duration_seconds, 0.0, 2.5)
    sticker_start, sticker_end = _cue(context.duration_seconds, 2.5, 2.0)
    fancy_start, fancy_end = _cue(context.duration_seconds, 4.5, 2.5)

    for slot in packaging_plan.SLOTS:
        if instance_mode and slot in ROLES:
            continue
        item = selected.get(slot)
        compiled = compiled_primary.get(slot)
        if item is None or compiled is None:
            continue
        manifest = compiled["manifest"]
        asset_ref = item["asset_ref"]
        file_path = compiled["file"]

        if slot == "voice":
            if context.voice_mode == "original":
                coverage.append(
                    _coverage("voice", "not_applicable", "ORIGINAL_VOICE_IN_USE")
                )
            elif context.voice_mode.startswith("cached:"):
                cached = Path(context.voice_mode.removeprefix("cached:")).resolve()
                if cached.is_file():
                    fields["voice"] = {"src": str(cached), "presetId": asset_ref}
                    coverage.append(_coverage("voice", "rendered"))
                else:
                    coverage.append(
                        _coverage("voice", "failed", "VOICE_CACHE_MISSING")
                    )
            else:
                coverage.append(_coverage("voice", "failed", "VOICE_CACHE_MISSING"))
        elif slot == "bgm":
            fields["bgm"] = {
                "src": file_path,
                "gain": (plan.get("bgm_gain") if frozen_settings and plan.get("bgm_gain") is not None
                         else manifest.get("default_gain", 0.18)),
                "duck": True,
            }
            coverage.append(_coverage("bgm", "rendered"))
        elif slot == "sfx":
            cue_start = sticker_start if "sticker" in selected else title_start
            fields["sfx"] = [{
                "src": file_path,
                "start": cue_start,
                "gain": manifest.get("default_gain", 0.35),
            }]
            coverage.append(_coverage("sfx", "rendered"))
        elif slot == "subtitle":
            fields["subtitlePresetId"] = asset_ref
            if manifest.get("_personal"):
                fields["subtitleAppearance"] = {"fontFamily": manifest.get("fontname_guess"), "fontSize": manifest.get("size", 93), "primaryHex": manifest.get("primary_hex", "#FFFFFF"), "highlightHex": manifest.get("highlight_hex", "#FD9437"), "outlineHex": manifest.get("outline_hex", "#000000"), **fields.get("subtitleAppearance", {})}
            coverage.append(_coverage("subtitle", "rendered"))
        elif slot == "title_card":
            fields["titles"] = [{
                "text": context.title,
                "start": title_start,
                "end": title_end,
                "presetId": asset_ref,
                "style": {
                    "fontFamily": manifest.get("fontname_guess")
                    or Path(str(manifest.get("font") or "")).stem,
                    "primaryHex": manifest.get("accent_hex", "#FFFFFF"),
                    "backgroundHex": manifest.get("background_hex", "#000000"),
                },
            }]
            coverage.append(_coverage("title_card", "rendered"))
        elif slot == "sticker":
            fields["stickers"] = [{
                "src": file_path,
                "presetId": asset_ref,
                "start": sticker_start,
                "end": sticker_end,
                "x": 0.72,
                "y": 0.12,
                "scale": 0.2,
                "opacity": 1.0,
            }]
            coverage.append(_coverage("sticker", "rendered"))
        elif slot == "fancy_text":
            fields["fancyTexts"] = [{
                "text": context.keyword,
                "presetId": asset_ref,
                "start": fancy_start,
                "end": fancy_end,
                "x": 0.08,
                "y": 0.18,
                "scale": 0.25,
                "opacity": 1.0,
                "entrance": manifest.get("entrance", "fade"),
                "style": {
                    "fontFamily": manifest.get("fontname_guess")
                    or Path(str(manifest.get("font") or "")).stem,
                    "primaryHex": (manifest.get("fill") or ["#FFB300"])[0],
                    "outlineHex": manifest.get("stroke_hex", "#000000"),
                    "backgroundHex": manifest.get("bg_hex"),
                    "glowHex": manifest.get("glow"),
                },
            }]
            coverage.append(_coverage("fancy_text", "rendered"))
        elif slot == "brand":
            fields["watermark"] = {
                "src": file_path,
                "opacity": 1.0,
            }
            coverage.append(_coverage("brand", "rendered"))

    compiled_instances: list[dict] = []
    if instance_mode:
        for instance in active_instances(plan):
            if instance["timing_rule"] != "explicit":
                raise RuntimeError("PACKAGING_INSTANCE_RULE_INVALID: 新方案不能沿用旧固定落点")
            role = instance["role"]
            start = instance["start_seconds"]
            end = instance["end_seconds"]
            if start is None or end is None or not 0 <= start < end <= context.duration_seconds:
                raise RuntimeError("PACKAGING_INSTANCE_TIME_INVALID: 包装实例超出当前成片")
            category = "title_card" if role in {"title_card", "info_card", "brand"} else role
            manifest = packaging_plan.find_item(category, instance["asset_ref"], db)
            if manifest is None:
                raise RuntimeError("PACKAGING_MANIFEST_UNKNOWN: 包装实例素材未登记")
            path = packaging_plan.resolve_manifest_asset_path(category, instance["asset_ref"], db)
            item = {
                "instanceId": instance["instance_id"], "role": role,
                "assetRef": instance["asset_ref"], "start": start, "end": end,
                "text": instance["content_text"], "x": instance["x"],
                "y": instance["y"], "scale": instance["scale"],
                "gain": instance["gain"], "anchorKind": instance["anchor_kind"],
                "anchorRef": instance["anchor_ref"],
                "file": None if path is None else str(path), "manifest": manifest,
            }
            if role == "sfx":
                for source_key, output_key in (
                    ("source_start_seconds", "sourceStart"),
                    ("fade_in_seconds", "fadeIn"),
                    ("fade_out_seconds", "fadeOut"),
                ):
                    if instance.get(source_key) is not None:
                        item[output_key] = instance[source_key]
            if path is not None:
                item["fileSignature"] = file_content_signature(path)
            if __import__("json").loads(instance.get("content_layout_json") or "{}"):
                item["contentLayout"] = __import__("json").loads(instance["content_layout_json"])
            compiled_instances.append(item)
            if role in {"title_card", "info_card"}:
                fields.setdefault("titles", []).append({
                    "instanceId": item["instanceId"], "role": role,
                    "text": item["text"], "start": start, "end": end,
                    "x": item["x"], "y": item["y"], "scale": item["scale"],
                    "presetId": item["assetRef"],
                    "contentLayout": __import__("json").loads(instance.get("content_layout_json") or "{}"),
                    "designVersion": manifest.get("design_version"),
                    "style": {
                        "fontFamily": manifest.get("fontname_guess")
                        or Path(str(manifest.get("font") or "")).stem,
                        "primaryHex": manifest.get("accent_hex", "#FFFFFF"),
                        "backgroundHex": manifest.get("background_hex", "#000000"),
                    },
                })
            elif role in {"brand", "sticker"}:
                fields.setdefault("stickers", []).append({
                    "instanceId": item["instanceId"], "role": role,
                    "src": item["file"], "presetId": item["assetRef"],
                    "start": start, "end": end, "x": item["x"],
                    "y": item["y"], "scale": item["scale"], "opacity": 1.0,
                })
            elif role == "fancy_text":
                fields.setdefault("fancyTexts", []).append({
                    "instanceId": item["instanceId"], "text": item["text"],
                    "presetId": item["assetRef"], "start": start, "end": end,
                    "x": item["x"], "y": item["y"], "scale": item["scale"],
                    "opacity": 1.0, "entrance": manifest.get("entrance", "fade"),
                    "role": role, "designVersion": manifest.get("design_version"),
                    "contentLayout": __import__("json").loads(instance.get("content_layout_json") or "{}"),
                    "style": {
                        "fontFamily": manifest.get("fontname_guess")
                        or Path(str(manifest.get("font") or "")).stem,
                        "primaryHex": (manifest.get("fill") or ["#FFB300"])[0],
                        "outlineHex": manifest.get("stroke_hex", "#000000"),
                        "backgroundHex": manifest.get("bg_hex"),
                        "glowHex": manifest.get("glow"),
                    },
                })
            elif role == "sfx":
                fields.setdefault("sfx", []).append({
                    "instanceId": item["instanceId"], "src": item["file"],
                    "start": start, "end": end, "gain": item["gain"],
                    **{key: item[key] for key in ("sourceStart", "fadeIn", "fadeOut")
                       if key in item},
                })
            coverage.append({"slot": role, "instanceId": item["instanceId"],
                             "status": "rendered", "reasonCode": None})

    snapshot = {
        "plan_id": plan_id,
        "items": compiled_items,
        "instances": compiled_instances,
        "coverage": coverage,
        **({"layout": layout} if instance_mode else {}),
    }
    from .packaging_appearance import read_appearance
    font_ref = read_appearance(plan).get("fontPresetId")
    if font_ref:
        font_path = packaging_plan.resolve_manifest_asset_path("subtitle", font_ref, db)
        snapshot["appearance_font"] = {"file": str(font_path), "file_signature": file_content_signature(font_path)}
    if instance_mode:
        snapshot["disabled_slots"] = sorted({item["slot"] for item in plan["items"]
                                             if not item.get("enabled", 1)})
    if frozen_settings:
        snapshot["settings"] = {
            "subtitle_highlight": bool(plan.get("subtitle_highlight", 1)),
            "bgm_gain": plan.get("bgm_gain"),
            **({"subtitle_appearance": fields["subtitleAppearance"]} if fields.get("subtitleAppearance") else {}),
        }
    return CompiledPackaging(snapshot, fields, coverage)
