"""Versioned, source-timed scene suggestions; no provider calls or source edits."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from .segments import SCENE_THRESHOLD, _detect_cut_times
from .visual_media_samples import _duration_us, _source


METHOD_VERSION = f"ffmpeg-scene-gt{SCENE_THRESHOLD}-bounded15-v1"
MAX_WINDOW_US = 15_000_000
MIN_CUT_GAP_US = 300_000


def _candidate_windows(duration_us: int, cuts: list[float]) -> list[dict]:
    if type(duration_us) is not int or duration_us <= 0:
        raise ValueError("MEDIA_PROBE_FAILED: 视频时长无效")
    candidates = sorted({round(cut * 1_000_000) for cut in cuts
                         if math.isfinite(cut) and 0 < cut * 1_000_000 < duration_us})
    boundaries = [0]
    for cut in candidates:
        if cut - boundaries[-1] >= MIN_CUT_GAP_US and \
                duration_us - cut >= MIN_CUT_GAP_US:
            boundaries.append(cut)
    boundaries.append(duration_us)
    windows = []
    has_detected_cut = len(boundaries) > 2
    for start, end in zip(boundaries, boundaries[1:]):
        spans = []
        cursor = start
        while end - cursor > MAX_WINDOW_US:
            spans.append((cursor, cursor + MAX_WINDOW_US))
            cursor += MAX_WINDOW_US
        spans.append((cursor, end))
        if len(spans) > 1 and spans[-1][1] - spans[-1][0] < MIN_CUT_GAP_US:
            count = len(spans)
            spans = [(start + (end - start) * index // count,
                      start + (end - start) * (index + 1) // count)
                     for index in range(count)]
        origin = "detected_cut" if has_detected_cut and len(spans) == 1 \
            else "bounded_fallback"
        windows.extend({"sourceStartUs": left, "sourceEndUs": right,
                        "origin": origin} for left, right in spans)
    return windows


def scene_windows_for_asset(db, asset_id: str, library_id: str,
                            relative_path: str) -> dict:
    """Return only a current-fingerprint cached set for the exact location."""
    _path, kind, fingerprint = _source(db, asset_id, library_id, relative_path)
    if kind == "image":
        return {"status": "ready", "durationUs": 0, "methodVersion": "still-image-v1",
                "items": [{"assetId": asset_id, "libraryId": library_id,
                           "relativePath": relative_path, "sourceStartUs": 0,
                           "sourceEndUs": 0, "origin": "still_image"}]}
    if kind != "video":
        raise ValueError("MEDIA_KIND_INVALID: 仅视频或图片可做画面分析")
    current = db.connection.execute(
        "SELECT * FROM scene_candidate_sets WHERE asset_id=?", (asset_id,),
    ).fetchone()
    if current is None or current["asset_fingerprint"] != fingerprint or \
            current["method_version"] != METHOD_VERSION:
        return {"status": "needs_detection", "durationUs": None,
                "methodVersion": METHOD_VERSION, "items": []}
    items = [{"assetId": asset_id, "libraryId": library_id,
              "relativePath": relative_path,
              "sourceStartUs": row["source_start_us"],
              "sourceEndUs": row["source_end_us"], "origin": row["origin"]}
             for row in db.connection.execute(
                 "SELECT * FROM scene_candidate_windows WHERE asset_id=? ORDER BY item_index",
                 (asset_id,),
             ).fetchall()]
    if not items:
        return {"status": "needs_detection", "durationUs": None,
                "methodVersion": METHOD_VERSION, "items": []}
    return {"status": "ready", "durationUs": current["duration_us"],
            "methodVersion": METHOD_VERSION, "items": items}


def detect_and_store_scene_windows(db, asset_id: str, library_id: str,
                                   relative_path: str, ffmpeg: Path,
                                   runner=subprocess.run) -> dict:
    """Detect from the current source and atomically replace only suggestions."""
    source, kind, fingerprint = _source(db, asset_id, library_id, relative_path)
    if kind == "image":
        return scene_windows_for_asset(db, asset_id, library_id, relative_path)
    if kind != "video":
        raise ValueError("MEDIA_KIND_INVALID: 仅视频或图片可做画面分析")
    duration_us = _duration_us(source, ffmpeg)
    cuts = _detect_cut_times(ffmpeg, source, runner)
    windows = _candidate_windows(duration_us, cuts)
    _path, _kind, current_fingerprint = _source(db, asset_id, library_id,
                                                relative_path)
    if current_fingerprint != fingerprint:
        raise ValueError("ASSET_STALE: 场景检测期间源素材内容变化")
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT quick_fingerprint FROM assets WHERE asset_id=?", (asset_id,),
        ).fetchone()
        if row is None or row["quick_fingerprint"] != fingerprint:
            raise ValueError("ASSET_STALE: 场景检测期间台账指纹变化")
        connection.execute("DELETE FROM scene_candidate_sets WHERE asset_id=?",
                           (asset_id,))
        connection.execute(
            """INSERT INTO scene_candidate_sets(asset_id,asset_fingerprint,
                 method_version,duration_us,source_library_id,source_relative_path,
                 detected_cut_count) VALUES(?,?,?,?,?,?,?)""",
            (asset_id, fingerprint, METHOD_VERSION, duration_us, library_id,
             relative_path, max(0, len(cuts))),
        )
        connection.executemany(
            """INSERT INTO scene_candidate_windows(asset_id,item_index,
                 source_start_us,source_end_us,origin) VALUES(?,?,?,?,?)""",
            [(asset_id, index, item["sourceStartUs"], item["sourceEndUs"],
              item["origin"]) for index, item in enumerate(windows)],
        )
    return scene_windows_for_asset(db, asset_id, library_id, relative_path)
