"""Compile the locked digital-human timeline into the shared RenderRecipe shape."""

from __future__ import annotations

from .audio_mix_policy import audio_mix_policy

import copy
import json
from pathlib import Path

from .provenance import canonical_digest
from .render_recipe import RECIPE_VERSION, _manifest_entry, _packaging_instances
from .timeline_contract import TimeWindow, frame_boundary, seconds_to_us, validate_coverage


_SPEED_NUM = 5
_SPEED_DEN = 4
_PACKAGING_FIELDS = ("bgm", "sfx", "titles", "stickers", "fancyTexts", "watermark")


def _selected_manifest(job: dict, shots: list[dict]) -> list[dict]:
    entries: dict[tuple[str, str], dict] = {}
    for shot in shots:
        if shot["media_kind"] == "presenter":
            continue
        kind = f"shot:{shot['asset_id'] or shot['shot_id']}"
        entry = _manifest_entry(kind, shot["src"])
        entries[(kind, entry["path"])] = entry
    presenter = _manifest_entry("main_audio", job["input_path"])
    entries[("main_audio", presenter["path"])] = presenter
    for item in job["packaging"].get("items", []):
        if item.get("file") and not item.get("is_backup"):
            kind = f"packaging:{item['slot']}:{item.get('asset_ref') or ''}"
            entry = _manifest_entry(kind, item["file"])
            entries[(kind, entry["path"])] = entry
    for item in job["packaging"].get("instances", []):
        if item.get("file"):
            kind = f"packaging-instance:{item['role']}:{item['instanceId']}"
            entry = _manifest_entry(kind, item["file"])
            entries[(kind, entry["path"])] = entry
    appearance_font = job["packaging"].get("appearance_font")
    if appearance_font:
        entry = _manifest_entry("subtitle_appearance_font", appearance_font["file"])
        entries[(entry["kind"], entry["path"])] = entry
    subtitle = job["dependency_snapshot"].get("subtitle") or {}
    for key in ("ass", "font"):
        if subtitle.get(key):
            entry = _manifest_entry(f"subtitle_{key}", subtitle[key]["path"])
            entries[(entry["kind"], entry["path"])] = entry
    if job.get("ass_path") and not subtitle:
        entry = _manifest_entry("subtitle_ass", job["ass_path"])
        entries[(entry["kind"], entry["path"])] = entry
    base_font = (Path(job["project_root"]) / "assets/fonts/SourceHanSerifSC-Heavy.otf")
    if base_font.is_file():
        entry = _manifest_entry("render_font", base_font)
        entries[(entry["kind"], entry["path"])] = entry
    return [entries[key] for key in sorted(entries)]


def _subtitle_cues(job: dict, duration_us: int) -> list[dict]:
    subtitle = job["dependency_snapshot"].get("subtitle") or {}
    metadata = subtitle.get("metadata")
    if metadata is None:
        return []
    path = Path(metadata["path"])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("RECIPE_SUBTITLE_METADATA_INVALID: 字幕元数据无法读取") from error
    cues = value.get("cues")
    if not isinstance(cues, list):
        raise RuntimeError("RECIPE_SUBTITLE_METADATA_INVALID: 字幕元数据缺少 cues")
    result = []
    for index, cue in enumerate(cues):
        start_us, end_us = seconds_to_us(cue["start"]), seconds_to_us(cue["end"])
        if not 0 <= start_us < end_us <= duration_us:
            raise RuntimeError("RECIPE_SUBTITLE_WINDOW_INVALID: 字幕超出成片时间轴")
        result.append({
            "index": index, "text": cue["text"], "start_us": start_us,
            "end_us": end_us, "style": cue.get("style", "Default"),
        })
    return result


