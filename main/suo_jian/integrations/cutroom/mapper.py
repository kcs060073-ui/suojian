"""Map a reviewed DraftBlueprint into a path-based Cutroom execution job."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def make_cutroom_job(reference: dict, draft: dict, video_path: str | Path, *, job_id: str, options: dict | None = None) -> dict:
    if not draft.get("timeline"):
        raise ValueError("DraftBlueprint 缺少时间线，不能建立 CutroomJob。")
    source = reference.get("source", {})
    audio = reference.get("audio_analysis", {})
    tts = reference.get("tts_analysis", {})
    options = options or {}
    timeline = []
    for item in draft["timeline"]:
        timing = item.get("time", {})
        timeline.append({
            "slot_id": item.get("slot_id"),
            "start": float(timing.get("start", 0)),
            "duration": float(timing.get("duration", 0)),
            "video_source": item.get("video", {}).get("source_path"),
            "reference_shot_id": item.get("video", {}).get("shot_id"),
            "subtitle": item.get("subtitle", {}),
        })
    return {
        "schema_version": "cutroom-job-0.1",
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "prepared",
        "project": {
            "name": options.get("project_name", f"溯剪-{job_id}"),
            "width": source.get("width"),
            "height": source.get("height"),
            "fps": source.get("fps"),
        },
        "reference": {"video_path": str(Path(video_path).resolve())},
        "timeline": timeline,
        "audio": {
            "tts": {
                "enabled": bool(options.get("tts", True)),
                "text": draft.get("script", {}).get("text", ""),
                "voice_profile": options.get("voice_profile", tts.get("voice_profile")),
                "speed": options.get("tts_speed", tts.get("speed_estimate")),
            },
            "bgm": audio.get("music", {}),
            "sfx_events": audio.get("sfx_events", []),
        },
        "execution_protocol": "cutroom-agent-mcp-v1",
        "requested_capabilities": [name for name in ("generate_tts", "apply_subtitles", "place_sfx", "render_preview") if options.get(name, True)],
        "outputs": {"preview_path": None, "jianying_draft_path": None},
    }


def make_ad_task_request(draft: dict, *, title: str, purpose: str,
                         primary_library_id: str, aspect_ratio: str = "9:16",
                         selected_generic_ids: list[str] | None = None) -> dict:
    """Build Cutroom's actual Agent ad-task input, never a guessed TTS call.

    The library ID must be chosen from Cutroom's own catalog. Preflight is
    required before creation, and all paid actions remain user-gated there.
    """
    if aspect_ratio not in {"9:16", "16:9"}:
        raise ValueError("Cutroom 广告路线只支持 9:16 或 16:9。")
    timeline = draft.get("timeline") or []
    script = draft.get("script", {}).get("text", "").strip()
    if not script:
        script = "".join((item.get("subtitle") or {}).get("text", "") for item in timeline).strip()
    if not script or not timeline:
        raise ValueError("需要先确认有文字脚本和时间线的 DraftBlueprint。")
    if not primary_library_id:
        raise ValueError("需要选择 Cutroom 中有效的任务素材库 ID。")
    duration = max(float((item.get("time") or {}).get("start", 0)) +
                   float((item.get("time") or {}).get("duration", 0)) for item in timeline)
    return {
        "route": "ad_mix", "orchestration_mode": "agent", "title": title,
        "purpose": purpose, "final_script": script, "aspect_ratio": aspect_ratio,
        "voice_mode": "ai_tts", "target_duration_seconds": duration,
        "primary_library_id": primary_library_id,
        "selected_generic_ids": selected_generic_ids or [],
        "include_default_generic": True,
    }


def make_ad_storyboard(draft: dict, locked_script: str, aspect_ratio: str,
                       visual_guidance: list[dict] | None = None) -> dict:
    """Map reviewed slots to Cutroom's ad_storyboard content schema.

    Cutroom requires exact coverage of its *locked* script. If the two scripts
    differ, stop instead of silently losing words or changing the user's copy.
    """
    timeline = draft.get("timeline") or []
    segments = []
    if visual_guidance is not None and len(visual_guidance) != len(timeline):
        raise ValueError("分镜画面指引数量必须与字幕段数一致。")
    for index, item in enumerate(timeline):
        text = (item.get("subtitle") or {}).get("text", "")
        if text:
            guidance = visual_guidance[index] if visual_guidance is not None else {}
            segments.append({"text": text, "type": "video",
                             "query": guidance.get("query", text),
                             "prompt": guidance.get("prompt", text)})
    compact = lambda value: "".join(str(value).split())
    if not segments or compact("".join(s["text"] for s in segments)) != compact(locked_script):
        raise ValueError("溯剪字幕与 Cutroom 已锁定脚本不一致；请先在两边确认同一版文案。")
    return {"plan": {"ratio": aspect_ratio, "segments": segments}}
