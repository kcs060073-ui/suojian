"""Evidence-backed visual observations for source windows, never file-name guesses."""

import json
import math


CAMERA_MOTIONS = frozenset((
    "静止", "向左", "向右", "向上", "向下", "推进", "拉远", "环绕", "复合", "未知",
))
MODEL_METHODS = frozenset(("multimodal-video", "multimodal-frames"))
MOTION_KINDS = frozenset((
    "摇摄", "横移", "俯仰", "推进后退", "变焦", "其他", "无法区分",
))

SINGLE_FRAME_UNCERTAINTY = "仅单帧，无法判断镜头及主体运动"


def effective_visual_observations(rows, *, replaced_windows=()):
    """Project current model claims without reviving superseded broad windows.

    Later v2 observations replace overlapping model claims, including another
    analysis with the same profile. Keep uncovered portions for manual selection
    and retain the precedence of human observations and marks.
    This is a read projection: original observations and their IDs stay intact.
    """
    rows = [dict(row) for row in rows]
    refined = {}
    for row in rows:
        if row["origin"] == "model" and row["method_version"].startswith("visual-content-v2-"):
            refined.setdefault((row["asset_id"], row["asset_fingerprint"]), []).append(row)
    result = []
    for row in rows:
        start, end = row["source_start_us"], row["source_end_us"]
        if (start, end) in replaced_windows:
            continue
        ranges = [(start, end)]
        if row["origin"] == "model":
            for newer in refined.get((row["asset_id"], row["asset_fingerprint"]), []):
                if (row["method_version"].startswith("visual-content-v2-")
                        and newer["observation_id"] <= row["observation_id"]):
                    continue
                left, right = newer["source_start_us"], newer["source_end_us"]
                if start == end == left == right == 0:
                    ranges = []  # Still images have one point, not a video interval.
                    break
                remaining = []
                for a, b in ranges:
                    if left >= b or right <= a:
                        remaining.append((a, b))
                    else:
                        if a < left:
                            remaining.append((a, left))
                        if right < b:
                            remaining.append((right, b))
                ranges = remaining
        for a, b in ranges:
            projected = {**row, "source_start_us": a, "source_end_us": b}
            if (a, b) != (start, end):
                projected["evidence_json"] = json.dumps([
                    t for t in json.loads(row["evidence_json"]) if a <= t <= b])
            result.append(projected)
    return result


def require_single_frame_unknown(camera, motion_kind, subject, uncertainty):
    if (camera != "未知" or motion_kind != "无法区分" or subject != "未知"
            or not isinstance(uncertainty, str) or SINGLE_FRAME_UNCERTAINTY not in uncertainty):
        raise ValueError("VISUAL_SINGLE_FRAME_MOTION_INVALID: 单帧的镜头和主体运动必须未知，并注明仅单帧无法判断")


