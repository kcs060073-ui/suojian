from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess


def detect_scenes(video: Path, duration: float, threshold: float = 27.0) -> tuple[list[dict], list[str]]:
    """Use PySceneDetect when present; return one safe full-length scene otherwise."""
    notes: list[str] = []
    try:
        from scenedetect import detect, ContentDetector
        raw_scenes = detect(str(video), ContentDetector(threshold=threshold))
        scenes = [{"start": round(start.get_seconds(), 3), "end": round(end.get_seconds(), 3)} for start, end in raw_scenes]
        if not scenes:
            scenes = [{"start": 0.0, "end": duration}]
            notes.append("未检测到明显切点，已将整段视频作为一个镜头。")
        return scenes, notes
    except ImportError:
        # Keep the demo useful on a minimal FFmpeg-only installation.  This is a
        # fallback, not a replacement for PySceneDetect's ContentDetector.
        points = []
        if shutil.which("ffmpeg"):
            command = ["ffmpeg", "-hide_banner", "-i", str(video), "-vf", "select='gt(scene,0.28)',showinfo", "-an", "-f", "null", "-"]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            points = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", completed.stderr)]
        starts = [0.0] + [round(x, 3) for x in points if .12 < x < duration - .05]
        boundaries = starts[1:] + [duration]
        notes.append("PySceneDetect 未安装，已使用 FFmpeg scene-score 作为切点回退。")
        return [{"start": start, "end": end} for start, end in zip(starts, boundaries)], notes
    except Exception as exc:
        notes.append(f"切点检测降级：{exc}")
        return [{"start": 0.0, "end": duration}], notes
