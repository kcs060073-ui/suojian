"""Compile validated packaging selections into renderer-neutral mix layers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import packaging_plan
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
    for item in plan["items"]:
        if not item["is_backup"] and item["slot"] not in selected:
            selected[item["slot"]] = item
    return selected


def _compile_item(item: dict) -> dict:
    slot = item["slot"]
    category = "title_card" if slot == "brand" else slot
    manifest = packaging_plan.find_item(category, item["asset_ref"])
    if manifest is None:  # validate_plan_for_render normally catches this first
        raise RuntimeError(
            f"PACKAGING_MANIFEST_UNKNOWN: manifest 未登记包装项：{slot}/{item['asset_ref']}"
        )
    path = packaging_plan.resolve_manifest_asset_path(category, item["asset_ref"])
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
    selected = _selected_items(plan)
    compiled_items = [_compile_item(item) for item in plan["items"]]
    compiled_primary = {
        item["slot"]: item
        for item in compiled_items
        if not item["is_backup"]
    }
    fields: dict = {}
    coverage: list[dict] = []

    title_start, title_end = _cue(context.duration_seconds, 0.0, 2.5)
    sticker_start, sticker_end = _cue(context.duration_seconds, 2.5, 2.0)
    fancy_start, fancy_end = _cue(context.duration_seconds, 4.5, 2.5)

    for slot in packaging_plan.SLOTS:
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
                "gain": manifest.get("default_gain", 0.18),
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

    snapshot = {
        "plan_id": plan_id,
        "items": compiled_items,
        "coverage": coverage,
    }
    return CompiledPackaging(snapshot, fields, coverage)
