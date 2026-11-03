"""Stable script-segment identity shared by the storyboard and rough cut."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext

from .timeline_contract import TimeWindow


def create_script_segment(
    db,
    task_id: str,
    script_revision: str,
    index: int,
    text: str,
    output_start_us: int,
    output_end_us: int,
) -> dict:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("SEGMENT_TASK: 任务编号缺失")
    if not isinstance(script_revision, str) or not script_revision.strip():
        raise ValueError("SEGMENT_SCRIPT_REVISION: 文案版本缺失")
    if script_revision == "legacy":
        raise ValueError("SEGMENT_SCRIPT_REVISION: legacy 是迁移保留版本")
    if type(index) is not int or index < 0:
        raise ValueError("SEGMENT_INDEX: 文案段序号无效")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("SEGMENT_TEXT: 文案段不能为空")
    window = TimeWindow(output_start_us, output_end_us)
    digest = hashlib.sha256(
        f"{task_id}\0{script_revision}\0{index}".encode("utf-8")
    ).hexdigest()[:24]
    segment_id = f"seg-{digest}"
    transaction = nullcontext(db.connection) if db.connection.in_transaction else db.transaction(immediate=True)
    with transaction as connection:
        if connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is None:
            raise ValueError("SEGMENT_TASK: 任务不存在")
        existing = connection.execute(
            "SELECT * FROM script_segments WHERE task_id=? AND script_revision=? AND idx=?",
            (task_id, script_revision, index),
        ).fetchone()
        if existing is not None:
            if (existing["text"], existing["output_start_us"], existing["output_end_us"]) != (
                text, window.start_us, window.end_us,
            ):
                raise ValueError("SEGMENT_CONFLICT: 同一文案版本的段内容或时间已变化")
            return dict(existing)
        # A confirmed rework can return to identical copy with a different clock.
        # Keep historical IDs (including reshoot references) immutable.
        if connection.execute("SELECT 1 FROM script_segments WHERE segment_id=?", (segment_id,)).fetchone():
            segment_id = f"seg-{uuid.uuid4().hex[:24]}"
        connection.execute(
            "INSERT INTO script_segments(segment_id,task_id,script_revision,idx,text,output_start_us,output_end_us) "
            "VALUES(?,?,?,?,?,?,?)",
            (segment_id, task_id, script_revision, index, text, window.start_us, window.end_us),
        )
        return dict(connection.execute(
            "SELECT * FROM script_segments WHERE segment_id=?", (segment_id,)
        ).fetchone())


def list_script_segments(db, task_id: str, script_revision: str | None = None) -> list[dict]:
    if script_revision is None:
        rows = db.connection.execute(
            "SELECT * FROM script_segments WHERE task_id=? ORDER BY script_revision,idx",
            (task_id,),
        )
    else:
        rows = db.connection.execute(
            "SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx",
            (task_id, script_revision),
        )
    return [dict(row) for row in rows]


def project_script_shots(db, task_id: str, shots: list[dict]) -> dict:
    """Project the current script and its shots without inventing missing links."""
    task = db.connection.execute(
        "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if task is None:
        raise KeyError(f"SEGMENT_TASK: 任务不存在：{task_id}")
    try:
        brief = json.loads(task["brief_json"] or "{}")
        expected = (brief.get("final_script") or {}).get("sha256")
    except (ValueError, AttributeError):
        expected = None
    all_segments = list_script_segments(db, task_id)
    revisions = {segment["script_revision"] for segment in all_segments}
    if isinstance(expected, str) and expected in revisions:
        revision, status = expected, "current"
    elif "legacy" in revisions:
        revision, status = "legacy", "legacy_unverified"
    elif expected and all_segments:
        revision, status = expected, "stale"
    elif all_segments:
        latest = db.connection.execute(
            "SELECT script_revision FROM script_segments WHERE task_id=? "
            "ORDER BY rowid DESC LIMIT 1", (task_id,),
        ).fetchone()
        revision, status = latest["script_revision"], "current"
    else:
        revision, status = expected, "not_generated"
    selected = sorted(
        (segment for segment in all_segments if segment["script_revision"] == revision),
        key=lambda segment: (segment["output_start_us"], segment["idx"], segment["segment_id"]),
    ) if status != "stale" else []
    grouped = {segment["segment_id"]: [] for segment in selected}
    unassigned = []
    for shot in shots:
        segment_shots = grouped.get(shot.get("script_segment_id"))
        if segment_shots is None:
            unassigned.append(shot["card_id"])
        else:
            segment_shots.append(shot)
    return {
        "scriptRevision": revision,
        "status": status,
        "groups": [
            {**segment, "shots": grouped[segment["segment_id"]]}
            for segment in selected
        ],
        "unassignedShotIds": unassigned,
    }


def storyboard_digest(db, task_id: str) -> str | None:
    """Digest the current script/shot decision, excluding display-only media URLs."""
    shots = [dict(row) for row in db.connection.execute(
        "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id", (task_id,)
    )]
    projection = project_script_shots(db, task_id, shots)
    if projection["status"] != "current" or not projection["groups"]:
        return None
    card_fields = (
        "card_id", "script_segment_id", "shot_revision", "idx", "narrative_text",
        "narrative_start", "narrative_end", "asset_id", "segment_id", "mix_form",
        "enter_tc", "exit_tc", "audio_policy_draft", "source_type", "final_decision",
    )
    asset_ids = sorted({shot["asset_id"] for shot in shots if shot["asset_id"]})
    assets = []
    for asset_id in asset_ids:
        row = db.connection.execute(
            "SELECT a.asset_id,a.media_kind,a.size_bytes,a.quick_fingerprint,a.full_sha256,"
            "probe.status AS probe_status,probe.tool_version AS probe_version,"
            "probe.result_json AS probe_result "
            "FROM assets a LEFT JOIN analysis_runs probe "
            "ON probe.asset_id=a.asset_id AND probe.stage='technical_probe' "
            "WHERE a.asset_id=?", (asset_id,),
        ).fetchone()
        assets.append(None if row is None else dict(row))
    payload = {
        "script_revision": projection["scriptRevision"],
        "asset_versions": assets,
        "groups": [
            {
                "segment_id": group["segment_id"],
                "text": group["text"],
                "start_us": group["output_start_us"],
                "end_us": group["output_end_us"],
                "shots": [
                    {key: shot[key] for key in card_fields} for shot in group["shots"]
                ],
            }
            for group in projection["groups"]
        ],
        "unassigned_shots": projection["unassignedShotIds"],
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def archive_script_segments(connection, task_id: str, rework_id: str) -> None:
    """Move active timing out of the current revision after an explicit rework.

    Call in the rework transaction after archiving the full rows and deleting
    current scene cards. Historical requirements keep their original segment IDs.
    """
    if not connection.in_transaction:
        raise RuntimeError("Segment archive requires the rework transaction")
    connection.execute(
        "UPDATE reshoot_requirements SET active=0,script_revision='archived:' || ? || ':' || script_revision "
        "WHERE task_id=? AND script_revision NOT LIKE 'archived:%'", (rework_id, task_id))
    connection.execute("DELETE FROM task_reshoot_current WHERE task_id=?", (task_id,))
    connection.execute(
        "UPDATE script_segments SET script_revision='archived:' || ? || ':' || script_revision "
        "WHERE task_id=? AND script_revision NOT LIKE 'archived:%'", (rework_id, task_id))
