"""Versioned human segment marks, kept separate from model evidence and scene cuts."""

import hashlib
import json
import math

from .visual_observations import CAMERA_MOTIONS, MOTION_KINDS


USAGE_STATUSES = frozenset(("可用", "待核验", "不建议使用"))


def segment_id_for_window(asset_id: str, start_us: int, end_us: int) -> str:
    digest = hashlib.sha256(f"{asset_id}\0{start_us}\0{end_us}".encode("utf-8")).hexdigest()[:24]
    return f"seg-{digest}"


def _asset(db, asset_id: str):
    asset = db.connection.execute(
        "SELECT media_kind,quick_fingerprint FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if asset is None:
        raise KeyError(f"ASSET_NOT_FOUND: {asset_id}")
    return asset


def _duration_us(db, asset_id: str) -> int | None:
    from .asset_index import _duration_seconds
    row = db.connection.execute(
        "SELECT result_json FROM analysis_runs WHERE asset_id=? AND stage='technical_probe' "
        "AND status='completed'", (asset_id,)
    ).fetchone()
    duration = _duration_seconds(row["result_json"]) if row else None
    return round(duration * 1_000_000) if duration is not None else None


def current_marks(db, asset_id: str, *, request_evidence=None) -> dict[str, dict]:
    asset = request_evidence.assets[asset_id] if request_evidence is not None else _asset(db, asset_id)
    rows = request_evidence.raw_marks(asset_id) if request_evidence is not None else db.connection.execute(
        "SELECT * FROM catalog_segment_marks WHERE asset_id=? AND asset_fingerprint=?",
        (asset_id, asset["quick_fingerprint"]),
    ).fetchall()
    effective = {}
    for raw in rows:
        mark = dict(raw)
        if mark["edited_fields_json"] is not None:
            edited = set(json.loads(mark["edited_fields_json"]))
            observation = request_evidence.mark_observation(asset_id, mark) if request_evidence is not None else db.connection.execute(
                """SELECT * FROM visual_observations WHERE asset_id=? AND asset_fingerprint=?
                   AND source_start_us=? AND source_end_us=?
                   ORDER BY CASE origin WHEN 'human' THEN 0 ELSE 1 END,
                            observation_id DESC LIMIT 1""",
                (asset_id, asset["quick_fingerprint"], mark["base_start_us"],
                 mark["base_end_us"]),
            ).fetchone()
            if observation is not None:
                if "源开始秒" not in edited:
                    mark["source_start_us"] = observation["source_start_us"]
                if "源结束秒" not in edited:
                    mark["source_end_us"] = observation["source_end_us"]
                if "描述" not in edited:
                    mark["title"] = observation["title"]
                    mark["description"] = observation["description"]
                for column, field in (("镜头方向", "camera_motion"),
                                      ("运动方式", "motion_kind"),
                                      ("主体运动", "subject_motion")):
                    if column not in edited:
                        mark[field] = observation[field]
        effective[mark["segment_id"]] = mark
    return effective


def save_segment_mark(
    db, asset_id: str, segment_id: str, *, expected_revision: int,
    expected_fingerprint: str, source_start_us: int, source_end_us: int,
    title: str | None, description: str | None, camera_motion: str,
    usage_status: str, note: str = "", motion_kind: str | None = None,
    subject_motion: str | None = None,
) -> dict:
    """Edit a currently observed window with optimistic concurrency."""
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise ValueError("REVISION_INVALID: 需要当前片段版本")
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise ValueError("ASSET_FINGERPRINT_REQUIRED: 需要当前素材指纹")
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("SEGMENT_INVALID: 片段标识无效")
    for value in (source_start_us, source_end_us):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("SOURCE_WINDOW_INVALID: 时码须为非负整数微秒")
    if usage_status not in USAGE_STATUSES:
        raise ValueError("USAGE_STATUS_INVALID: 使用标记无效")
    if not isinstance(note, str) or not all(value is None or isinstance(value, str)
                                        for value in (title, description, subject_motion)):
        raise ValueError("SEGMENT_TEXT_INVALID: 文字字段无效")
    if any(value is not None and len(value) > 4000 for value in (title, description, note, subject_motion)):
        raise ValueError("SEGMENT_TEXT_INVALID: 文字字段过长")
    asset = _asset(db, asset_id)
    if asset["quick_fingerprint"] != expected_fingerprint:
        raise ValueError("ASSET_STALE: 素材内容已变化，请重新分析")
    if asset["media_kind"] == "image":
        if (source_start_us, source_end_us) != (0, 0) or camera_motion != "不适用" or motion_kind is not None:
            raise ValueError("SOURCE_WINDOW_INVALID: 照片没有源视频区间或镜头运动")
    elif asset["media_kind"] == "video":
        duration_us = _duration_us(db, asset_id)
        if source_end_us <= source_start_us or duration_us is None or source_end_us > duration_us:
            raise ValueError("SOURCE_WINDOW_INVALID: 区间超出已测量的原片时长")
        if camera_motion not in CAMERA_MOTIONS or motion_kind not in MOTION_KINDS | {None}:
            raise ValueError("MOTION_INVALID: 镜头运动无效")
    else:
        raise ValueError("MEDIA_KIND_INVALID: 仅视频或照片可标注画面片段")
    with db.transaction(immediate=True) as conn:
        current = conn.execute("SELECT quick_fingerprint FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if current is None or current["quick_fingerprint"] != expected_fingerprint:
            raise ValueError("ASSET_STALE: 素材内容已变化，请重新分析")
        prior = conn.execute("SELECT * FROM catalog_segment_marks WHERE segment_id=? AND asset_id=?",
                             (segment_id, asset_id)).fetchone()
        if prior:
            if prior["asset_fingerprint"] != expected_fingerprint:
                raise ValueError("ASSET_STALE: 原片段指向旧素材内容")
            if prior["revision"] != expected_revision:
                raise ValueError("REVISION_CONFLICT: 片段已由其他编辑更新")
            base_start, base_end = prior["base_start_us"], prior["base_end_us"]
        else:
            if expected_revision != 0:
                raise ValueError("REVISION_CONFLICT: 片段版本不匹配")
            observation = conn.execute(
                "SELECT source_start_us,source_end_us FROM visual_observations "
                "WHERE asset_id=? AND asset_fingerprint=?",
                (asset_id, expected_fingerprint),
            ).fetchall()
            window = next((row for row in observation if segment_id_for_window(
                asset_id, row["source_start_us"], row["source_end_us"]) == segment_id), None)
            if window is None:
                from .quality_windows import quality_result
                window = next(({"source_start_us": w["sourceStartUs"], "source_end_us": w["sourceEndUs"]}
                    for w in quality_result(db, asset_id).get("windows", [])
                    if segment_id_for_window(asset_id, w["sourceStartUs"], w["sourceEndUs"]) == segment_id), None)
            if window is None:
                raise ValueError("SEGMENT_INVALID: 当前素材没有这个片段")
            base_start, base_end = window["source_start_us"], window["source_end_us"]
        conn.execute(
            """INSERT INTO catalog_segment_marks(
               segment_id,asset_id,base_start_us,base_end_us,asset_fingerprint,
               source_start_us,source_end_us,title,description,camera_motion,motion_kind,
               subject_motion,usage_status,note,revision)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(segment_id) DO UPDATE SET
                 source_start_us=excluded.source_start_us,source_end_us=excluded.source_end_us,
                 title=excluded.title,description=excluded.description,
                 camera_motion=excluded.camera_motion,motion_kind=excluded.motion_kind,
                 subject_motion=excluded.subject_motion,usage_status=excluded.usage_status,
                 note=excluded.note,revision=catalog_segment_marks.revision+1,
                 edited_fields_json=NULL,
                 updated_at=datetime('now')""",
            (segment_id, asset_id, base_start, base_end, expected_fingerprint,
             source_start_us, source_end_us, title, description, camera_motion,
             motion_kind, subject_motion, usage_status, note),
        )
        saved = conn.execute("SELECT * FROM catalog_segment_marks WHERE segment_id=?", (segment_id,)).fetchone()
    return _public_mark(saved)


def _public_mark(row) -> dict:
    return {
        "segmentId": row["segment_id"], "revision": row["revision"],
        "sourceStartUs": row["source_start_us"], "sourceEndUs": row["source_end_us"],
        "title": row["title"], "description": row["description"],
        "cameraMotion": row["camera_motion"], "motionKind": row["motion_kind"],
        "subjectMotion": row["subject_motion"], "usageStatus": row["usage_status"],
        "note": row["note"], "tags": row["tags"],
        "qualityIssue": row["quality_issue"], "usableReason": row["usable_reason"],
        "duplicateNote": row["duplicate_note"],
        "origin": "human", "uncertaintyReason": None,
    }


def resolve_source_segment(connection, asset_id: str, segment_id: str) -> dict:
    """Read the current human window or existing scene cut without duplicating ledger rows."""
    from types import SimpleNamespace
    db = SimpleNamespace(connection=connection)
    mark = connection.execute(
        "SELECT asset_fingerprint FROM catalog_segment_marks WHERE segment_id=? AND asset_id=?",
        (segment_id, asset_id),
    ).fetchone()
    if mark is not None:
        asset = _asset(db, asset_id)
        if mark["asset_fingerprint"] != asset["quick_fingerprint"]:
            raise ValueError("SOURCE_REFERENCE_MISMATCH: 人工片段已失效，请重新核对当前素材")
        current = current_marks(db, asset_id).get(segment_id)
        if current is None:
            raise ValueError("SOURCE_REFERENCE_MISMATCH: 人工片段已失效")
        if current["usage_status"] == "不建议使用":
            raise ValueError("SOURCE_USAGE_REJECTED: 人工已禁用该源片段")
        start, end = current["source_start_us"], current["source_end_us"]
        duration = _duration_us(db, asset_id)
        if (type(start) is not int or type(end) is not int or not 0 <= start < end
                or duration is None or end > duration):
            raise ValueError("SOURCE_REFERENCE_MISMATCH: 人工片段越过当前实测来源")
        return {"segment_id": segment_id, "asset_id": asset_id,
                "start_sec": start / 1_000_000, "end_sec": end / 1_000_000,
                "label": current["title"], "reference_type": "human_mark",
                "asset_fingerprint": current["asset_fingerprint"],
                "revision": current["revision"], "usage_status": current["usage_status"]}
    row = connection.execute(
        "SELECT segment_id,asset_id,start_sec,end_sec,label FROM segments WHERE segment_id=?",
        (segment_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"素材片段不存在：{segment_id}")
    result = dict(row)
    if result["asset_id"] != asset_id:
        raise ValueError("SOURCE_REFERENCE_MISMATCH: 素材片段不属于所选素材")
    try:
        start, end = float(result["start_sec"]), float(result["end_sec"])
    except (TypeError, ValueError) as error:
        raise ValueError("源片段时间无效") from error
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
        raise ValueError("源片段时间顺序或边界无效")
    result["start_sec"], result["end_sec"] = start, end
    return result
