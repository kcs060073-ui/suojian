from __future__ import annotations

from suo_jian.analyzer.motion import analyze_motion


def build_shots(video, scene_ranges: list[dict], beat_times: list[float]) -> tuple[list[dict], list[str]]:
    shots, notes = [], []
    for index, scene in enumerate(scene_ranges):
        motion, warning = analyze_motion(video, scene["start"], scene["end"])
        if warning and warning not in notes:
            notes.append(warning)
        boundary = scene["end"]
        nearest = min(beat_times, key=lambda x: abs(x - boundary), default=None)
        beat_sync = nearest is not None and abs(nearest - boundary) <= 0.15
        transition = "hard_cut" if index < len(scene_ranges) - 1 else "end"
        shots.append({
            "id": index + 1, "start": scene["start"], "end": scene["end"],
            "duration": round(scene["end"] - scene["start"], 3),
            "transition_out": transition,
            "motion": motion, "beat_sync": beat_sync,
            "confidence": round(min(0.95, 0.55 + motion["confidence"] * 0.3 + (0.1 if beat_sync else 0)), 2),
        })
    return shots, notes
