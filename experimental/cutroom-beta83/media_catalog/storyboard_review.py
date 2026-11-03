"""Explicit user acceptance of one exact storyboard revision."""

import json
from pathlib import Path

from .rough_media import assert_picture_media_available
from .script_segments import project_script_shots, storyboard_digest
from .storyboard_edits import _validate


def accept_storyboard(db, task_id: str, expected_digest: str, *, pre_accept_check=None) -> dict:
    if not isinstance(expected_digest, str) or not expected_digest:
        raise ValueError("STORYBOARD_DIGEST_REQUIRED: 请刷新分镜后再确认")
    with db.transaction(immediate=True) as connection:
        task = connection.execute(
            "SELECT task_type,brief_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise KeyError(f"任务不存在：{task_id}")
        if task["task_type"] not in {"ad_mix", "digital_human"}:
            raise ValueError("STORYBOARD_ROUTE: 当前路线不支持分镜确认")
        digest = storyboard_digest(db, task_id)
        if digest != expected_digest:
            raise RuntimeError("STORYBOARD_CHANGED: 分镜在您查看后已变化，请刷新核对")
        cards = [dict(row) for row in connection.execute(
            "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id", (task_id,)
        )]
        projection = project_script_shots(db, task_id, cards)
        if projection["status"] != "current" or not projection["groups"]:
            raise ValueError("STORYBOARD_STALE: 尚无当前锁定文案的分镜")
        if projection["unassignedShotIds"] or any(
            not group["shots"] for group in projection["groups"]
        ):
            raise ValueError("STORYBOARD_COVERAGE: 每段文案需要完整的镜头安排")
        if any(not shot["asset_id"] for shot in cards):
            raise ValueError("STORYBOARD_GAP: 尚有画面素材缺口，不能生成粗剪")
        assets = [dict(row) for row in connection.execute(
            "SELECT task_id,asset_id,role,created_at FROM task_assets WHERE task_id=?",
            (task_id,),
        )]
        _validate(db, task_id, {"cards": cards, "task_assets": assets})
        # A recommendation may have been generated while a removable library
        # was online. Recheck the selected files at the approval boundary.
        assert_picture_media_available(db, task_id)
        if task["task_type"] == "digital_human":
            try:
                brief = json.loads(task["brief_json"] or "{}")
            except (ValueError, TypeError):
                brief = {}
            source = brief.get("digital_human_source") if isinstance(brief, dict) else None
            if isinstance(source, dict) and all(source.get(key) is not None for key in
                                                ("path", "size_bytes", "mtime_ns", "quick_fingerprint")):
                from .digital_workflow import _default_file_snapshot, _source_snapshot_identity
                try:
                    current_source = _default_file_snapshot(Path(source["path"]))
                    same_source = (_source_snapshot_identity(current_source)
                                   == _source_snapshot_identity(source))
                except (OSError, ValueError, TypeError, KeyError):
                    same_source = False
                if not same_source:
                    raise RuntimeError("SOURCE_CHANGED: 口播底片已变化，请重新核对文案和分镜")
        stage = connection.execute(
            "SELECT detail_json FROM task_stage_states WHERE task_id=? AND stage_key='ai_storyboard'",
            (task_id,),
        ).fetchone()
        if stage is None or digest is None:
            raise ValueError("STORYBOARD_STALE: 分镜阶段尚未就绪")
        try:
            current = json.loads(stage["detail_json"] or "{}")
        except (ValueError, TypeError):
            current = {}
        if pre_accept_check is not None:
            pre_accept_check(connection)
        details = current.get("details") if isinstance(current, dict) else None
        if isinstance(details, dict) and details.get("storyboard_accepted_digest") == digest:
            return {"status": "accepted", "storyboardDigest": digest, "reused": True}
        detail_json = json.dumps(
            {"input_digest": digest, "details": {"storyboard_accepted_digest": digest}},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        connection.execute(
            "UPDATE task_stage_states SET status='completed',detail_json=?,updated_at=datetime('now') "
            "WHERE task_id=? AND stage_key='ai_storyboard'",
            (detail_json, task_id),
        )
        connection.execute(
            "UPDATE task_stage_states SET status='ready',updated_at=datetime('now') "
            "WHERE task_id=? AND stage_key='rough_cut' AND status IN ('not_started','invalidated')",
            (task_id,),
        )
        return {"status": "accepted", "storyboardDigest": digest, "reused": False}
