"""Frozen render timeline shared by production render-plan projections.

This module introduces the route-neutral time and asset contract. The ad
projection is the first consumer; digital-human and editable-project adapters
will use the same fields as they migrate from their older MixPlan formats.
"""

from __future__ import annotations

from .audio_mix_policy import audio_mix_policy

import json
from pathlib import Path

from .provenance import canonical_digest
from .snapshots import file_content_signature
from .timeline_contract import TimeWindow, frame_boundary, seconds_to_us, validate_coverage


RECIPE_VERSION = 1
AD_TRANSITION_US = 400_000  # Existing render-ad.ts fade handle.
AD_OUTPUT = {
    "fps_num": 30, "fps_den": 1, "pixel_format": "yuv420p",
    "audio_rate": 48000, "video_codec": "libx264", "audio_codec": "aac",
    "crf": 23,
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_PACKAGING_FIELDS = ("bgm", "sfx", "titles", "stickers", "fancyTexts", "watermark")


def _manifest_entry(kind: str, source: str | Path) -> dict:
    path = Path(source).resolve()
    if not path.is_file():
        raise RuntimeError(f"RECIPE_ASSET_MISSING: {kind}/{path}")
    return {"kind": kind, "path": str(path), **file_content_signature(path)}


def _ad_shot(segment: dict, output: dict) -> dict:
    window = TimeWindow(seconds_to_us(segment["start"]), seconds_to_us(segment["end"]))
    if window.duration_us != seconds_to_us(segment["dur"]):
        raise RuntimeError("RECIPE_SHOT_DURATION_MISMATCH: 广告镜头时长与输出区间不一致")
    source = str(Path(segment["src"]).resolve())
    media_kind = segment.get("mediaKind") or (
        "image" if Path(source).suffix.lower() in _IMAGE_EXTENSIONS else "video"
    )
    if media_kind not in {"image", "video"}:
        raise RuntimeError("RECIPE_MEDIA_KIND_INVALID: 广告镜头只支持图片或视频")
    shot = {
        "index": int(segment["i"]),
        "shot_id": segment.get("cardId"),
        "asset_id": segment.get("assetId"),
        "segment_id": segment.get("segmentId"),
        "media_kind": media_kind,
        "src": source,
        "output_start_us": window.start_us,
        "output_end_us": window.end_us,
        "output_start_frame": frame_boundary(
            window.start_us, fps_num=output["fps_num"], fps_den=output["fps_den"]
        ),
        "output_end_frame": frame_boundary(
            window.end_us, fps_num=output["fps_num"], fps_den=output["fps_den"]
        ),
        "highlight_terms": list(segment.get("highlightTerms") or []),
    }
    if media_kind == "image":
        if "sourceStart" in segment or "sourceEnd" in segment:
            raise RuntimeError("IMAGE_HAS_SOURCE_WINDOW: 图片只有成片展示区间")
    else:
        explicit = "sourceStart" in segment or "sourceEnd" in segment
        if explicit and ("sourceStart" not in segment or "sourceEnd" not in segment):
            raise RuntimeError("SOURCE_WINDOW_REQUIRED: 视频源入出点必须成对提供")
        start_us = seconds_to_us(segment.get("sourceStart", 0))
        end_us = seconds_to_us(segment.get("sourceEnd", segment["dur"]))
        if end_us - start_us < window.duration_us:
            raise RuntimeError("SOURCE_WINDOW_TOO_SHORT: 视频源区间不足以覆盖镜头")
        shot.update(source_start_us=start_us, source_end_us=end_us,
                    source_window_explicit=explicit)
    return shot


def _packaging_instances(fields: dict, duration_us: int) -> list[dict]:
    result = []
    for field in _PACKAGING_FIELDS:
        value = fields.get(field)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise RuntimeError("RECIPE_PACKAGING_INVALID: 包装层必须是对象")
            start_us = seconds_to_us(item.get("start", 0))
            end_us = seconds_to_us(item.get("end", duration_us / 1_000_000))
            if not 0 <= start_us < end_us <= duration_us:
                raise RuntimeError("RECIPE_PACKAGING_TIME_INVALID: 包装实例超出成片")
            result.append({
                "instance_id": item.get("instanceId") or f"legacy:{field}:{index}",
                "kind": field,
                "start_us": start_us,
                "end_us": end_us,
                "render_value": dict(item),
            })
    return result


def compile_ad_recipe(
    *, task_id: str, ratio: str, segments: list[dict], voice_path: Path,
    subtitle_preset: dict, packaging_snapshot: dict, packaging_fields: dict,
    picture_lock_digest: str | None,
    subtitle_font_path: Path | None = None,
    display_cues: list[dict] | None = None,
) -> dict:
    """Compile one frozen ad timeline before full or low-resolution rendering."""
    if ratio not in {"9:16", "16:9"}:
        raise RuntimeError("AD_ASPECT_RATIO_INVALID: 广告配方缺少锁定画幅")
    if not segments:
        raise RuntimeError("RECIPE_SHOTS_EMPTY: 广告配方缺少镜头")
    output = {
        "width": 1080 if ratio == "9:16" else 1920,
        "height": 1920 if ratio == "9:16" else 1080,
        **AD_OUTPUT,
    }
    shots = [_ad_shot(segment, output) for segment in segments]
    duration_us = shots[-1]["output_end_us"]
    validate_coverage(
        (TimeWindow(shot["output_start_us"], shot["output_end_us"]) for shot in shots),
        TimeWindow(0, duration_us),
    )
    voice_src = str(Path(voice_path).resolve())
    voice_setting = packaging_fields.get("voice")
    voice_preset_id = (
        voice_setting.get("presetId") if isinstance(voice_setting, dict) else None
    )
    task_snapshot = {
        "task_id": task_id, "task_type": "ad_mix", "aspect_ratio": ratio,
        "picture_lock_digest": picture_lock_digest,
        "packaging_plan_id": packaging_snapshot.get("plan_id"),
    }
    cues = []
    for shot, segment in zip(shots, segments, strict=True):
        cue = {**(segment.get("speech") or {
            "index": shot["index"], "text": segment["text"],
            "start_us": shot["output_start_us"], "end_us": shot["output_end_us"],
        }), "highlight_terms": shot["highlight_terms"]}
        if not (cue["start_us"] <= shot["output_start_us"] < shot["output_end_us"] <= cue["end_us"]):
            raise RuntimeError("RECIPE_CUE_WINDOW_MISMATCH: 镜头超出对应配音区间")
        if cues and cue["index"] == cues[-1]["index"]:
            if cue != cues[-1]:
                raise RuntimeError("RECIPE_CUE_WINDOW_MISMATCH: 同一配音段内容不一致")
        else:
            if cue["index"] != len(cues):
                raise RuntimeError("RECIPE_CUE_WINDOW_MISMATCH: 配音段顺序不连续")
            cues.append(cue)
    validate_coverage((TimeWindow(cue["start_us"], cue["end_us"]) for cue in cues), TimeWindow(0, duration_us))
    display = None
    if display_cues is not None:
        display = []
        last_end = 0
        for cue in display_cues:
            start = seconds_to_us(cue.get('subtitleStart', cue['start']))
            end = seconds_to_us(cue.get('subtitleEnd', cue['end']))
            if not 0 <= last_end <= start < end <= duration_us:
                raise RuntimeError('RECIPE_DISPLAY_CUE_INVALID: 字幕时间区间无效或重叠')
            display.append({'start_us': start, 'end_us': end,
                            'text': str(cue.get('text') or ''),
                            'display_text': str(cue.get('displayText', cue.get('text')) or '')})
            last_end = end
    instances = _packaging_instances(packaging_fields, duration_us)
    manifest: dict[tuple[str, str], dict] = {}
    for shot in shots:
        kind = f"shot:{shot['asset_id'] or shot['index']}"
        entry = _manifest_entry(kind, shot["src"])
        manifest[(kind, entry["path"])] = entry
    entry = _manifest_entry("main_audio", voice_src)
    manifest[("main_audio", entry["path"])] = entry
    appearance_font = packaging_snapshot.get("appearance_font")
    if appearance_font:
        entry = _manifest_entry("subtitle_appearance_font", appearance_font["file"])
        manifest[(entry["kind"], entry["path"])] = entry
    if subtitle_font_path is not None:
        entry = _manifest_entry("subtitle_font", subtitle_font_path)
        manifest[("subtitle_font", entry["path"])] = entry
    for instance in instances:
        source = instance["render_value"].get("src")
        if source:
            kind = f"packaging:{instance['instance_id']}"
            entry = _manifest_entry(kind, source)
            manifest[(kind, entry["path"])] = entry
    # Text render values carry a font family, not a src. Freeze their actual
    # selected files from the compiled packaging snapshot, including fonts used
    # only by non-primary instances. Shared subtitle/instance fonts occur once.
    signed_paths = {entry["path"] for entry in manifest.values()}
    for item in [*packaging_snapshot.get("items", []), *packaging_snapshot.get("instances", [])]:
        role = item.get("role") or item.get("slot")
        source = item.get("file")
        if role not in {"title_card", "info_card", "fancy_text"} or item.get("is_backup") or not source:
            continue
        if str(Path(source).resolve()) in signed_paths:
            continue
        entry = _manifest_entry("packaging_font", source)
        manifest[(entry["kind"], entry["path"])] = entry
        signed_paths.add(entry["path"])
    asset_manifest = [manifest[key] for key in sorted(manifest)]
    transitions = [
        {
            "kind": "fade", "left_shot_id": left["shot_id"],
            "right_shot_id": right["shot_id"],
            "duration_us": AD_TRANSITION_US,
            "output_at_us": left["output_end_us"],
        }
        for left, right in zip(shots, shots[1:])
    ]
    dependencies = {
        "task_snapshot": task_snapshot, "asset_manifest": asset_manifest,
        "packaging_snapshot": packaging_snapshot,
        "subtitle_preset": subtitle_preset,
    }
    return {
        "recipe_version": RECIPE_VERSION,
        "audio_policy": audio_mix_policy(),
        "task_snapshot": task_snapshot,
        "duration_us": duration_us,
        "output": output,
        "shots": shots,
        "main_audio": {
            "src": voice_src, "source_start_us": 0, "source_end_us": duration_us,
            "output_start_us": 0, "output_end_us": duration_us,
            "preset_id": voice_preset_id,
        },
        "subtitle_cues": cues,
        **({"display_cues": display} if display is not None else {}),
        "subtitle_style": dict(subtitle_preset),
        "subtitle_highlight": bool(packaging_fields.get("subtitleHighlight", False)),
        **({"subtitle_appearance": packaging_fields["subtitleAppearance"]} if packaging_fields.get("subtitleAppearance") else {}),
        "packaging_instances": instances,
        "transitions": transitions,
        "asset_manifest": asset_manifest,
        "dependency_digest": canonical_digest(dependencies),
    }


def ad_plan_from_recipe(recipe: dict, *, preview_output: dict | None = None) -> dict:
    """Project the frozen recipe into the current FFmpeg render-ad interface."""
    if recipe.get("recipe_version") != RECIPE_VERSION:
        raise RuntimeError("RECIPE_VERSION_UNSUPPORTED: 广告渲染配方版本无效")
    cues = recipe["subtitle_cues"]
    segments = []
    for shot in recipe["shots"]:
        matching = [cue for cue in cues if cue["start_us"] <= shot["output_start_us"] and shot["output_end_us"] <= cue["end_us"]]
        if len(matching) != 1:
            raise RuntimeError("RECIPE_CUE_WINDOW_MISMATCH: 镜头无法对应唯一配音段")
        cue = matching[0]
        segment = {
            "i": shot["index"], "cardId": shot["shot_id"],
            "assetId": shot["asset_id"], "mediaKind": shot["media_kind"],
            "text": cue["text"], "start": shot["output_start_us"] / 1_000_000,
            "end": shot["output_end_us"] / 1_000_000,
            "dur": (shot["output_end_us"] - shot["output_start_us"]) / 1_000_000,
            "src": shot["src"], "matched": True,
            "segmentId": shot["segment_id"],
        }
        if shot.get("source_window_explicit"):
            segment["sourceStart"] = shot["source_start_us"] / 1_000_000
            segment["sourceEnd"] = shot["source_end_us"] / 1_000_000
        if cue.get("highlight_terms"):
            segment["highlightTerms"] = list(cue["highlight_terms"])
        segments.append(segment)
    voice = recipe["main_audio"]
    voice_value = (
        {"src": voice["src"], "presetId": voice["preset_id"]}
        if voice.get("preset_id") else voice["src"]
    )
    plan = {
        "name": recipe["task_snapshot"]["task_id"],
        "ratio": recipe["task_snapshot"]["aspect_ratio"],
        "total": recipe["duration_us"] / 1_000_000,
        "voice": voice_value,
        "segments": segments,
        "subtitleSegments": [{"i": cue["index"], "text": cue["text"],
                              "start": cue["start_us"] / 1_000_000, "end": cue["end_us"] / 1_000_000,
                              "highlightTerms": cue.get("highlight_terms", [])} for cue in cues],
        "subtitlePreset": recipe["subtitle_style"],
        "subtitleHighlight": recipe["subtitle_highlight"],
        **({"subtitleAppearance": recipe["subtitle_appearance"]} if recipe.get("subtitle_appearance") else {}),
        "recipe_digest": canonical_digest(recipe),
        "renderRecipeJson": json.dumps(
            recipe, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }
    if 'display_cues' in recipe:
        plan['subtitleSegments'] = [{'text': cue['text'], 'displayText': cue['display_text'],
                                    'start': cue['start_us'] / 1_000_000,
                                    'end': cue['end_us'] / 1_000_000}
                                   for cue in recipe['display_cues']]
    for instance in recipe["packaging_instances"]:
        field = instance["kind"]
        value = dict(instance["render_value"])
        if field in {"bgm", "watermark"}:
            if field in plan:
                raise RuntimeError("RECIPE_PACKAGING_DUPLICATE: 单实例包装层重复")
            plan[field] = value
        else:
            plan.setdefault(field, []).append(value)
    if preview_output is not None:
        plan["output"] = dict(preview_output)
    return plan


def preview_receipt(recipe: dict, timeline_map: tuple[dict, ...] | list[dict] = ()) -> dict:
    """Bind a preview's full-timeline coverage and exact asset bytes to its Recipe."""
    intervals = (
        [{"start_us": seconds_to_us(item["fullStart"]),
          "end_us": seconds_to_us(item["fullEnd"])} for item in timeline_map]
        if timeline_map else [{"start_us": 0, "end_us": recipe["duration_us"]}]
    )
    return {
        "recipe_digest": canonical_digest(recipe),
        "covered_output_intervals": intervals,
        "asset_versions": list(recipe["asset_manifest"]),
    }
