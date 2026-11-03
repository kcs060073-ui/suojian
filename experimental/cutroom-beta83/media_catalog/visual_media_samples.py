"""Prepare small, evidence-timed visual samples from current catalog media.

This module never contacts a provider. A separate authorized batch must decide
whether these in-memory bytes may leave the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PureWindowsPath
import json
import os
import subprocess
import tempfile

from PIL import Image, ImageOps

from .fingerprint import quick_fingerprint


MAX_EDGE = 640
MAX_JPEG_BYTES = 1_000_000
SAMPLING_VERSION = "visual-frames-v1"


@dataclass(frozen=True)
class VisualFrame:
    timestamp_us: int
    jpeg_bytes: bytes
    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class VisualSample:
    asset_id: str
    asset_fingerprint: str
    media_kind: str
    source_window_us: tuple[int, int]
    frames: tuple[VisualFrame, ...]
    sampling_version: str = SAMPLING_VERSION


def _source(db, asset_id: str, library_id: str,
            relative_path: str) -> tuple[Path, str, str]:
    rows = db.connection.execute(
        """SELECT a.media_kind,a.quick_fingerprint,l.mount_path,l.online,
                  al.relative_path,al.state
           FROM assets a
           JOIN asset_locations al USING(asset_id)
           JOIN libraries l USING(library_id)
           WHERE a.asset_id=? AND l.library_id=? AND al.relative_path=?
           ORDER BY CASE WHEN l.online=1 AND al.state='available' THEN 0 ELSE 1 END,
                    al.location_id""",
        (asset_id, library_id, relative_path),
    ).fetchall()
    if not rows:
        raise ValueError("ASSET_NOT_FOUND: 素材没有可用的目录记录")
    row = rows[0]
    if not row["online"] or row["state"] != "available":
        raise ValueError("ASSET_OFFLINE: 素材目录当前不可访问")
    raw = str(row["relative_path"] or "").replace("\\", "/")
    parts = raw.split("/")
    if not raw or raw.startswith("/") or PureWindowsPath(raw).drive or any(
        part in {"", ".", ".."} for part in parts
    ):
        raise ValueError("ASSET_PATH_INVALID: 素材路径不是目录内相对路径")
    try:
        root = Path(row["mount_path"]).resolve(strict=True)
        source = root.joinpath(*parts).resolve(strict=True)
    except OSError as error:
        raise ValueError("ASSET_OFFLINE: 素材目录或文件当前不可访问") from error
    try:
        source.relative_to(root)
    except ValueError as error:
        raise ValueError("ASSET_PATH_INVALID: 素材路径超出登记目录") from error
    try:
        current_fingerprint = quick_fingerprint(source) if source.is_file() else None
    except OSError as error:
        raise ValueError("ASSET_OFFLINE: 素材文件当前不可读取") from error
    if current_fingerprint != row["quick_fingerprint"]:
        raise ValueError("ASSET_STALE: 源文件已变化，请先重扫素材库")
    return source, row["media_kind"], row["quick_fingerprint"]


def _duration_us(source: Path, ffmpeg: Path) -> int:
    ffprobe = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    completed = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(source)],
        capture_output=True, timeout=30, check=False,
    )
    if completed.returncode:
        raise ValueError("MEDIA_PROBE_FAILED: 无法核对视频长度")
    try:
        duration = float(json.loads(completed.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("MEDIA_PROBE_FAILED: 视频长度不可用") from error
    if not 0 < duration < float("inf"):
        raise ValueError("MEDIA_PROBE_FAILED: 视频长度无效")
    return round(duration * 1_000_000)


def _frame(timestamp_us: int, data: bytes) -> VisualFrame:
    if not data or len(data) > MAX_JPEG_BYTES:
        raise ValueError("VISUAL_SAMPLE_INVALID: 取样帧为空或超出大小限制")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "JPEG":
                raise ValueError("帧不是 JPEG")
            width, height = image.size
            image.verify()
    except (OSError, ValueError) as error:
        raise ValueError("VISUAL_SAMPLE_INVALID: 取样帧不可解码") from error
    if not 0 < width <= MAX_EDGE or not 0 < height <= MAX_EDGE:
        raise ValueError("VISUAL_SAMPLE_INVALID: 取样帧尺寸不受控")
    return VisualFrame(timestamp_us, data, sha256(data).hexdigest(), width, height)


def _video_frame(source: Path, timestamp_us: int, ffmpeg: Path) -> VisualFrame:
    completed = subprocess.run(
        [str(ffmpeg), "-nostdin", "-v", "error", "-ss",
         f"{timestamp_us / 1_000_000:.6f}", "-i", str(source),
         "-frames:v", "1", "-vf",
         f"scale={MAX_EDGE}:{MAX_EDGE}:force_original_aspect_ratio=decrease",
         "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
        capture_output=True, timeout=30, check=False,
    )
    if completed.returncode:
        raise ValueError("VISUAL_SAMPLE_FAILED: 视频取帧失败")
    return _frame(timestamp_us, completed.stdout)



def _video_frames(source: Path, timestamps_us: tuple[int, ...], ffmpeg: Path) -> tuple[VisualFrame, ...]:
    """Keep independent seek semantics, with one bounded process for dense frames."""
    if not 1 <= len(timestamps_us) <= 16 or any(
        type(timestamp) is not int or timestamp < 0 for timestamp in timestamps_us
    ):
        raise ValueError("VISUAL_SAMPLE_INVALID: 视频取样数量或时码无效")
    # Automatic stream selection in the original single-frame command may choose
    # a later/high-resolution stream. Never silently replace that with v:0.
    ffprobe = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    try:
        probe = subprocess.run(
            [str(ffprobe), "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=index", "-of", "json", str(source)],
            capture_output=True, timeout=30, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        streams = json.loads(probe.stdout)["streams"] if not probe.returncode else []
        if not isinstance(streams, list) or not streams:
            raise ValueError("VISUAL_SAMPLE_FAILED: 无法核对视频轨道")
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("VISUAL_SAMPLE_FAILED: 无法核对视频轨道") from error
    if len(streams) != 1:
        return tuple(_video_frame(source, timestamp, ffmpeg) for timestamp in timestamps_us)

    with tempfile.TemporaryDirectory(prefix="cutroom-visual-") as directory:
        paths = [Path(directory) / f"{index}.jpg" for index in range(len(timestamps_us))]
        command = [str(ffmpeg), "-nostdin", "-v", "error", "-y", "-filter_threads", "1"]
        for timestamp in timestamps_us:
            command.extend(["-threads", "1", "-ss", f"{timestamp / 1_000_000:.6f}",
                            "-i", str(source)])
        for index, path in enumerate(paths):
            command.extend([
                "-map", f"{index}:v:0", "-frames:v", "1", "-vf",
                f"scale={MAX_EDGE}:{MAX_EDGE}:force_original_aspect_ratio=decrease",
                "-threads", "1", "-q:v", "5", "-f", "image2", "-vcodec", "mjpeg", str(path),
            ])
        try:
            completed = subprocess.run(
                command, capture_output=True, timeout=60, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if completed.returncode:
                raise ValueError("VISUAL_SAMPLE_FAILED: 视频批量取帧失败")
            frames = []
            for timestamp, path in zip(timestamps_us, paths):
                with path.open("rb") as handle:
                    data = handle.read(MAX_JPEG_BYTES + 1)
                frames.append(_frame(timestamp, data))
            return tuple(frames)
        except subprocess.TimeoutExpired as error:
            raise ValueError("VISUAL_SAMPLE_FAILED: 视频批量取帧超时") from error
        except OSError as error:
            raise ValueError("VISUAL_SAMPLE_FAILED: 视频取样帧不完整或不可读") from error


def _image_frame(source: Path) -> VisualFrame:
    try:
        with Image.open(source) as image:
            if image.width * image.height > 50_000_000:
                raise ValueError("VISUAL_SAMPLE_INVALID: 照片尺寸过大")
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            normalized.save(buffer, format="JPEG", quality=85)
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("VISUAL_SAMPLE_INVALID: 照片不可解码") from error
    return _frame(0, buffer.getvalue())


def prepare_visual_sample(db, asset_id: str, source_start_us: int,
                          source_end_us: int, ffmpeg: Path, *,
                          library_id: str, relative_path: str, dense: bool = False) -> VisualSample:
    """Return only bounded local JPEGs from an unchanged, online catalog file."""
    if any(type(value) is not int for value in (source_start_us, source_end_us)) or \
            source_start_us < 0 or source_end_us < source_start_us:
        raise ValueError("SOURCE_WINDOW_INVALID: 源区间需要非负整数微秒")
    source, kind, fingerprint = _source(db, asset_id, library_id, relative_path)
    if kind == "image":
        if (source_start_us, source_end_us) != (0, 0):
            raise ValueError("SOURCE_WINDOW_INVALID: 照片没有源视频时码")
        frames = (_image_frame(source),)
    elif kind == "video":
        duration = _duration_us(source, Path(ffmpeg))
        window = source_end_us - source_start_us
        if window <= 0 or source_end_us > duration:
            raise ValueError("SOURCE_WINDOW_INVALID: 源区间超出视频长度")
        times = tuple(source_start_us + (window * numerator + 15) // 30
                      for numerator in (3, 15, 27))
        if dense:
            from .visual_content_segments import content_frame_times
            times = tuple(content_frame_times(source_start_us, source_end_us))
        if len(set(times)) < 2:
            raise ValueError("SOURCE_WINDOW_INVALID: 源区间短到无法取三帧")
        frames = _video_frames(source, times, Path(ffmpeg)) if dense else tuple(
            _video_frame(source, time, Path(ffmpeg)) for time in times)
    else:
        raise ValueError("MEDIA_KIND_INVALID: 只分析图片和视频")
    from .visual_content_segments import CONTENT_SAMPLING_VERSION
    return VisualSample(asset_id, fingerprint, kind,
                        (source_start_us, source_end_us), frames,
                        CONTENT_SAMPLING_VERSION if dense and kind == "video" else SAMPLING_VERSION)