def compile_digital_recipe(job: dict, profile: dict) -> dict:
    """Freeze actual scene, main audio, subtitles and packaging before render-mix."""
    cards = job["dependency_snapshot"]["scene_cards"]
    if not cards or job["plan"].get("speed") != 1.25:
        raise RuntimeError("RECIPE_DIGITAL_TIMELINE_INVALID: 缺少锁定镜头或1.25倍速")
    output = {
        "width": 1080, "height": 1920,
        "fps_num": int(profile["fps_num"]), "fps_den": int(profile["fps_den"]),
        "pixel_format": "yuv420p", "audio_rate": int(profile["output_audio_rate"]),
        "video_codec": "libx264", "audio_codec": "aac", "crf": 20,
    }
    if output["fps_num"] <= 0 or output["fps_den"] <= 0 or output["audio_rate"] <= 0:
        raise RuntimeError("RECIPE_DIGITAL_OUTPUT_INVALID: 输出帧率或采样率无效")
    assets = {item["asset_id"]: item for item in job["dependency_snapshot"]["assets"]}
    inserts = iter(job["plan"].get("inserts") or [])
    shots = []
    for index, card in enumerate(cards):
        window = TimeWindow(seconds_to_us(card["narrative_start"]),
                            seconds_to_us(card["narrative_end"]))
        presenter = card["mix_form"] == "数字人全屏"
        insert = None if presenter else next(inserts, None)
        if not presenter and insert is None:
            raise RuntimeError("RECIPE_DIGITAL_SHOT_MISMATCH: 镜头与插入计划数量不一致")
        media_kind = "presenter" if presenter else assets[card["asset_id"]]["media_kind"]
        src = str(Path(job["input_path"] if presenter else insert["src"]).resolve())
        if insert is not None and (
            seconds_to_us(insert["start"]) != window.start_us or
            seconds_to_us(insert["end"]) != window.end_us or
            str(Path(assets[card["asset_id"]]["path"]).resolve()) != src
        ):
            raise RuntimeError("RECIPE_DIGITAL_SHOT_MISMATCH: 插入计划与锁定镜头不一致")
        shot = {
            "index": index, "shot_id": card["card_id"],
            "asset_id": card["asset_id"], "segment_id": card.get("segment_id"),
            "media_kind": media_kind, "mix_form": card["mix_form"], "src": src,
            "output_start_us": window.start_us, "output_end_us": window.end_us,
            "output_start_frame": frame_boundary(
                window.start_us, fps_num=output["fps_num"], fps_den=output["fps_den"]),
            "output_end_frame": frame_boundary(
                window.end_us, fps_num=output["fps_num"], fps_den=output["fps_den"]),
            "render_value": None if presenter else dict(insert),
        }
        if media_kind != "image":
            source_start = (
                window.start_us * _SPEED_NUM // _SPEED_DEN if presenter
                else seconds_to_us(insert["sourceStart"])
            )
            source_end = (
                window.end_us * _SPEED_NUM // _SPEED_DEN if presenter
                else seconds_to_us(insert["sourceEnd"])
            )
            if source_end - source_start < window.duration_us:
                raise RuntimeError("SOURCE_WINDOW_TOO_SHORT: 数字人镜头源窗口不足")
            shot.update(source_start_us=source_start, source_end_us=source_end)
        shots.append(shot)
    if next(inserts, None) is not None:
        raise RuntimeError("RECIPE_DIGITAL_SHOT_MISMATCH: 插入计划多于锁定镜头")
    duration_us = shots[-1]["output_end_us"]
    validate_coverage(
        (TimeWindow(item["output_start_us"], item["output_end_us"]) for item in shots),
        TimeWindow(0, duration_us),
    )
    required_source_us = duration_us * _SPEED_NUM // _SPEED_DEN
    if int(profile["source_duration_us"]) < required_source_us:
        raise RuntimeError("DIGITAL_SOURCE_TOO_SHORT: 口播源音画不足以覆盖1.25倍速成片")
    instances = _packaging_instances(job["plan"], duration_us)
    manifest = _selected_manifest(job, shots)
    gates = job["dependency_snapshot"]["gate_events"]
    task_snapshot = {
        "task_id": job["task_id"], "task_type": "digital_human",
        "aspect_ratio": "9:16",
        "picture_lock_digest": gates["picture_lock"]["gate_digest"],
        "packaging_plan_id": job["packaging"].get("plan_id"),
    }
    dependencies = {
        "task_snapshot": task_snapshot,
        "scene_cards": cards,
        "packaging_snapshot": job["packaging"],
        "subtitle": job["dependency_snapshot"].get("subtitle"),
        "asset_manifest": manifest,
    }
    return {
        "recipe_version": RECIPE_VERSION,
        "audio_policy": audio_mix_policy(),
        "task_snapshot": task_snapshot,
        "duration_us": duration_us,
        "output": output,
        "shots": shots,
        "main_audio": {
            "src": str(Path(job["input_path"]).resolve()),
            "source_start_us": 0, "source_end_us": required_source_us,
            "output_start_us": 0, "output_end_us": duration_us,
            "playback_rate_num": _SPEED_NUM,
            "playback_rate_den": _SPEED_DEN,
            "source_audio_rate": int(profile["source_audio_rate"]),
        },
        "subtitle_cues": _subtitle_cues(job, duration_us),
        "subtitle_style": {
            "preset_id": job["plan"].get("subtitlePresetId"),
            "ass_path": job.get("ass_path"),
        },
        **({"subtitle_appearance": job["plan"]["subtitleAppearance"]} if job["plan"].get("subtitleAppearance") else {}),
        "packaging_instances": instances,
        "transitions": [],
        "asset_manifest": manifest,
        "dependency_digest": canonical_digest(dependencies),
    }


def digital_recipe_content_digest(recipe: dict) -> str:
    """Identify the same locked render across preview and formal job directories."""
    content = copy.deepcopy(recipe)
    content.pop("dependency_digest", None)
    content["subtitle_style"].pop("ass_path", None)
    for item in content["asset_manifest"]:
        if item["kind"] == "subtitle_ass":
            item.pop("path", None)
    return canonical_digest(content)


def digital_plan_from_recipe(recipe: dict, *, preview_output: dict | None = None) -> dict:
    if (recipe.get("recipe_version") != RECIPE_VERSION or
            (recipe.get("task_snapshot") or {}).get("task_type") != "digital_human"):
        raise RuntimeError("RECIPE_VERSION_UNSUPPORTED: 数字人配方版本无效")
    plan = {
        "speed": recipe["main_audio"]["playback_rate_num"] /
                 recipe["main_audio"]["playback_rate_den"],
        "inserts": [dict(shot["render_value"]) for shot in recipe["shots"]
                    if shot["media_kind"] != "presenter"],
        "subtitlePresetId": recipe["subtitle_style"]["preset_id"],
        **({"subtitleAppearance": recipe["subtitle_appearance"]} if recipe.get("subtitle_appearance") else {}),
        "recipe_digest": canonical_digest(recipe),
        "renderRecipeJson": json.dumps(
            recipe, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
    for instance in recipe["packaging_instances"]:
        field = instance["kind"]
        value = dict(instance["render_value"])
        if field in {"bgm", "watermark"}:
            plan[field] = value
        else:
            plan.setdefault(field, []).append(value)
    if preview_output is not None:
        plan["output"] = dict(preview_output)
    return plan
