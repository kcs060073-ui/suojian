from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def read_metadata(video: Path) -> dict:
    """Read dependable video properties through ffprobe (no Python codec dependency)."""
    if not shutil.which("ffprobe"):
        raise RuntimeError("未找到 ffprobe。请安装 FFmpeg 并将其加入 PATH。")
    command = ["ffprobe", "-v", "error", "-show_entries",
               "format=duration:format_tags=creation_time:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate",
               "-of", "json", str(video)]
    completed = subprocess.run(command, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
    raw = json.loads(completed.stdout)
    streams = raw.get("streams", [])
    visual = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    rate = visual.get("avg_frame_rate") or visual.get("r_frame_rate") or "0/1"
    numerator, denominator = rate.split("/")
    fps = round(float(numerator) / max(float(denominator), 1), 3)
    return {
        "file_name": video.name,
        "duration": round(float(raw.get("format", {}).get("duration", 0)), 3),
        "width": visual.get("width"), "height": visual.get("height"), "fps": fps,
        "video_codec": visual.get("codec_name"), "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name") if audio else None,
    }
