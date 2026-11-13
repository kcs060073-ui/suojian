"""Convert a completed 剪辑工作台 editable delivery into a Jianying draft.

The published video or base video is a single editable clip. Audio stems and
subtitle text are separate tracks when 剪辑工作台 actually delivered them.
Effects baked into video cannot be reconstructed as native Jianying effects.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from .subtitle_builder import add_subtitles


def _component_file(package: Path, components: dict, name: str) -> Path | None:
    item = components.get(name) or {}
    if item.get("status") != "included" or not item.get("path"):
        return None
    candidate = (package / item["path"]).resolve()
    if package.resolve() not in candidate.parents or not candidate.is_file():
        raise ValueError(f"剪辑工作台 交付文件无效：{name}")
    return candidate


def _ass_time(value: str) -> float:
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _subtitle_cues(path: Path) -> list[dict]:
    cues = []
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".ass":
        for line in content.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            fields = line.partition(":")[2].lstrip().split(",", 9)
            if len(fields) != 10:
                continue
            start, end = _ass_time(fields[1]), _ass_time(fields[2])
            text = re.sub(r"\{[^}]*\}", "", fields[9]).replace(r"\N", "\n").strip()
            if text and end > start:
                cues.append({"time": {"start": start, "duration": end - start},
                             "subtitle": {"text": text, "style_id": "subtitle_style_01"}})
    elif path.suffix.lower() == ".srt":
        for block in re.split(r"\n\s*\n", content.replace("\r\n", "\n")):
            lines = block.strip().splitlines()
            timing = next((line for line in lines if " --> " in line), None)
            if not timing:
                continue
            left, right = timing.split(" --> ", 1)
            start = _ass_time(left.replace(",", "."))
            end = _ass_time(right.replace(",", "."))
            text = "\n".join(lines[lines.index(timing) + 1:]).strip()
            if text and end > start:
                cues.append({"time": {"start": start, "duration": end - start},
                             "subtitle": {"text": text, "style_id": "subtitle_style_01"}})
    return cues


def build_from_cutroom_delivery(package_dir: str | Path, reference: dict,
                                draft_blueprint: dict, draft_root: str | Path,
                                draft_name: str) -> tuple[Path, dict]:
    import pyJianYingDraft as draft

    package = Path(package_dir).resolve()
    manifest_path = package / "delivery-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "cutroom.editable-delivery/v1":
        raise ValueError("剪辑工作台 交付包格式不受支持。")
    components = manifest.get("components") or {}
    video = _component_file(package, components, "base_video")
    video_kind = "base_video"
    if video is None:
        video = _component_file(package, components, "final_video")
        video_kind = "final_video"
    if video is None:
        raise ValueError("剪辑工作台 交付包缺少可用视频。")

    material = draft.VideoMaterial(str(video), video.name)
    audio_sources = [(key, label, _component_file(package, components, key)) for key, label in (
        ("voice_or_original_audio", "人声"), ("bgm_stem", "背景音乐"), ("sfx_stem", "音效"))]
    has_stems = any(path is not None for _, _, path in audio_sources)
    target_root = Path(draft_root)
    target_root.mkdir(parents=True, exist_ok=True)
    source = reference["source"]
    script = draft.DraftFolder(str(target_root)).create_draft(
        draft_name, int(material.width), int(material.height),
        int(round(source["fps"])), allow_replace=False)
    output = target_root / draft_name
    duration = material.duration / 1_000_000
    time_range = draft.trange("0s", f"{duration:.3f}s")
    video_track = script.append_track(draft.TrackSpec(draft.TrackType.video, "剪辑工作台-视频"))
    script.add_segment(draft.VideoSegment(material, time_range,
                                         source_timerange=time_range,
                                         volume=0.0 if has_stems else 1.0), video_track)

    audio_tracks = []
    for key, label, path in audio_sources:
        if path is None:
            continue
        audio = draft.AudioMaterial(str(path), f"剪辑工作台-{label}")
        track = script.append_track(draft.TrackSpec(draft.TrackType.audio, f"剪辑工作台-{label}"))
        length = min(duration, audio.duration / 1_000_000)
        span = draft.trange("0s", f"{length:.3f}s")
        script.add_segment(draft.AudioSegment(audio, span, source_timerange=span), track)
        audio_tracks.append(label)

    subtitle_path = _component_file(package, components, "subtitles")
    cues = _subtitle_cues(subtitle_path) if subtitle_path and video_kind == "base_video" else []
    subtitle_count = add_subtitles(script, draft, {
        "timeline": cues, "styles": draft_blueprint.get("styles", {}),
    }) if cues else 0
    script.save()
    report = {
        "generator": "溯剪 × 剪辑工作台", "cutroom_package": str(package),
        "video_source": video_kind, "editable_video_clips": 1,
        "editable_audio_tracks": audio_tracks, "editable_subtitles": subtitle_count,
        "baked_effects_remain_editable": False,
        "original_video_audio_muted": has_stems,
        "subtitle_track_skipped_on_final_video": bool(subtitle_path and video_kind == "final_video"),
    }
    (output / "suojian_cutroom_manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, report
