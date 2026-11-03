"""Read-only, task-scoped preview before any paid visual-analysis batch."""

from __future__ import annotations

import hashlib
import json

from .model_profiles import _profile_row
from .task_sources import scoped_asset_location
from .visual_capabilities import require_verified_visual_profile, visual_profile_digest
from .visual_media_samples import SAMPLING_VERSION, _source
from .visual_content_segments import CONTENT_SAMPLING_VERSION, content_frame_times


MAX_BATCH_ITEMS = 2000
RESULT_SCHEMA_VERSION = "visual-content-v2.1"


def analysis_method_version(profile_digest: str) -> str:
    source = f"{RESULT_SCHEMA_VERSION}:{CONTENT_SAMPLING_VERSION}:{profile_digest}"
    return "visual-content-v2-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def _duration_us(db, asset_id: str) -> int | None:
    from .scene_window_candidates import METHOD_VERSION

    current = db.connection.execute(
        """SELECT sc.duration_us FROM scene_candidate_sets sc
           JOIN assets a USING(asset_id)
           WHERE sc.asset_id=? AND sc.asset_fingerprint=a.quick_fingerprint
             AND sc.method_version=?""",
        (asset_id, METHOD_VERSION),
    ).fetchone()
    if current is not None:
        return current["duration_us"]
    row = db.connection.execute(
        "SELECT result_json FROM analysis_runs WHERE asset_id=? "
        "AND stage='technical_probe' AND status='completed'",
        (asset_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["result_json"])
        value = data.get("duration_seconds")
        if value is None:
            value = data["format"]["duration"]
        seconds = float(value)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return round(seconds * 1_000_000) if 0 < seconds < float("inf") else None


def _privacy_pending(db, asset_id: str, start_us: int, end_us: int) -> bool:
    if start_us == end_us:
        row = db.connection.execute(
            "SELECT 1 FROM privacy_flags WHERE asset_id=? AND "
            "(needs_human_confirm=1 OR confirmed=1) LIMIT 1",
            (asset_id,),
        ).fetchone()
    else:
        row = db.connection.execute(
            """SELECT 1 FROM privacy_flags WHERE asset_id=?
               AND (needs_human_confirm=1 OR confirmed=1)
               AND (start_sec IS NULL OR start_sec*1000000<?)
               AND (end_sec IS NULL OR end_sec*1000000>?) LIMIT 1""",
            (asset_id, end_us, start_us),
        ).fetchone()
    return row is not None


def _has_matching_observation(db, asset_id: str, fingerprint: str,
                              start_us: int, end_us: int,
                              method_version: str) -> bool:
    rows = db.connection.execute(
        "SELECT source_start_us,source_end_us FROM visual_observations WHERE asset_id=? "
        "AND asset_fingerprint=? AND origin='model' AND method_version=? "
        "AND source_start_us>=? AND source_end_us<=? ORDER BY source_start_us,source_end_us",
        (asset_id,fingerprint,method_version,start_us,end_us)).fetchall()
    if start_us == end_us:
        return bool(rows)
    cursor=start_us
    for left,right in rows:
        if left > cursor: return False
        cursor=max(cursor,right)
    return cursor >= end_us



def preview_visual_batch(db, task_id: str | None, profile_id: str,
                         selections: list[dict], *, library_id: str | None = None) -> dict:
    """Report real scope, reusable work and cost status; never call a provider."""
    if task_id is None and (not isinstance(library_id, str) or not library_id):
        raise ValueError("ASSET_SCOPE_INVALID: 库级分析需要明确的素材库 ID")
    if task_id is not None and library_id is not None:
        raise ValueError("ASSET_SCOPE_INVALID: 任务范围与库范围不能混用")
    profile = require_verified_visual_profile(db, profile_id)
    if type(selections) is not list or not 1 <= len(selections) <= MAX_BATCH_ITEMS:
        raise ValueError("VISUAL_BATCH_INVALID: 请选择1至2000个素材源区间")
    row = _profile_row(db, profile_id)
    profile_digest = visual_profile_digest(row)
    method_version = analysis_method_version(profile_digest)
    items = []
    digest_items = []
    seen = set()
    for selection in selections:
        if type(selection) is not dict:
            raise ValueError("VISUAL_BATCH_INVALID: 素材区间格式无效")
        asset_id = selection.get("assetId")
        selected_library_id = selection.get("libraryId")
        start_us = selection.get("sourceStartUs")
        end_us = selection.get("sourceEndUs")
        if not isinstance(asset_id, str) or not asset_id or \
                not isinstance(selected_library_id, str) or not selected_library_id:
            raise ValueError("VISUAL_BATCH_INVALID: 缺少素材或素材库")
        if type(start_us) is not int or type(end_us) is not int or \
                start_us < 0 or end_us < start_us:
            raise ValueError("SOURCE_WINDOW_INVALID: 源区间必须是非负整数微秒")
        identity = (asset_id, selected_library_id, start_us, end_us)
        if identity in seen:
            raise ValueError("VISUAL_BATCH_DUPLICATE: 同一素材区间重复选择")
        seen.add(identity)
        if task_id is not None:
            scoped = scoped_asset_location(db, task_id, asset_id)
            if scoped is None or scoped["library_id"] != selected_library_id:
                raise ValueError("ASSET_SCOPE_INVALID: 素材不属于本任务选定范围")
            relative_path = scoped["relative_path"]
        else:
            relative_path = selection.get("relativePath")
            if library_id != selected_library_id or \
                    not isinstance(relative_path, str) or not relative_path or \
                    db.connection.execute(
                        """SELECT 1 FROM asset_locations al JOIN libraries l USING(library_id)
                           WHERE al.asset_id=? AND al.library_id=? AND al.relative_path=?
                             AND al.state='available' AND l.online=1 LIMIT 1""",
                        (asset_id, selected_library_id, relative_path),
                    ).fetchone() is None:
                raise ValueError("ASSET_SCOPE_INVALID: 素材不在明确选择的库和路径中")
        _path, kind, fingerprint = _source(
            db, asset_id, selected_library_id, relative_path,
        )
        if kind == "image":
            if (start_us, end_us) != (0, 0):
                raise ValueError("SOURCE_WINDOW_INVALID: 图片没有源时码")
            frame_count = 1
        elif kind == "video":
            duration_us = _duration_us(db, asset_id)
            if duration_us is None:
                raise ValueError("MEDIA_PROBE_REQUIRED: 请先完成视频技术探测")
            if end_us <= start_us or end_us > duration_us:
                raise ValueError("SOURCE_WINDOW_INVALID: 源区间超出视频长度")
            frame_count = len(content_frame_times(start_us, end_us))
        else:
            raise ValueError("MEDIA_KIND_INVALID: 仅图片或视频可做画面分析")
        if _privacy_pending(db, asset_id, start_us, end_us):
            status = "privacy_review"
        elif _has_matching_observation(
            db, asset_id, fingerprint, start_us, end_us, method_version,
        ):
            status = "reused"
        else:
            status = "pending"
        items.append({
            "assetId": asset_id, "libraryId": selected_library_id,
            "relativePath": relative_path,
            "mediaKind": kind, "sourceStartUs": start_us,
            "sourceEndUs": end_us, "frameCount": frame_count,
            "status": status,
        })
        digest_items.append([*identity, relative_path, fingerprint, status])
    pending = [item for item in items if item["status"] == "pending"]
    blocked = [item for item in items if item["status"] == "privacy_review"]
    digest_payload = [task_id, library_id, profile_id, profile_digest,
                      profile["lastCheckedAt"], digest_items]
    preview_digest = hashlib.sha256(json.dumps(
        digest_payload, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "taskId": task_id, "libraryId": library_id, "profileId": profile_id,
        "provider": profile["provider"], "model": profile["model"],
        "methodVersion": method_version,
        "items": items, "selectedCount": len(items),
        "reusedCount": sum(item["status"] == "reused" for item in items),
        "pendingCount": len(pending), "blockedCount": len(blocked),
        "frameCount": sum(item["frameCount"] for item in pending),
        "costStatus": "unknown", "estimatedCost": None,
        "canStart": bool(pending) and not blocked,
        "previewDigest": preview_digest,
    }
