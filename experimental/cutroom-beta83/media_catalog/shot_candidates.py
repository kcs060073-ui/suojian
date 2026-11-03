"""Task-scoped storyboard choices at source-window granularity."""

from __future__ import annotations

from .asset_index import with_technical_durations
from .rough_media import list_rough_edit_assets
from .visual_observations import visual_observation_for_shot
from .segment_marks import current_marks
from .source_windows import usable_source_ranges
from .candidate_ranking import observed_text_match
from .quality_windows import quality_for_window, quality_source_ranges
from .duplicate_evidence import duplicates_for_window
from .shot_candidate_evidence import RequestEvidence


MAX_PAGE_SIZE = 100


def _public_candidate(db, asset: dict, start: float | None, end: float | None,
                      segment_id: str | None, request_evidence=None) -> dict:
    visual = visual_observation_for_shot(db, {
        "asset_id": asset["asset_id"],
        "media_kind": asset["media_kind"],
        "resolved_source_start": start,
        "resolved_source_end": end,
    }, **({"request_evidence":request_evidence} if request_evidence is not None else {}))
    observed = visual["status"] == "observed"
    return {
        "candidateKey": (
            f"{asset['asset_id']}|{segment_id or 'source'}|"
            f"{None if start is None else round(start * 1_000_000)}|"
            f"{None if end is None else round(end * 1_000_000)}"
        ),
        "assetId": asset["asset_id"],
        "mediaKind": asset["media_kind"],
        "relativePath": asset["relative_path"],
        "sourceMode": asset["source_mode"],
        "sourceSegmentId": segment_id,
        "sourceStart": start,
        "sourceEnd": end,
        "title": visual.get("title") if observed and visual.get("title") else asset["relative_path"],
        "description": visual.get("description") if observed else None,
        "observedTitle": visual.get("title") if observed else None,
        "observedTags": visual.get("tags") if observed else None,
        "usageStatus": visual.get("usageStatus") if observed else None,
        "cameraMotion": visual.get("cameraMotion") if observed else (
            "不适用" if asset["media_kind"] == "image" else "未知"
        ),
        "analysisStatus": visual["status"],
        "observationId": visual.get("observationId") if observed else None,
        "fixedPresenter": False,
        "qualityEvidence": quality_for_window(db, asset["asset_id"], round((start or 0)*1_000_000), round((end or 0)*1_000_000), **({"request_evidence":request_evidence} if request_evidence is not None else {})),
        "duplicateEvidence": duplicates_for_window(db, asset["asset_id"], round((start or 0)*1_000_000), round((end or 0)*1_000_000), **({"request_evidence":request_evidence} if request_evidence is not None else {})),
    }


from .performance import measured as _performance_measured, metric as _performance_metric

