"""Nonblocking same-direction guidance for actual adjacent storyboard shots."""

import hashlib
import json


SIMPLE_DIRECTIONS = frozenset((
    "静止", "向左", "向右", "向上", "向下", "推进", "拉远", "环绕",
))


def _edge(shot: dict, name: str) -> str | None:
    visual = shot.get("visual_analysis") or {}
    if visual.get("status") != "observed":
        return None
    direction = visual.get(name) or visual.get("cameraMotion")
    return direction if direction in SIMPLE_DIRECTIONS else None


def _pair_state(left: dict, right: dict, left_index: int) -> list:
    def identity(shot):
        visual = shot.get("visual_analysis") or {}
        identity = [
            shot.get("card_id"), shot.get("shot_revision"),
            shot.get("script_segment_id"), shot.get("asset_id"),
            shot.get("segment_id"),
            shot.get("narrative_start"), shot.get("narrative_end"),
            shot.get("resolved_source_start"), shot.get("resolved_source_end"),
            visual.get("observationId"),
        ]
        # Excel/manual marks have no observationId. Bind acceptance to their
        # own revision and directions so correcting a mark cannot reuse it.
        if visual.get("method") == "manual-segment-mark":
            identity.append([
                visual.get("segmentId"), visual.get("methodVersion"),
                visual.get("cameraMotion"), visual.get("entryMotion"),
                visual.get("exitMotion"),
            ])
        return identity
    return [left_index, identity(left), identity(right)]


def _pair_key(state: list) -> str:
    content = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def project_motion_advisories(db, task_id: str, groups: list[dict]) -> list[dict]:
    """Compare across script-group boundaries in the visible shot sequence."""
    shots = [shot for group in groups for shot in group.get("shots", [])]
    accepted = {
        row["pair_key"] for row in db.connection.execute(
            "SELECT pair_key FROM motion_pair_acceptances WHERE task_id=?", (task_id,)
        )
    }
    advisories = []
    for index, (left, right) in enumerate(zip(shots, shots[1:])):
        # Adjacent script sentences can be one uninterrupted presenter take.
        # Only a real cut needs direction comparison, including returns from B-roll.
        if (left.get("mix_form") == right.get("mix_form") == "数字人全屏"
                and left.get("asset_id") == right.get("asset_id")
                and left.get("asset_id") is not None
                and left.get("resolved_source_end") is not None
                and left.get("resolved_source_end") == right.get("resolved_source_start")):
            continue
        left_exit, right_entry = _edge(left, "exitMotion"), _edge(right, "entryMotion")
        if left_exit is None or left_exit != right_entry:
            continue
        state = _pair_state(left, right, index)
        key = _pair_key(state)
        advisories.append({
            "pairKey": key, "fromShotId": left["card_id"],
            "toShotId": right["card_id"], "fromOrdinal": index + 1,
            "toOrdinal": index + 2, "direction": left_exit,
            "accepted": key in accepted, "pairState": state,
        })
    return advisories


def accept_motion_advisory(
    db, task_id: str, pair_key: str, expected_digest: str,
    current_digest, groups,
) -> dict:
    """Accept a preference warning; storyboard acceptance remains a separate gate."""
    if not isinstance(pair_key, str) or not pair_key:
        raise ValueError("MOTION_PAIR_REQUIRED: 请指定要保留的相邻镜头")
    with db.transaction(immediate=True) as connection:
        task = connection.execute(
            "SELECT task_type FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task is None or task["task_type"] not in {"ad_mix", "digital_human"}:
            raise ValueError("MOTION_ROUTE: 当前路线的相邻镜头序列尚未就绪")
        if not expected_digest or expected_digest != current_digest():
            raise RuntimeError("STORYBOARD_CHANGED: 分镜已变化，请刷新后重新核对")
        advisory = next((item for item in project_motion_advisories(
            db, task_id, groups()
        ) if item["pairKey"] == pair_key), None)
        if advisory is None:
            raise ValueError("MOTION_PAIR_STALE: 该衔接已变化，请刷新后核对")
        connection.execute(
            """INSERT OR IGNORE INTO motion_pair_acceptances(
               task_id,pair_key,left_shot_id,right_shot_id,direction,pair_state_json
            ) VALUES(?,?,?,?,?,?)""",
            (task_id, pair_key, advisory["fromShotId"],
             advisory["toShotId"], advisory["direction"],
             json.dumps(advisory["pairState"], ensure_ascii=False, separators=(",", ":"))),
        )
    return {**advisory, "accepted": True}
