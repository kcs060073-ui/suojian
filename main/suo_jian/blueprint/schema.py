from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from suo_jian.script_parser import classify_role


def _style_from_creative(creative_analysis: dict) -> dict:
    source = (creative_analysis or {}).get("subtitle_style", {})
    size = source.get("font_size_px_estimate", 52)
    if isinstance(size, dict):
        size = round((float(size.get("min", 48)) + float(size.get("max", 56))) / 2)
    stroke = source.get("stroke") or {"color": "#111111", "width": source.get("stroke_width_px_estimate", 5)}
    if "width_px_estimate" in stroke and "width" not in stroke:
        stroke = {**stroke, "width": stroke["width_px_estimate"]}
    highlight_source = source.get("highlight") or {}
    return {
        "font_family_candidates": source.get("font_family_candidates", ["思源黑体 Heavy", "阿里巴巴普惠体 Heavy", "剪映默认粗体"]),
        "font_weight": source.get("font_weight", 900),
        "font_size": int(size),
        "fill": source.get("fill", "#FFD800"),
        "stroke": {"color": stroke.get("color", "#111111"), "width": stroke.get("width", 5)},
        "shadow": {"enabled": bool(source.get("shadow", True))},
        "position": {"anchor": "bottom_center", "x": 0.5, "y": 0.66},
        "highlight": {"enabled": highlight_source.get("present", highlight_source.get("enabled", True)), "color": highlight_source.get("candidate_color", highlight_source.get("color", "#F02B21"))},
        "animation": {"type": (source.get("animation_candidates") or ["pop_in"])[0]},
        "confidence": source.get("confidence", 0.55),
    }


def _shot_for_time(shots: list[dict], timestamp: float) -> int | None:
    for shot in shots:
        if float(shot["start"]) <= timestamp < float(shot["end"]):
            return shot["id"]
    return shots[-1]["id"] if shots else None


def _audio_analysis(beats: dict, tts_analysis: dict, creative_analysis: dict, sound_effects: dict) -> dict:
    music = (creative_analysis or {}).get("background_music", {})
    bpm = beats.get("bpm") if beats.get("available") else None
    events = [
        {
            "time": event.get("start"),
            "type": event.get("type"),
            "label": event.get("label"),
            "strength": event.get("features", {}).get("onset_strength"),
            "confidence": event.get("confidence"),
        }
        for event in sound_effects.get("events", [])
    ]
    return {
        "speech": {
            "usable_for_asr": bool(tts_analysis.get("detected")),
            "cleanliness": tts_analysis.get("confidence", 0.0),
            "pace": tts_analysis.get("speed_estimate"),
            "voice_profile": tts_analysis.get("voice_profile"),
            "caveat": "用于字幕、节奏、语速和音色画像；不承诺还原干净人声。",
        },
        "music": {
            "bpm": bpm,
            "mood": music.get("mood", []),
            "search_keywords": music.get("search_keywords", []),
            "ducking_under_voice": "unknown",
            "caveat": "用于节奏、风格和配乐检索；不作为原曲指纹或纯净伴奏声明。",
        },
        "sfx_events": events,
        "strategy": "检测与重建：溯剪标注事件，Cutroom/素材库匹配并重新放置相似音效。",
    }


def enrich_blueprint(blueprint: dict) -> dict:
    """Add normalized styles/edit slots while preserving existing analysis fields."""
    styles = blueprint.setdefault("styles", {})
    styles.setdefault("subtitle_style_01", _style_from_creative(blueprint.get("creative_analysis", {})))
    subtitles = blueprint.setdefault("subtitles", [])
    for index, subtitle in enumerate(subtitles, start=1):
        subtitle.setdefault("id", f"S{index:02d}")
        subtitle.setdefault("style_id", "subtitle_style_01")
        subtitle.setdefault("confidence", blueprint.get("creative_analysis", {}).get("subtitle_style", {}).get("confidence", 0.55))
    slots = []
    for subtitle in subtitles:
        duration = round(float(subtitle["end"]) - float(subtitle["start"]), 3)
        chars = len(subtitle.get("text", ""))
        midpoint = (float(subtitle["start"]) + float(subtitle["end"])) / 2
        slots.append({
            "slot_id": subtitle["id"],
            "time": {"start": float(subtitle["start"]), "end": float(subtitle["end"]), "duration": duration},
            "subtitle": {
                "original_text": subtitle.get("text", ""),
                "preferred_chars": chars,
                "max_chars": max(8, round(chars * 1.65)),
                "style_id": subtitle["style_id"],
            },
            "video": {"shot_id": _shot_for_time(blueprint.get("shots", []), midpoint)},
            "semantic_role": classify_role(subtitle.get("text", "")),
            "rhythm": {"beat_sync": any(abs(float(beat) - float(subtitle["start"])) <= 0.12 for beat in blueprint.get("beats", {}).get("timestamps", []))},
        })
    blueprint["edit_slots"] = slots
    blueprint.setdefault("draft_generation", {"status": "awaiting_user_approval", "approved": False})
    return blueprint


def build_blueprint(metadata, shots, beats, subtitles, tts_analysis, warnings, creative_analysis=None, sound_effects=None, stickers=None, audio_stems=None, speech_transcript=None, subtitle_conflicts=None, onscreen_text=None):
    sound_effects = sound_effects or {"available": False, "events": [], "exact_asset_names": False}
    stickers = stickers or {"available": False, "candidates": [], "exact_asset_names": False}
    audio_stems = audio_stems or {"available": False, "status": "not_requested", "tracks": []}
    speech_transcript = speech_transcript or []
    subtitle_conflicts = subtitle_conflicts or []
    onscreen_text = onscreen_text or [item for item in subtitles if item.get("source") == "ocr"]
    blueprint = {"schema_version": "0.9", "product": "溯剪 / SuoJian", "generated_at": datetime.now(timezone.utc).isoformat(), "source": metadata, "timing": {"duration": metadata["duration"], "fps": metadata["fps"]}, "beats": beats, "shots": shots, "subtitles": subtitles, "speech_transcript": speech_transcript, "onscreen_text": onscreen_text, "subtitle_conflicts": subtitle_conflicts, "tts_analysis": tts_analysis, "creative_analysis": creative_analysis or {}, "sound_effects": sound_effects, "audio_analysis": _audio_analysis(beats, tts_analysis, creative_analysis or {}, sound_effects), "audio_stems": audio_stems, "stickers": stickers, "draft_generation": {"status": "awaiting_user_approval", "approved": False}, "warnings": warnings, "capabilities": {"scene_detection": True, "motion": all(s["motion"]["type"] != "unknown" for s in shots), "beat_analysis": beats["available"], "transcription": bool(speech_transcript), "onscreen_text_ocr": bool(onscreen_text), "tts_matching": tts_analysis.get("available", False), "creative_analysis": bool(creative_analysis), "sound_effect_analysis": sound_effects.get("available", False), "audio_stem_separation": audio_stems.get("available", False), "sticker_analysis": stickers.get("available", False)}}
    return enrich_blueprint(blueprint)


def save_blueprint(blueprint, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination
