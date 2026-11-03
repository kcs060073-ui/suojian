"""Local, preview-bound music level advice and human listening feedback.

Advice estimates the isolated voice/music balance before the final limiter.
It is not LUFS, music suitability, or approval, and never edits settings.
"""
import json
import math
import os
from pathlib import Path
import statistics

import numpy as np

from .path_utils import filesystem_path
from .packaging_audio_evidence import _run_bounded, AudioEvidenceError
from .tools import find_ffmpeg, default_ffprobe_search_roots

METHOD = "full-track-mix-energy-v2"
RATINGS = {"too_weak", "audible_raise", "balanced", "too_loud", "style_mismatch"}
LIMITATIONS = "基于本版整轨处理后的人声与音乐分轨，抽取预览覆盖片段比较能量；不含同期声、音效和最终限幅，不代表曲风适配或全片验收，不会自动改音量。"


def suggest_gain(current_gain, windows):
    base = {"method": METHOD, "currentGain": current_gain, "suggestedGain": None,
            "windows": windows, "limitations": LIMITATIONS}
    if current_gain == 0:
        return {**base, "status": "muted"}
    if (not isinstance(current_gain, (int, float)) or not math.isfinite(current_gain)
            or not 0 < current_gain <= .5 or not windows):
        return {**base, "status": "unknown"}
    gaps = []
    for window in windows:
        voice, music = window.get("voiceDbfs"), window.get("musicDbfs")
        if (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (voice, music))
                or voice < -55 or music < -90):
            return {**base, "status": "unknown", "windows": []}
        gaps.append(voice-music)
    # Aim for a 16 dB median bed; no sampled window closer than 12 dB.
    adjustment = min(statistics.median(gaps)-16, min(gaps)-12)
    unconstrained = current_gain * 10 ** (max(-60, min(60, adjustment))/20)
    gain = max(0, min(.5, math.floor(unconstrained*100)/100))
    return {**base, "status": "measured", "suggestedGain": gain,
            "targetGapDb": 16, "minimumGapDb": 12,
            "limited": unconstrained > .5,
            "measuredGapRangeDb": [round(min(gaps), 2), round(max(gaps), 2)]}


def measurement_windows(timeline_map):
    """Sample inside covered media, including the tail of a merged long window."""
    if not timeline_map:
        return []
    first, last = timeline_map[0], timeline_map[-1]
    if len(timeline_map) == 1 and last["fullEnd"]-first["fullStart"] <= 5:
        return [{"fullStart": first["fullStart"], "fullEnd": last["fullEnd"]}]
    start, end = float(first["fullStart"]), float(last["fullEnd"])
    selected = [{"fullStart": start, "fullEnd": min(float(first["fullEnd"]), start+2.5)}]
    middle = (start+end)/2
    covered = min(timeline_map, key=lambda w: max(w["fullStart"]-middle, middle-w["fullEnd"], 0))
    span = min(8., covered["fullEnd"]-covered["fullStart"])
    center_start = max(covered["fullStart"], min(middle-span/2, covered["fullEnd"]-span))
    selected.append({"fullStart": center_start, "fullEnd": center_start+span})
    selected.append({"fullStart": max(float(last["fullStart"]), end-2.5), "fullEnd": end})
    return [item for i, item in enumerate(selected) if item not in selected[:i]]


def measure_rendered_music(music, stem_path, timeline_map, *, default_gain=.18):
    """Read preview-renderer stems; never re-normalize or rebuild a separate mix."""
    if not music:
        return {"status": "disabled", "method": METHOD, "suggestedGain": None}
    gain = music.get("gain", default_gain)
    if gain == 0:
        return suggest_gain(0, [])
    try:
        ffmpeg = find_ffmpeg(os.environ, search_roots=default_ffprobe_search_roots())
        stems = filesystem_path(Path(stem_path))
        if not stems.is_file():
            return {**suggest_gain(gain, []), "reason": "render_stems_unavailable"}
        selected = measurement_windows(list(timeline_map))
        if not selected:
            return {**suggest_gain(gain, []), "reason": "missing_preview_windows"}
        measures = []
        for window in selected:
            start = float(window["fullStart"])
            duration = min(8., float(window["fullEnd"])-start)
            command = [str(ffmpeg), "-v", "error", "-nostdin", "-ss", str(start),
                       "-i", str(stems), "-map", "0:a:0", "-t", str(duration),
                       "-ar", "48000", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]
            pcm = _run_bounded(command, timeout=25, max_bytes=math.ceil((duration+.1)*48000)*16)
            samples = np.frombuffer(pcm, dtype="<f4").reshape(-1, 4)
            if len(samples) < duration*48000*.95 or not np.isfinite(samples).all():
                raise AudioEvidenceError("MIX_AUDIO_INCOMPLETE")
            def rms(block):
                return round(20*math.log10(max(1e-6, float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))))), 3)
            measures.append({"fullStart": start, "fullEnd": round(start+duration, 3),
                             "voiceDbfs": rms(samples[:, :2]), "musicDbfs": rms(samples[:, 2:])})
        return suggest_gain(gain, measures)
    except (OSError, TypeError, ValueError, RuntimeError, KeyError):
        return {**suggest_gain(gain, []), "reason": "measurement_unavailable"}


