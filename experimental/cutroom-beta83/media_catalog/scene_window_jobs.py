"""Scoped, nonpaid durable jobs for local FFmpeg scene suggestions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from .scene_window_candidates import (
    detect_and_store_scene_windows, scene_windows_for_asset,
)
from .task_sources import scoped_asset_location
from .visual_media_samples import _source
from .workbench_worker import enqueue_job


MAX_SCENE_ASSETS = 500


def _has_technical_probe(db, asset_id: str) -> bool:
    """Scene/quality caches alone do not contain matching's media metadata."""
    row = db.connection.execute(
        "SELECT a.media_kind,p.result_json FROM assets a JOIN analysis_runs p USING(asset_id) "
        "WHERE a.asset_id=? AND p.stage='technical_probe' AND p.status='completed'",
        (asset_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        probe = json.loads(row["result_json"])
        if row["media_kind"] == "image":
            dimensions = (probe.get("width"), probe.get("height"))
        else:
            from .asset_index import _duration_seconds
            if _duration_seconds(row["result_json"]) is None:
                return False
            stream = next(item for item in probe.get("streams", [])
                          if isinstance(item, dict) and item.get("codec_type") == "video")
            dimensions = (stream.get("width"), stream.get("height"))
        return all(type(value) in (int, float) and math.isfinite(value) and value > 0
                   for value in dimensions)
    except (TypeError, ValueError, AttributeError, StopIteration):
        return False


def _ensure_technical_probe(db, item: dict, ffmpeg: Path) -> None:
    from .probe import probe_media
    from .image_probe import probe_image
    source, kind, fingerprint = _source(db, item["assetId"], item["libraryId"], item["relativePath"])
    if fingerprint != item["assetFingerprint"]:
        raise ValueError("ASSET_STALE: 技术检测前素材已变化")
    if _has_technical_probe(db, item["assetId"]):
        return
    ffprobe = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    result = probe_image(source) if kind == "image" else probe_media(source, ffprobe)
    _path, _kind, current = _source(db, item["assetId"], item["libraryId"], item["relativePath"])
    if current != fingerprint:
        raise ValueError("ASSET_STALE: 技术检测期间素材已变化")
    with db.transaction(immediate=True) as connection:
        row = connection.execute("SELECT quick_fingerprint FROM assets WHERE asset_id=?", (item["assetId"],)).fetchone()
        if row is None or row[0] != fingerprint:
            raise ValueError("ASSET_STALE: 技术检测期间台账已变化")
        connection.execute(
            "INSERT INTO analysis_runs(asset_id,stage,status,result_json,error_summary,updated_at) "
            "VALUES(?,'technical_probe','completed',?,NULL,datetime('now')) "
            "ON CONFLICT(asset_id,stage) DO UPDATE SET status='completed',result_json=excluded.result_json,"
            "error_summary=NULL,updated_at=excluded.updated_at",
            (item["assetId"], json.dumps(result, ensure_ascii=False)),
        )
    if not _has_technical_probe(db, item["assetId"]):
        raise ValueError("MEDIA_PROBE_FAILED: 无法取得选材所需的尺寸或时长")


def preview_scene_windows(db, task_id: str | None, library_id: str | None,
                          assets: list[dict], *, offset: int = 0,
                          limit: int = 50) -> dict:
    """Page cached suggestions without starting either local or paid work."""
    if (task_id is None) == (library_id is None):
        raise ValueError("ASSET_SCOPE_INVALID: 必须选择任务或一个素材目录")
    if type(assets) is not list or not 1 <= len(assets) <= MAX_SCENE_ASSETS:
        raise ValueError("SCENE_ASSETS_INVALID: 每次选择1至500项图片或视频")
    if type(offset) is not int or offset < 0 or type(limit) is not int or \
            not 1 <= limit <= 2000:
        raise ValueError("SCENE_PAGE_INVALID: 候选分页范围无效")
    seen = set()
    items = []
    pending = []
    pending_evidence = []
    measured_refs = {}
    asset_states = []
    for asset in assets:
        if type(asset) is not dict:
            raise ValueError("SCENE_ASSETS_INVALID: 素材格式错误")
        identity = (asset.get("assetId"), asset.get("libraryId"),
                    asset.get("relativePath"))
        if identity in seen:
            raise ValueError("SCENE_ASSETS_INVALID: 素材重复选择")
        seen.add(identity)
        _validate_scope(db, task_id, library_id, asset)
        current = scene_windows_for_asset(db, *identity)
        from .quality_windows import quality_result
        quality = quality_result(db, asset["assetId"])
        if quality["status"] != "measured":
            pending_evidence.append(asset["assetId"])
        else:
            measured_refs[asset["assetId"]] = dict(assetId=asset["assetId"],
                assetFingerprint=quality["assetFingerprint"],sourceStartUs=0,sourceEndUs=quality["durationUs"])
        if current["status"] != "ready" or not _has_technical_probe(db, asset["assetId"]):
            pending.append(asset["assetId"])
        if current["status"] == "ready":
            items.extend(current["items"])
        asset_states.append({"assetId": asset["assetId"],
                             "relativePath": asset["relativePath"],
                             "status": current["status"],
                             "windowCount": len(current["items"])})
    from .duplicate_evidence import pending_quality_pairs
    pending_pairs = pending_quality_pairs(db,list(measured_refs.values()))
    pending_quality = list(pending_evidence)
    if pending_pairs:
        pending_evidence = list(dict.fromkeys([*pending_evidence,*measured_refs]))
    return {"status": "ready" if not pending else "needs_detection",
            "pendingAssets": pending, "pendingQualityAssets": pending_quality, "pendingEvidenceAssets": pending_evidence, "pendingDuplicatePairs": pending_pairs, "assetStates": asset_states,
            "totalWindows": len(items), "offset": offset, "limit": limit,
            "hasMore": offset + limit < len(items),
            "items": items[offset:offset + limit]}


def _validate_scope(db, task_id: str | None, library_id: str | None,
                    item: dict) -> str:
    asset_id = item.get("assetId")
    selected_library = item.get("libraryId")
    relative_path = item.get("relativePath")
    if any(not isinstance(value, str) or not value for value in
           (asset_id, selected_library, relative_path)):
        raise ValueError("ASSET_SCOPE_INVALID: 缺少明确素材位置")
    if task_id is not None:
        scoped = scoped_asset_location(db, task_id, asset_id)
        if scoped is None or scoped["library_id"] != selected_library or \
                scoped["relative_path"] != relative_path:
            raise ValueError("ASSET_SCOPE_INVALID: 素材不属于当前任务范围")
    elif selected_library != library_id:
        raise ValueError("ASSET_SCOPE_INVALID: 素材不属于本次选择的目录")
    _path, kind, fingerprint = _source(db, asset_id, selected_library,
                                       relative_path)
    if kind not in {"image", "video"}:
        raise ValueError("MEDIA_KIND_INVALID: 仅视频或图片可生成画面候选")
    return fingerprint


def queue_scene_window_job(db, task_id: str | None, library_id: str | None,
                           assets: list[dict], request_id: str) -> dict:
    if (task_id is None) == (library_id is None):
        raise ValueError("ASSET_SCOPE_INVALID: 必须选择任务或一个素材目录")
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", request_id
    ):
        raise ValueError("REQUEST_ID_INVALID: 本机检测请求编号无效")
    if type(assets) is not list or not 1 <= len(assets) <= MAX_SCENE_ASSETS:
        raise ValueError("SCENE_ASSETS_INVALID: 每次选择1至500项图片或视频")
    identities = [(item.get("assetId"), item.get("libraryId"),
                   item.get("relativePath")) for item in assets
                  if type(item) is dict]
    if len(identities) != len(assets) or len(set(identities)) != len(identities):
        raise ValueError("SCENE_ASSETS_INVALID: 素材列表存在重复或格式错误")
    request_key = json.dumps([task_id, library_id, request_id],
                             ensure_ascii=False, separators=(",", ":"))
    job_id = "scene-" + hashlib.sha256(request_key.encode()).hexdigest()[:32]
    existing = db.connection.execute(
        "SELECT payload_json FROM job_payloads WHERE job_id=?", (job_id,),
    ).fetchone()
    if existing is not None:
        prior = json.loads(existing["payload_json"])
        if prior.get("selections") != assets:
            raise ValueError("JOB_IDEMPOTENCY_CONFLICT: 请求编号已绑定另一批素材")
        return {"jobId": job_id, "assetCount": len(prior["items"])}
    items = []
    for item in assets:
        fingerprint = _validate_scope(db, task_id, library_id, item)
        items.append({"assetId": item["assetId"],
                      "libraryId": item["libraryId"],
                      "relativePath": item["relativePath"],
                      "assetFingerprint": fingerprint})
    payload = {"selections": assets, "items": items, "library_id": library_id}
    try:
        enqueue_job(db, job_id=job_id, job_type="scene_detection",
                    task_id=task_id, library_id=library_id, payload=payload)
    except ValueError as error:
        # Another caller may have won the deterministic job-ID race.
        row = db.connection.execute(
            "SELECT payload_json FROM job_payloads WHERE job_id=?", (job_id,),
        ).fetchone()
        if row is None or json.loads(row["payload_json"]).get("selections") != assets:
            raise ValueError("JOB_IDEMPOTENCY_CONFLICT: 请求编号已绑定另一批素材") from error
    return {"jobId": job_id, "assetCount": len(items)}


def scene_window_job_status(db, job_id: str) -> dict:
    row = db.connection.execute(
        """SELECT j.job_type,p.payload_json FROM jobs j
           JOIN job_payloads p USING(job_id) WHERE j.job_id=?""",
        (job_id,),
    ).fetchone()
    if row is None or row["job_type"] != "scene_detection":
        raise KeyError("SCENE_JOB_NOT_FOUND: 场景检测作业不存在")
    payload = json.loads(row["payload_json"])
    results = []
    for item in db.connection.execute(
        "SELECT * FROM scene_detection_items WHERE job_id=? ORDER BY item_index",
        (job_id,),
    ).fetchall():
        source = payload["items"][item["item_index"]]
        results.append({"index": item["item_index"], "assetId": item["asset_id"],
                        "relativePath": source["relativePath"],
                        "status": item["status"], "windowCount": item["window_count"],
                        "errorCode": item["error_code"],
                        "qualityStatus": item["quality_status"], "qualityErrorCode": item["quality_error"]})
    return {"jobId": job_id, "total": len(payload["items"]), "items": results}


def _record_item(db, job_id: str, index: int, item: dict, status: str,
                 window_count: int = 0, error_code: str | None = None) -> None:
    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO scene_detection_items(job_id,item_index,asset_id,
                 asset_fingerprint,status,window_count,error_code)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,item_index) DO UPDATE SET
                 status=excluded.status,window_count=excluded.window_count,
                 error_code=excluded.error_code,updated_at=datetime('now')""",
            (job_id, index, item["assetId"], item["assetFingerprint"], status,
             window_count, error_code),
        )


def run_scene_window_job(db, job, project_root, *,
                         ffmpeg: Path | None = None,
                         runner=subprocess.run) -> dict:
    ffmpeg = Path(ffmpeg or shutil.which("ffmpeg") or "ffmpeg")
    from .quality_windows import analyze_quality_windows
    from .dedup import FrameSamplingCancelled
    from .duplicate_evidence import compare_quality_results
    items = job.payload["items"]
    for index, item in enumerate(items):
        prior = db.connection.execute(
            "SELECT status,quality_status FROM scene_detection_items WHERE job_id=? AND item_index=?",
            (job.job_id, index),
        ).fetchone()
        if (prior is not None and prior["status"] == "succeeded"
                and prior["quality_status"] == "measured"
                and _has_technical_probe(db, item["assetId"])):
            continue
        control = db.connection.execute(
            "SELECT cancel_requested FROM jobs WHERE job_id=?", (job.job_id,),
        ).fetchone()
        if control is None or control["cancel_requested"]:
            break
        try:
            fingerprint = _validate_scope(
                db, job.task_id, job.payload.get("library_id"), item,
            )
            if fingerprint != item["assetFingerprint"]:
                raise ValueError("ASSET_STALE: 场景检测前素材内容变化")
            _ensure_technical_probe(db, item, ffmpeg)
            cached = scene_windows_for_asset(
                db, item["assetId"], item["libraryId"], item["relativePath"],
            )
            result = cached if cached["status"] == "ready" else \
                detect_and_store_scene_windows(
                    db, item["assetId"], item["libraryId"], item["relativePath"],
                    ffmpeg, runner=runner,
                )
            _record_item(db, job.job_id, index, item, "succeeded", len(result["items"]))
            try:
                def cancelled():
                    control = db.connection.execute("SELECT cancel_requested FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
                    return control is None or bool(control[0])
                analyze_quality_windows(db, item["assetId"], fingerprint, ffmpeg=ffmpeg,
                    library_id=item["libraryId"], relative_path=item["relativePath"], cancel_check=cancelled)
                quality_status, quality_error = "measured", None
            except FrameSamplingCancelled:
                with db.transaction() as connection:
                    connection.execute("UPDATE scene_detection_items SET quality_status='pending',quality_error='LOCAL_ANALYSIS_CANCELLED' WHERE job_id=? AND item_index=?", (job.job_id, index))
                break
            except (ValueError, KeyError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                quality_status = "failed"
                quality_error = "QUALITY_ANALYSIS_TIMEOUT" if isinstance(error, subprocess.TimeoutExpired) else str(error).partition(":")[0]
                if not re.fullmatch(r"[A-Z][A-Z0-9_]+", quality_error):
                    quality_error = "QUALITY_ANALYSIS_FAILED"
            with db.transaction() as connection:
                connection.execute("UPDATE scene_detection_items SET quality_status=?,quality_error=? WHERE job_id=? AND item_index=?",
                    (quality_status, quality_error, job.job_id, index))
        except (ValueError, KeyError, RuntimeError, OSError,
                subprocess.TimeoutExpired) as error:
            code = str(error).partition(":")[0]
            if not re.fullmatch(r"[A-Z][A-Z0-9_]+", code):
                code = "SCENE_DETECTION_FAILED"
            _record_item(db, job.job_id, index, item, "failed", error_code=code)
    # Only compare directly selected, successfully measured assets. Bound pair
    # work independently of library size; additional pairs remain unmeasured.
    measured_ids = list(dict.fromkeys(row["asset_id"] for row in db.connection.execute(
        "SELECT asset_id FROM scene_detection_items WHERE job_id=? AND quality_status='measured' ORDER BY item_index",
        (job.job_id,))))
    compared, pair_failures, reused_pairs, pending_pairs = 0, 0, 0, 0
    for index, left_id in enumerate(measured_ids):
        for right_index, right_id in enumerate(measured_ids[index+1:], start=index+1):
            control = db.connection.execute("SELECT cancel_requested FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
            if control is None or control[0]:
                pending_pairs += len(measured_ids)-right_index
                break
            try:
                pair = compare_quality_results(db, left_id, right_id, allow_new=compared < 100)
                if pair is None: pending_pairs += 1
                elif pair['reused']: reused_pairs += 1
                else: compared += 1
            except (ValueError, KeyError):
                pair_failures += 1
                if compared < 100: compared += 1
    statuses = [row["status"] for row in db.connection.execute(
        "SELECT status FROM scene_detection_items WHERE job_id=? ORDER BY item_index",
        (job.job_id,),
    ).fetchall()]
    quality_failures = db.connection.execute(
        "SELECT count(*) FROM scene_detection_items WHERE job_id=? AND quality_status='failed'", (job.job_id,)).fetchone()[0]
    return {"cursor": "scene-detection-complete", "progress": {
        "current": len(statuses), "total": len(items)},
        "phase_label": "场景候选已生成，部分画质证据未完成" if quality_failures else "场景候选生成完成" if "failed" not in statuses else "部分素材检测失败",
        "result": {"status": "partial" if "failed" in statuses else "succeeded",
                   "succeededCount": statuses.count("succeeded"),
                   "failedCount": statuses.count("failed"),
                   "pendingCount": len(items) - len(statuses),
                   "qualityMeasuredCount": len(measured_ids), "qualityFailedCount": quality_failures,
                   "duplicateComparedPairs": compared, "duplicateFailedPairs": pair_failures,
                   "duplicateReusedPairs": reused_pairs, "duplicatePendingPairs": pending_pairs}}