@_performance_measured("candidate_search")
def list_shot_candidates(db, task_id: str, *, limit: int = 50,
                         offset: int = 0, query: str = "") -> dict:
    """Return real observed windows and clearly labeled technical fallbacks."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError("候选分页大小无效")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("候选分页偏移无效")
    if not isinstance(query, str) or len(query) > 4000:
        raise ValueError("候选查询文字无效")
    eligible = with_technical_durations(db, list_rough_edit_assets(db, task_id))
    evidence = RequestEvidence(db, eligible)
    task = db.connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (task_id,),
    ).fetchone()
    digital = task is not None and task["task_type"] == "digital_human"
    items = []
    for asset in eligible:
        if asset.get("task_role") == "digital_human":
            if digital and asset["media_kind"] == "video":
                items.append({
                    "candidateKey": f"{asset['asset_id']}|presenter",
                    "assetId": asset["asset_id"], "mediaKind": "video",
                    "relativePath": asset["relative_path"],
                    "sourceMode": asset["source_mode"],
                    "sourceSegmentId": None, "sourceStart": None, "sourceEnd": None,
                    "title": "恢复口播原画面", "description": "原声连续，画面与口播固定同步",
                    "cameraMotion": "不适用", "analysisStatus": "fixed",
                    "observationId": None, "fixedPresenter": True,
                })
            continue
        if asset["media_kind"] not in {"image", "video"}:
            continue
        if asset["media_kind"] == "image":
            if not any(mark["usage_status"] == "不建议使用"
                       for mark in (evidence.marks(asset["asset_id"]) if evidence is not None else current_marks(db, asset["asset_id"])).values()):
                items.append(_public_candidate(db, asset, None, None, None, evidence))
            continue
        asset_id = asset["asset_id"]
        segments = evidence.segments(asset_id) if evidence is not None else [dict(row) for row in db.connection.execute(
            "SELECT segment_id,start_sec,end_sec FROM segments WHERE asset_id=? "
            "ORDER BY start_sec,segment_id", (asset_id,)
        )]
        duration = asset.get("duration_seconds")
        seen_windows = set()

        def append(start: float, end: float, segment_id: str | None, *, manual=False):
            window = (round(start * 1_000_000), round(end * 1_000_000))
            if window in seen_windows or (window in replaced_windows and not manual) or end <= start:
                return
            if duration is not None and end > duration + 0.001:
                return
            measured_ranges = quality_source_ranges(db, asset_id, window[0], window[1], **({"request_evidence":evidence} if evidence is not None else {}))
            for left, right in (piece for a, b in measured_ranges
                                for piece in usable_source_ranges(a, b, marks.values())):
                piece = (left, right)
                if piece in seen_windows:
                    continue
                seen_windows.add(piece)
                items.append(_public_candidate(db, asset, left / 1_000_000,
                                               right / 1_000_000, segment_id, evidence))

        marks = evidence.marks(asset_id) if evidence is not None else current_marks(db, asset_id)
        replaced_windows = {(mark["base_start_us"], mark["base_end_us"]) for mark in marks.values()}
        observations = evidence.observation_windows(asset_id) if evidence is not None else db.connection.execute(
            """SELECT DISTINCT source_start_us,source_end_us
               FROM visual_observations
               WHERE asset_id=? AND asset_fingerprint=? AND source_end_us>source_start_us
               ORDER BY source_start_us,source_end_us""",
            (asset_id, asset["quick_fingerprint"]),
        ).fetchall()
        for row in observations:
            if (row["source_start_us"], row["source_end_us"]) in replaced_windows:
                continue
            start, end = row["source_start_us"] / 1_000_000, row["source_end_us"] / 1_000_000
            containing = next((segment for segment in segments
                               if segment["start_sec"] <= start + 0.000001 and
                               segment["end_sec"] >= end - 0.000001), None)
            segment_id = None if containing is None else containing["segment_id"]
            append(start, end, segment_id)
        for mark in marks.values():
            if mark["usage_status"] != "不建议使用":
                append(mark["source_start_us"] / 1_000_000,
                       mark["source_end_us"] / 1_000_000, mark["segment_id"], manual=True)
        for row in segments:
            append(float(row["start_sec"]), float(row["end_sec"]), row["segment_id"])
        if duration is not None and duration > 0:
            append(0.0, float(duration), None)
        elif not seen_windows:
            # Keep an unprobed file visible as unavailable, rather than
            # implying that the task has no such source at all.
            items.append(_public_candidate(db, asset, 0.0, None, None, evidence))
    def readiness(candidate: dict) -> int:
        if candidate["fixedPresenter"]:
            return 0
        if candidate["analysisStatus"] == "observed" and candidate["sourceEnd"] is not None:
            return 1
        if candidate["mediaKind"] == "image":
            return 2
        if candidate["sourceEnd"] is not None:
            return 3
        return 4

    # Keep unprobed sources visible, but do not let them fill the first page
    # ahead of choices that can actually be selected in the storyboard.
    for item in items:
        evidence = observed_text_match(query,
            title=item.get("observedTitle") if item["analysisStatus"] == "observed" else "",
            description=item["description"] if item["analysisStatus"] == "observed" else "",
            tags=item.get("observedTags") if item["analysisStatus"] == "observed" else "",
            usage_status=item.get("usageStatus"))
        evidence["observationId"] = item.get("observationId")
        item["matchEvidence"] = evidence
    items.sort(key=lambda item: (0 if item["fixedPresenter"] else 1,
                                -item["matchEvidence"]["score"],
                                item.get("qualityEvidence", {}).get("penalty", 0), readiness(item)))
    locations = {asset['asset_id']: asset for asset in eligible}
    names = {row['library_id']: row['name'] for row in db.connection.execute('SELECT library_id,name FROM libraries')}
    page = items[offset:offset + limit]
    for item in page:
        location = locations.get(item['assetId'], {})
        item['libraryId'] = location.get('library_id')
        item['libraryName'] = names.get(item['libraryId'])
    _performance_metric("candidate_count", len(items))
    _performance_metric("input_count", len(eligible))
    return {"items": page, "total": len(items),
            "limit": limit, "offset": offset}