def _asset(db, asset_id):
    row = db.connection.execute(
        "SELECT media_kind,quick_fingerprint FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"ASSET_NOT_FOUND: {asset_id}")
    return row


def record_visual_observation(
    db, asset_id: str, source_start_us: int, source_end_us: int, *,
    origin: str, method: str, method_version: str, evidence_us: list[int],
    source_segment_id: str | None = None, title: str | None = None,
    description: str | None = None, camera_motion: str = "未知",
    motion_kind: str | None = None, subject_motion: str | None = None,
    entry_motion: str | None = None, exit_motion: str | None = None,
    uncertainty_reason: str | None = None,
    expected_asset_fingerprint: str | None = None,
    sample_frame_times_us: list[int] | None = None,
) -> int:
    """Append an evidence record; human decisions retain priority on read."""
    asset = _asset(db, asset_id)
    if asset["media_kind"] not in {"video", "image"}:
        raise ValueError("MEDIA_KIND_INVALID: 仅视频或照片可记录画面观察")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in
           (source_start_us, source_end_us)) or source_start_us < 0:
        raise ValueError("SOURCE_WINDOW_INVALID: 源时码必须为非负整数微秒")
    if asset["media_kind"] == "video" and source_end_us <= source_start_us:
        raise ValueError("SOURCE_WINDOW_INVALID: 视频观察区间必须大于零")
    if asset["media_kind"] == "image" and (source_start_us, source_end_us) != (0, 0):
        raise ValueError("SOURCE_WINDOW_INVALID: 照片没有源视频区间")
    if origin not in {"model", "human"} or not method_version or not method:
        raise ValueError("METHOD_INVALID: 缺少可信的观察来源和版本")
    if origin == "model" and not expected_asset_fingerprint:
        raise ValueError("ASSET_FINGERPRINT_REQUIRED: 模型观察须绑定取样时的素材指纹")
    if origin == "model" and method not in MODEL_METHODS:
        raise ValueError("METHOD_INVALID: 模型画面结论必须来自多帧或短片段")
    if camera_motion not in CAMERA_MOTIONS or entry_motion not in CAMERA_MOTIONS | {None} \
            or exit_motion not in CAMERA_MOTIONS | {None}:
        raise ValueError("MOTION_INVALID: 镜头方向无效")
    if motion_kind not in MOTION_KINDS | {None}:
        raise ValueError("MOTION_INVALID: 镜头运动类型无效")
    if origin == "model" and camera_motion == "未知" and not uncertainty_reason:
        raise ValueError("UNCERTAINTY_REQUIRED: 未识别方向须注明原因")
    if asset["media_kind"] == "image" and any(value not in (None, "未知") for value in
                                              (camera_motion, entry_motion, exit_motion)):
        raise ValueError("MOTION_INVALID: 照片没有源镜头运动")
    if not isinstance(evidence_us, list) or not evidence_us or any(
        isinstance(value, bool) or not isinstance(value, int) or
        value < source_start_us or value > source_end_us for value in evidence_us
    ):
        raise ValueError("EVIDENCE_REQUIRED: 必须提供区间内的证据时码")
    if origin == "model" and asset["media_kind"] == "video" and len(set(evidence_us)) < 2:
        # Only validated content segmentation may retain a single sampled frame.
        # Broad video summaries and direct writes retain the two-frame minimum.
        if (method != "multimodal-frames" or not method_version.startswith("visual-content-v2-")
                or not isinstance(sample_frame_times_us, list)
                or any(type(t) is not int for t in sample_frame_times_us)
                or len(set(sample_frame_times_us)) != len(sample_frame_times_us)
                or [t for t in sample_frame_times_us if source_start_us <= t < source_end_us] != evidence_us
                or evidence_us != [source_start_us]):
            raise ValueError("EVIDENCE_REQUIRED: 视频模型结论至少需要两个不同时码；单帧内容段须有真实取样边界")
        require_single_frame_unknown(camera_motion, motion_kind, subject_motion, uncertainty_reason)
        if entry_motion not in (None, "未知") or exit_motion not in (None, "未知"):
            raise ValueError("VISUAL_SINGLE_FRAME_MOTION_INVALID: 单帧不能判断进出镜运动")
    if source_segment_id is not None:
        segment = db.connection.execute(
            "SELECT start_sec,end_sec FROM segments WHERE segment_id=? AND asset_id=?",
            (source_segment_id, asset_id),
        ).fetchone()
        if segment is None or source_start_us < round(segment["start_sec"] * 1_000_000) \
                or source_end_us > round(segment["end_sec"] * 1_000_000):
            raise ValueError("SEGMENT_INVALID: 片段不属于该素材")
    with db.transaction() as connection:
        current = connection.execute(
            "SELECT quick_fingerprint FROM assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if current is None:
            raise KeyError(f"ASSET_NOT_FOUND: {asset_id}")
        if origin == "model" and current["quick_fingerprint"] != expected_asset_fingerprint:
            raise ValueError("ASSET_STALE: 源素材在模型分析期间已变化")
        cursor = connection.execute(
            """INSERT INTO visual_observations(
                asset_id,source_segment_id,source_start_us,source_end_us,
                asset_fingerprint,origin,method,method_version,evidence_json,
                title,description,camera_motion,motion_kind,subject_motion,
                entry_motion,exit_motion,uncertainty_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (asset_id, source_segment_id, source_start_us, source_end_us,
             current["quick_fingerprint"], origin, method, method_version,
             json.dumps(sorted(set(evidence_us))), title, description,
             camera_motion, motion_kind, subject_motion, entry_motion,
             exit_motion, uncertainty_reason),
        )
    return cursor.lastrowid


def visual_observation_for_shot(db, shot: dict, *, request_evidence=None) -> dict:
    """Show claims only when the observation covers the actual source trim."""
    asset_id = shot.get("asset_id")
    if not asset_id:
        return {"status": "unobserved", "cameraMotion": "未知", "title": None}
    asset = request_evidence.assets[asset_id] if request_evidence is not None else _asset(db, asset_id)
    kind = asset["media_kind"]
    if kind not in {"video", "image"}:
        return {"status": "not_applicable", "cameraMotion": "不适用", "title": None}
    if kind == "image":
        start_us = end_us = 0
    else:
        start = shot.get("resolved_source_start")
        end = shot.get("resolved_source_end")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not math.isfinite(value) for value in (start, end)) or end <= start:
            return {"status": "unobserved", "cameraMotion": "未知", "title": None}
        start_us, end_us = round(start * 1_000_000), round(end * 1_000_000)
    from .segment_marks import current_marks
    all_marks = request_evidence.marks(asset_id) if request_evidence is not None else current_marks(db, asset_id)
    marks = [mark for mark in all_marks.values()
             if mark["source_start_us"] <= start_us and mark["source_end_us"] >= end_us]
    if marks:
        mark = min(marks, key=lambda item: (
            item["source_end_us"] - item["source_start_us"], -item["revision"]
        ))
        if mark["usage_status"] == "不建议使用":
            return {"status": "rejected", "segmentId": mark["segment_id"],
                    "cameraMotion": "未知", "title": None, "usageStatus": "不建议使用"}
        return {
            "status": "observed", "segmentId": mark["segment_id"],
            "sourceStartUs": mark["source_start_us"], "sourceEndUs": mark["source_end_us"],
            "origin": "human", "method": "manual-segment-mark", "methodVersion": str(mark["revision"]),
            "evidenceUs": [], "title": mark["title"], "description": mark["description"], "tags": mark["tags"],
            "cameraMotion": mark["camera_motion"], "motionKind": mark["motion_kind"],
            "subjectMotion": mark["subject_motion"], "entryMotion": None, "exitMotion": None,
            "usageStatus": mark["usage_status"], "uncertaintyReason": None,
        }
    rows = request_evidence.visual_rows(asset_id, start_us, end_us) if request_evidence is not None else db.connection.execute(
        """SELECT * FROM visual_observations
           WHERE asset_id=?
           ORDER BY CASE origin WHEN 'human' THEN 0 ELSE 1 END,
                    (source_end_us-source_start_us) ASC,
                    observation_id DESC""",
        (asset_id,),
    ).fetchall()
    replaced_windows = {(mark["base_start_us"], mark["base_end_us"])
                        for mark in all_marks.values()}
    if request_evidence is None:
        rows = effective_visual_observations(rows, replaced_windows=replaced_windows)
        rows = sorted((row for row in rows if row["source_start_us"] <= start_us
                       and row["source_end_us"] >= end_us), key=lambda row: (
                           0 if row["origin"] == "human" else 1,
                           row["source_end_us"] - row["source_start_us"],
                           -row["observation_id"]))
    current = [row for row in rows if row["asset_fingerprint"] == asset["quick_fingerprint"]
               and (row["source_start_us"], row["source_end_us"]) not in replaced_windows]
    if not current:
        stale = any(row["asset_fingerprint"] != asset["quick_fingerprint"] for row in rows)
        return {"status": "stale" if stale else "unobserved",
                "cameraMotion": "不适用" if kind == "image" else "未知", "title": None}
    row = current[0]
    return {
        "status": "observed", "observationId": row["observation_id"],
        "sourceStartUs": row["source_start_us"], "sourceEndUs": row["source_end_us"],
        "origin": row["origin"], "method": row["method"],
        "methodVersion": row["method_version"],
        "evidenceUs": json.loads(row["evidence_json"]),
        "title": row["title"], "description": row["description"],
        "cameraMotion": "不适用" if kind == "image" else row["camera_motion"],
        "motionKind": row["motion_kind"], "subjectMotion": row["subject_motion"],
        "entryMotion": None if kind == "image" else row["entry_motion"],
        "exitMotion": None if kind == "image" else row["exit_motion"],
        "uncertaintyReason": row["uncertainty_reason"],
    }
