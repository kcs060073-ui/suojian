"""P1-C 场景片段：把长视频切成"候选镜头"，每段带原片时间码。

用 ffmpeg 场景切换检测（scdet / select=gt(scene)）找切点；若整段无明显切换，
退回"按固定时长切片"，保证总有可挑选的片段。片段时间码指向原片，
后续在代理上选的切点能精准对回原片（P1-E 时间码一致性）。
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import math
import re
import sqlite3
import subprocess

from .path_utils import filesystem_path
from .preview import _asset_source, _duration_of

SCENE_THRESHOLD = 0.4
FALLBACK_CHUNK_SEC = 15.0


def _asset_video_source(db, asset_id: str):
    info = _asset_source(db, asset_id)
    if info is None:
        raise RuntimeError(f"素材当前不可访问：{asset_id}")
    mount_path, relative_path, media_kind = info
    if media_kind != "video":
        raise RuntimeError(f"场景切片仅支持视频，当前为 {media_kind}")
    return filesystem_path(Path(mount_path) / relative_path), _duration_of(db, asset_id)


def _detect_cut_times(ffmpeg: Path, source: Path, runner: Callable[..., object]) -> list[float]:
    completed = runner(
        [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-i",
            str(source),
            "-filter:v",
            f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("SCENE_DETECTION_FAILED: FFmpeg 未完成场景检测")
    cuts = []
    for line in (completed.stderr or "").splitlines():
        match = re.search(r"pts_time:([\d.]+(?:e[-+]?\d+)?)", line)
        if match:
            value = float(match.group(1))
            if math.isfinite(value):
                cuts.append(value)
    return cuts


def detect_segments(
    db, asset_id: str, ffmpeg: Path, runner: Callable[..., object] = subprocess.run
) -> list[dict]:
    source, duration = _asset_video_source(db, asset_id)
    segments = _detect_segments_files(asset_id, source, duration, ffmpeg, runner)
    _persist(db, asset_id, segments)
    return segments


def _detect_segments_files(
    asset_id: str,
    source: Path,
    duration: float | None,
    ffmpeg: Path,
    runner: Callable[..., object],
) -> list[dict]:
    # 纯 ffmpeg 文件 I/O，不碰数据库；可在工作线程内安全调用
    if not duration or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("视频时长未知，无法切分片段")
    cuts = _detect_cut_times(ffmpeg, source, runner)
    candidates = sorted(c for c in cuts if 0 < c < duration)
    accepted = []
    previous = 0.0
    for cut in candidates:
        if cut - previous < 0.3 or duration - cut < 0.3:
            continue
        accepted.append(cut)
        previous = cut
    boundaries = [0.0, *accepted, duration]
    segments = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1]
        spans = []
        cursor = start
        while end - cursor > FALLBACK_CHUNK_SEC:
            spans.append((cursor, cursor + FALLBACK_CHUNK_SEC))
            cursor += FALLBACK_CHUNK_SEC
        spans.append((cursor, end))
        if len(spans) > 1 and spans[-1][1] - spans[-1][0] < 0.3:
            # Avoid inventing an almost empty tail while staying under 15 s.
            count = len(spans)
            spans = [
                (start + (end - start) * index / count,
                 start + (end - start) * (index + 1) / count)
                for index in range(count)
            ]
        for span_start, span_end in spans:
            segments.append({
                "segment_id": f"{asset_id}:{len(segments):04d}",
                "asset_id": asset_id,
                "start_sec": round(span_start, 3),
                "end_sec": round(span_end, 3),
                "label": None,
            })
    return segments


def _persist(db, asset_id: str, segments: list[dict]) -> None:
    import json

    with db.transaction() as connection:
        connection.execute("DELETE FROM segments WHERE asset_id=?", (asset_id,))
        for seg in segments:
            connection.execute(
                """
                INSERT INTO segments(segment_id, asset_id, start_sec, end_sec, label)
                VALUES(?, ?, ?, ?, ?)
                """,
                (seg["segment_id"], asset_id, seg["start_sec"], seg["end_sec"], seg["label"]),
            )
        # 与全管线其它阶段（preview/audio/probe）一致：在 analysis_runs 写 stage 完成标记，
        # 供 _status 指标与统一阶段追踪使用。
        connection.execute(
            """
            INSERT INTO analysis_runs(asset_id, stage, status, result_json, updated_at)
            VALUES(?, 'segments', 'completed', ?, datetime('now'))
            ON CONFLICT(asset_id, stage) DO UPDATE SET
              status='completed', result_json=excluded.result_json, updated_at=datetime('now')
            """,
            (asset_id, json.dumps({"segment_count": len(segments)})),
        )


def segments_pending(
    db,
    library_id: str,
    ffmpeg: Path,
    runner: Callable[..., object] = subprocess.run,
    *,
    workers: int = 1,
    batch_size: int = 16,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> object:
    if not 1 <= workers <= 4:
        raise ValueError("workers 必须在 1 到 4 之间")
    rows = db.connection.execute(
        """
        SELECT a.asset_id, a.media_kind, l.mount_path, al.relative_path
        FROM assets a
        JOIN asset_locations al USING(asset_id)
        JOIN libraries l USING(library_id)
        LEFT JOIN segments s ON s.asset_id=a.asset_id
        WHERE al.library_id=? AND al.state='available' AND l.online=1
          AND a.media_kind='video'
          AND s.asset_id IS NULL
        """,
        (library_id,),
    ).fetchall()
    discovered = len(rows)
    completed_count = failed_count = 0
    for batch in _chunks(list(rows), batch_size):
        # 主线程读取素材信息（sqlite 连接只能在创建它的线程用）
        work: list[tuple] = []
        for row in batch:
            info = _asset_source(db, row["asset_id"])
            if info is None:
                continue
            mount_path, relative_path, media_kind = info
            if media_kind != "video":
                continue
            source = filesystem_path(Path(mount_path) / relative_path)
            duration = _duration_of(db, row["asset_id"])
            work.append((row["asset_id"], source, duration, relative_path))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for (aid, src, dur, rel) in work:
                fut = executor.submit(
                    _detect_segments_files, aid, src, dur, ffmpeg, runner
                )
                futures[fut] = (aid, rel)
            for future in as_completed(futures):
                aid, rel = futures[future]
                try:
                    segments = future.result()
                    _persist(db, aid, segments)  # 主线程写回
                    completed_count += 1
                except (OSError, RuntimeError, subprocess.TimeoutExpired, sqlite3.Error) as error:
                    failed_count += 1
                if on_progress is not None:
                    on_progress(rel, completed_count, failed_count)
    from .models import ScanSummary

    return ScanSummary(discovered=discovered, completed=completed_count, failed=failed_count)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