def measure_digital_music(job, timeline_map):
    return measure_rendered_music(job.get("plan", {}).get("bgm"),
                                  job.get("audio_review_path"), timeline_map)


def record_feedback(db, task_id, preview_id, rating, request_id):
    if rating not in RATINGS or not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
        raise ValueError("MUSIC_FEEDBACK_INVALID: 请选择有效的试听反馈")
    with db.transaction(immediate=True) as connection:
        preview = connection.execute("SELECT task_id,status FROM packaging_preview_artifacts WHERE preview_id=?", (preview_id,)).fetchone()
        if preview is None or preview["task_id"] != task_id:
            raise PermissionError("MUSIC_PREVIEW_NOT_OWNED: 预览不属于当前任务")
        # Historical ready previews are allowed; feedback stays on that exact version.
        if preview["status"] != "ready":
            raise RuntimeError("MUSIC_PREVIEW_NOT_READY: 请先完成对应版本的预览")
        existing = connection.execute("SELECT * FROM packaging_music_feedback WHERE request_id=?", (request_id,)).fetchone()
        if existing and (existing["preview_id"] != preview_id or existing["rating"] != rating):
            raise RuntimeError("MUSIC_FEEDBACK_REQUEST_CONFLICT: 此次反馈编号已被使用")
        connection.execute("INSERT OR IGNORE INTO packaging_music_feedback(request_id,preview_id,rating) VALUES(?,?,?)", (request_id, preview_id, rating))
        return dict(connection.execute("SELECT * FROM packaging_music_feedback WHERE request_id=?", (request_id,)).fetchone())


def review_history(db, task_id, plan_id):
    owner = db.connection.execute("SELECT task_id FROM packaging_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if owner is None or owner["task_id"] != task_id:
        raise PermissionError("MUSIC_PLAN_NOT_OWNED: 方案不属于当前任务")
    rows = db.connection.execute("SELECT a.preview_id,a.plan_id,a.audio_advice_json,a.duration_seconds,p.plan_name FROM packaging_preview_artifacts a JOIN packaging_plans p USING(plan_id) WHERE a.task_id=? AND a.status='ready' ORDER BY a.rowid DESC LIMIT 6", (task_id,)).fetchall()
    result = []
    for row in rows:
        feedback = db.connection.execute("SELECT rating,created_at FROM packaging_music_feedback WHERE preview_id=? ORDER BY rowid DESC LIMIT 1", (row["preview_id"],)).fetchone()
        dimensions = {}
        for item in db.connection.execute("SELECT rating,created_at FROM packaging_music_feedback WHERE preview_id=? ORDER BY rowid DESC", (row["preview_id"],)):
            dimensions.setdefault("style" if item["rating"] == "style_mismatch" else "volume", dict(item))
        result.append({"planId": row["plan_id"], "feedbackByDimension": dimensions, "previewId": row["preview_id"], "planName": row["plan_name"], "advice": json.loads(row["audio_advice_json"]) if row["audio_advice_json"] else None,
                       "durationSeconds": row["duration_seconds"], "feedback": dict(feedback) if feedback else None,
                       "mediaUrl": f"/api/tasks/{task_id}/packaging-previews/{row['preview_id']}/media"})
    return result


def music_feedback_context(db, task_id):
    """Bind feedback to the listened file and picture, never the mutable plan."""
    from .provenance import latest_gate
    gate = latest_gate(db, task_id, "picture_lock")
    if not gate:
        return []
    result = []
    for row in db.connection.execute(
        "SELECT f.request_id,f.preview_id,f.rating,a.preview_receipt_json,a.audio_advice_json "
        "FROM packaging_music_feedback f JOIN packaging_preview_artifacts a USING(preview_id) "
        "WHERE a.task_id=? AND a.picture_lock_digest=? ORDER BY f.rowid",
        (task_id, gate["gate_digest"]),
    ):
        try:
            receipt = json.loads(row["preview_receipt_json"] or "{}")
            advice = json.loads(row["audio_advice_json"] or "{}")
        except (TypeError, ValueError):
            continue
        tracks = []
        for asset in receipt.get("asset_versions", []):
            if not isinstance(asset, dict):
                continue
            kind, sha = asset.get("kind", ""), asset.get("sha256")
            if not isinstance(kind, str) or not isinstance(sha, str) or len(sha) != 64:
                continue
            if kind.startswith("packaging:bgm:"):
                tracks.append({"assetRef": kind.removeprefix("packaging:bgm:"), "sourceSha256": sha})
            elif kind.startswith("packaging:legacy:bgm:"):
                # Ad receipts bind legacy layer IDs rather than catalog references.
                # The immutable audio hash suffices; never guess from today's plan.
                tracks.append({"assetRef": None, "sourceSha256": sha})
        result.append({"requestId": row["request_id"], "previewId": row["preview_id"],
                       "rating": row["rating"], "tracks": tracks,
                       "gain": advice.get("currentGain"), "scope": "current_task_and_picture"})
    return result


def rejected_music_hashes(feedback):
    return {track["sourceSha256"] for item in feedback if item["rating"] == "style_mismatch"
            for track in item["tracks"]}
