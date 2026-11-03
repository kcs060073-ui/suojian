"""Probe the real presenter streams before compiling a digital RenderRecipe."""

from __future__ import annotations

from fractions import Fraction
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import subprocess

from .timeline_contract import seconds_to_us
from .audio_mix_policy import audio_mix_policy
from .tools import default_ffprobe_search_roots, find_ffprobe


# Full-track audio and all preview/final encoders share one explicit format.
DIGITAL_OUTPUT_AUDIO_RATE = audio_mix_policy()["sampleRate"]


def _rate(stream: dict) -> Fraction | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            value = Fraction(str(stream.get(key, "0/0")))
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return value
    return None


def _duration(value) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def probe_digital_render_profile(
    source: str | Path, *, ffprobe_path: str | Path | None = None,
) -> dict:
    """Return source-derived rational FPS and the current digital audio format."""
    path = Path(source).resolve()
    if not path.is_file():
        raise RuntimeError(f"DIGITAL_SOURCE_MISSING: 口播源文件不存在：{path}")
    binary = Path(ffprobe_path) if ffprobe_path else find_ffprobe(
        os.environ, search_roots=default_ffprobe_search_roots())
    command = [
        str(binary), "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,avg_frame_rate,r_frame_rate,sample_rate,"
        "duration:stream_disposition=attached_pic", "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("DIGITAL_MEDIA_PROBE_FAILED: 口播媒体探测无法完成") from error
    if result.returncode != 0:
        raise RuntimeError("DIGITAL_MEDIA_PROBE_FAILED: 口播媒体探测失败")
    try:
        detail = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("DIGITAL_MEDIA_PROBE_FAILED: ffprobe 返回无效 JSON") from error
    streams = detail.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError("DIGITAL_MEDIA_PROBE_FAILED: ffprobe 缺少媒体流")
    videos = [entry for entry in streams
              if entry.get("codec_type") == "video"
              and (entry.get("disposition") or {}).get("attached_pic", 0) in (0, "0", False)]
    if not videos:
        raise RuntimeError("DIGITAL_VIDEO_STREAM_MISSING: 口播源缺少连续视频流")
    audios = [entry for entry in streams if entry.get("codec_type") == "audio"]
    if not audios:
        raise RuntimeError("DIGITAL_AUDIO_STREAM_MISSING: 口播源缺少音轨")
    rate = _rate(videos[0])
    if rate is None:
        raise RuntimeError("DIGITAL_FRAME_RATE_MISSING: 视频没有有效有理数帧率")
    try:
        audio_rate = int(audios[0].get("sample_rate"))
    except (TypeError, ValueError) as error:
        raise RuntimeError("DIGITAL_AUDIO_RATE_MISSING: 原音轨采样率无效") from error
    if audio_rate <= 0:
        raise RuntimeError("DIGITAL_AUDIO_RATE_MISSING: 原音轨采样率无效")
    format_duration = _duration((detail.get("format") or {}).get("duration"))
    durations = [_duration(videos[0].get("duration")),
                 _duration(audios[0].get("duration"))]
    available = [value for value in durations if value is not None]
    source_duration = min(available) if available else format_duration
    if source_duration is None:
        raise RuntimeError("DIGITAL_DURATION_MISSING: 口播源缺少可用时长")
    return {
        "fps_num": rate.numerator,
        "fps_den": rate.denominator,
        "source_audio_rate": audio_rate,
        "output_audio_rate": DIGITAL_OUTPUT_AUDIO_RATE,
        "source_duration_us": seconds_to_us(source_duration),
    }


@lru_cache(maxsize=32)
def _duration_profile(path: str, size: int, sha256: str) -> dict:
    # Display-only probe reuse, bound to full bytes, never a render/QA cache.
    from .snapshots import file_content_signature
    result = probe_digital_render_profile(path)
    if file_content_signature(path) != {"size_bytes": size, "sha256": sha256}:
        raise RuntimeError("DIGITAL_SOURCE_CHANGED: 探测期间口播文件已变化")
    return result


def current_digital_duration(task: dict) -> float | None:
    """Project only the currently bound, readable presenter at render speed."""
    if task.get("task_type") != "digital_human":
        return None
    brief = task.get("brief")
    source = brief.get("digital_human_source") if isinstance(brief, dict) else None
    if not isinstance(source, dict) or not isinstance(source.get("path"), str):
        return None
    try:
        from .digital_workflow import _default_file_snapshot
        from .digital_render_recipe import _SPEED_DEN, _SPEED_NUM
        path = Path(source["path"])
        identity_keys = ("path", "size_bytes", "mtime_ns", "quick_fingerprint", "file_identity")
        before = _default_file_snapshot(path)
        if any(source.get(key) != before.get(key) for key in identity_keys):
            return None
        from .snapshots import file_content_signature
        signature = file_content_signature(path)
        profile = _duration_profile(str(path.resolve()), signature["size_bytes"], signature["sha256"])
        after = _default_file_snapshot(path)
        if any(source.get(key) != after.get(key) for key in identity_keys):
            return None
        return profile["source_duration_us"] / 1_000_000 * _SPEED_DEN / _SPEED_NUM
    except (OSError, RuntimeError, ValueError, KeyError):
        return None
