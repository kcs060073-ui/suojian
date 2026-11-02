from __future__ import annotations

import subprocess
from pathlib import Path


def add_reference_timeline(script, draft, reference: dict, video_path: Path, output: Path) -> dict:
    video_track = script.append_track(draft.TrackSpec(draft.TrackType.video, "溯剪-视频主轨"))
    stem_tracks = []
    for item in (reference.get("audio_stems", {}).get("tracks", []) if reference.get("audio_stems", {}).get("available") else []):
        stem_path = Path(item.get("path", ""))
        if stem_path.exists() and item.get("kind") in {"voice", "background_music", "sound_effect_candidate"}:
            label = {"voice": "人声", "background_music": "背景音乐", "sound_effect_candidate": "其他音效候选"}[item["kind"]]
            stem_tracks.append((label, stem_path))
    audio_tracks = [script.append_track(draft.TrackSpec(draft.TrackType.audio, f"溯剪-{label}")) for label, _ in stem_tracks]
    fallback_audio_track = None if audio_tracks else script.append_track(draft.TrackSpec(draft.TrackType.audio, "溯剪-原片声音"))
    material = draft.VideoMaterial(str(video_path), reference["source"].get("file_name", "reference.mp4"))
    material_duration_s = material.duration / 1_000_000
    media_dir = output / "media"
    media_dir.mkdir(exist_ok=True)
    audio_path = media_dir / "source_audio.wav"
    has_audio = False
    full_range = draft.trange("0s", f"{material_duration_s:.3f}s")
    for (label, stem_path), audio_track in zip(stem_tracks, audio_tracks):
        try:
            audio_material = draft.AudioMaterial(str(stem_path), f"溯剪-{label}")
            script.add_segment(draft.AudioSegment(audio_material, full_range, source_timerange=full_range), audio_track)
            has_audio = True
        except (OSError, ValueError, RuntimeError):
            pass
    try:
        if fallback_audio_track is not None:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video_path), "-vn", "-ac", "2", "-ar", "44100", str(audio_path)], check=True)
            audio_material = draft.AudioMaterial(str(audio_path), "溯剪-原片声音")
            script.add_segment(draft.AudioSegment(audio_material, full_range, source_timerange=full_range), fallback_audio_track)
            has_audio = True
    except (OSError, subprocess.SubprocessError):
        pass
    for shot in reference.get("shots", []):
        clip_duration = max(0.0, min(float(shot["duration"]), material_duration_s - float(shot["start"])))
        if clip_duration <= 0:
            continue
        target = draft.trange(f"{float(shot['start']):.3f}s", f"{clip_duration:.3f}s")
        source_range = draft.trange(f"{float(shot['start']):.3f}s", f"{clip_duration:.3f}s")
        script.add_segment(draft.VideoSegment(material, target, source_timerange=source_range, volume=0.0), video_track)
    return {"video_segments": len(reference.get("shots", [])), "audio_tracks": len(audio_tracks) if has_audio and audio_tracks else int(has_audio), "audio_track_names": [label for label, _ in stem_tracks] if has_audio and audio_tracks else (["原片声音"] if has_audio else [])}
