"""Versioned ad storyboard edits over the authoritative scene-card ledger."""

from __future__ import annotations

import copy
import json
import math
import uuid
from pathlib import Path

from .ad_matching import TRANSITION_HANDLE_SECONDS
from .asset_index import _duration_seconds
from .rough_cut import (
    _CARD_COLUMNS, _TASK_ASSET_COLUMNS, _allowed_asset, _snapshot_state,
    _source_window, _validate_visual_binding,
)
from .script_segments import project_script_shots, storyboard_digest
from .rough_media import list_rough_edit_assets
from .timeline_contract import TimeWindow, seconds_to_us, validate_coverage
from .workflow import project_workflow


MIN_SHOT_US = 500_000


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _matches_snapshot(saved_json: str, current: dict) -> bool:
    try:
        saved = json.loads(saved_json)
        assets = saved["task_assets"]
        if not isinstance(assets, list):
            return False
        assets.sort(key=lambda asset: asset["asset_id"])
    except (KeyError, TypeError, ValueError):
        return False
    return _canonical(saved) == _canonical(current)


def _number(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"STORYBOARD_TIME: {label}必须是有限数字")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"STORYBOARD_TIME: {label}必须是有限数字") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"STORYBOARD_TIME: {label}必须是非负有限数字")
    return result


def _duration_limit(connection, asset_id: str, segment_id: str | None) -> tuple[float, float]:
    if segment_id:
        from .segment_marks import resolve_source_segment
        try:
            row = resolve_source_segment(connection, asset_id, segment_id)
        except KeyError as error:
            raise ValueError("STORYBOARD_SOURCE: 素材片段不属于所选素材") from error
        return float(row["start_sec"]), float(row["end_sec"])
    probe = connection.execute(
        "SELECT result_json FROM analysis_runs WHERE asset_id=? "
        "AND stage='technical_probe' AND status='completed'", (asset_id,),
    ).fetchone()
    duration = _duration_seconds(None if probe is None else probe["result_json"])
    if duration is None or duration <= 0:
        raise ValueError("STORYBOARD_PROBE: 视频尚无可核验时长，请先完成技术探测")
    return 0.0, duration


def _set_source(connection, card: dict, *, source_start=None, source_end=None) -> None:
    if card.get("mix_form") == "数字人全屏":
        card["enter_tc"] = card["exit_tc"] = 0.0
        return
    asset_id = card.get("asset_id")
    if not asset_id:
        card["enter_tc"] = card["exit_tc"] = None
        return
    kind_row = connection.execute(
        "SELECT media_kind FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if kind_row is None:
        raise ValueError("STORYBOARD_ASSET: 素材未入册")
    if kind_row["media_kind"] == "image":
        card["enter_tc"] = card["exit_tc"] = 0.0
        return
    if kind_row["media_kind"] != "video":
        raise ValueError("STORYBOARD_ASSET: 只能选择视频或照片")
    lower, upper = _duration_limit(connection, asset_id, card.get("segment_id"))
    length = (card["narrative_end"] - card["narrative_start"])
    start = lower if source_start is None else _number(source_start, "取用起点")
    end = start + length if source_end is None else _number(source_end, "取用终点")
    task = connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (card["task_id"],)
    ).fetchone()
    handle = TRANSITION_HANDLE_SECONDS if task["task_type"] == "ad_mix" else 0.0
    if start < lower - 1e-6 or end + handle > upper + 1e-6 or end <= start:
        raise ValueError("STORYBOARD_SOURCE: 取用区间及转场真实画面越过实际可用来源窗口")
    if abs((end - start) - length) > 1e-5:
        raise ValueError("STORYBOARD_SOURCE: 取用时长必须等于镜头展示时长")
    card["enter_tc"], card["exit_tc"] = start, end


def _groups(db, task_id: str, state: dict) -> dict:
    projection = project_script_shots(db, task_id, state["cards"])
    if projection["status"] != "current" or not projection["groups"]:
        raise ValueError("STORYBOARD_STALE: 尚无当前文案分镜")
    if projection["unassignedShotIds"]:
        raise ValueError("STORYBOARD_COVERAGE: 有未归属当前文案的镜头")
    return {group["segment_id"]: group for group in projection["groups"]}


def _validate(db, task_id: str, state: dict) -> None:
    groups = _groups(db, task_id, state)
    task = db.connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (task_id,),
    ).fetchone()
    digital = task is not None and task["task_type"] == "digital_human"
    roles = {asset["asset_id"]: asset["role"] for asset in state["task_assets"]}
    ids = [card["card_id"] for card in state["cards"]]
    if len(ids) != len(set(ids)):
        raise ValueError("STORYBOARD_SHOT: 镜头编号重复")
    for group in groups.values():
        shots = group["shots"]
        if not shots:
            raise ValueError("STORYBOARD_COVERAGE: 每段文案至少保留一个镜头")
        windows = [TimeWindow(seconds_to_us(shot["narrative_start"]),
                              seconds_to_us(shot["narrative_end"])) for shot in shots]
        validate_coverage(windows, TimeWindow(group["output_start_us"], group["output_end_us"]))
        if any(shot["narrative_text"] != group["text"] for shot in shots):
            raise ValueError("STORYBOARD_TEXT: 镜头文案与当前段不一致")
        for shot in shots:
            asset_id = shot.get("asset_id")
            if not asset_id:
                continue  # A visible gap may be filled before confirming this stage.
            if digital and shot["mix_form"] == "数字人全屏":
                if (roles.get(asset_id) != "digital_human"
                        or shot.get("segment_id")
                        or shot.get("enter_tc") != 0
                        or shot.get("exit_tc") != 0
                        or shot.get("audio_policy_draft") != "keep"):
                    raise ValueError("STORYBOARD_DIGITAL_ANCHOR: 数字人口播主画面必须保持原声和固定源时间")
                _source_window(db.connection, state, shot)
                continue
            if digital and shot.get("audio_policy_draft") != "mute":
                raise ValueError("STORYBOARD_DIGITAL_AUDIO: 补充画面必须静音并保留主口播")
            row = db.connection.execute(
                "SELECT media_kind FROM assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            if row is None:
                raise ValueError("STORYBOARD_ASSET: 镜头素材不存在")
            _validate_visual_binding(state, asset_id, row["media_kind"], shot["mix_form"])
            _source_window(db.connection, state, shot)
            if row["media_kind"] == "video" and not digital:
                # The existing rough-cut check covers segment bounds; this also
                # requires a real technical duration for unsegmented video.
                lower, upper = _duration_limit(db.connection, asset_id, shot.get("segment_id"))
                source_start = float(shot["enter_tc"] or lower)
                source_end = float(shot["exit_tc"] or source_start + (
                    shot["narrative_end"] - shot["narrative_start"]
                ))
                if source_start < lower - 1e-6 or source_end + TRANSITION_HANDLE_SECONDS > upper + 1e-6:
                    raise ValueError("STORYBOARD_SOURCE: 镜头及转场真实画面越过实际可用来源窗口")


def _write_state(connection, task_id: str, state: dict) -> None:
    current_cards = {row["card_id"] for row in connection.execute(
        "SELECT card_id FROM scene_cards WHERE task_id=?", (task_id,)
    )}
    desired_cards = {card["card_id"] for card in state["cards"]}
    for card_id in current_cards - desired_cards:
        connection.execute("DELETE FROM scene_cards WHERE task_id=? AND card_id=?", (task_id, card_id))
    current_assets = {row["asset_id"] for row in connection.execute(
        "SELECT asset_id FROM task_assets WHERE task_id=?", (task_id,)
    )}
    desired_assets = {asset["asset_id"] for asset in state["task_assets"]}
    for asset_id in current_assets - desired_assets:
        connection.execute("DELETE FROM task_assets WHERE task_id=? AND asset_id=?", (task_id, asset_id))
    for asset in state["task_assets"]:
        if asset["asset_id"] in current_assets:
            connection.execute(
                "UPDATE task_assets SET role=?,created_at=? WHERE task_id=? AND asset_id=?",
                (asset["role"], asset["created_at"], task_id, asset["asset_id"]),
            )
        else:
            connection.execute(
                f"INSERT INTO task_assets({','.join(_TASK_ASSET_COLUMNS)}) VALUES({','.join('?' for _ in _TASK_ASSET_COLUMNS)})",
                tuple(asset[column] for column in _TASK_ASSET_COLUMNS),
            )
    for card in state["cards"]:
        if card["card_id"] in current_cards:
            columns = tuple(key for key in _CARD_COLUMNS if key not in {"card_id", "task_id", "created_at"})
            connection.execute(
                f"UPDATE scene_cards SET {','.join(key + '=?' for key in columns)} "
                "WHERE task_id=? AND card_id=?",
                (*[card[key] for key in columns], task_id, card["card_id"]),
            )
        else:
            connection.execute(
                f"INSERT INTO scene_cards({','.join(_CARD_COLUMNS)}) VALUES({','.join('?' for _ in _CARD_COLUMNS)})",
                tuple(card[column] for column in _CARD_COLUMNS),
            )
    for feedback in state.get("removed_feedback", []):
        columns = ("feedback_id", "task_id", "card_id", "decision", "instruction",
                   "original_asset_id", "selected_asset_id", "candidates_json",
                   "created_at", "applied_at")
        connection.execute(
            f"INSERT OR IGNORE INTO storyboard_feedback({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            tuple(feedback[key] for key in columns),
        )


def _reallocate(connection, shots: list[dict], group: dict) -> None:
    span = group["output_end_us"] - group["output_start_us"]
    if span // len(shots) < MIN_SHOT_US:
        raise ValueError("STORYBOARD_MIN_SHOT: 每镜至少需要 0.5 秒")
    for index, card in enumerate(shots):
        old_source = card.get("enter_tc")
        start_us = group["output_start_us"] + span * index // len(shots)
        end_us = group["output_start_us"] + span * (index + 1) // len(shots)
        card["narrative_start"] = start_us / 1_000_000
        card["narrative_end"] = end_us / 1_000_000
        _set_source(connection, card, source_start=old_source)


def _candidate(db, task_id: str, state: dict, card: dict, asset_id: str,
               source_segment_id: str | None, source_start) -> None:
    if not asset_id:
        raise ValueError("STORYBOARD_ASSET: 请选择一个镜头素材")
    asset = _allowed_asset(db, task_id, asset_id)
    available = next((item for item in list_rough_edit_assets(db, task_id)
                      if item["asset_id"] == asset_id), None)
    if available is None:
        raise RuntimeError("STORYBOARD_OFFLINE: 所选素材不在当前在线任务范围")
    library = db.connection.execute(
        "SELECT mount_path FROM libraries WHERE library_id=? AND online=1",
        (available["library_id"],),
    ).fetchone()
    if library is None:
        raise RuntimeError("STORYBOARD_OFFLINE: 所选素材目录不可访问")
    root = Path(library["mount_path"]).resolve()
    source = (root / available["relative_path"]).resolve()
    if root not in source.parents or not source.is_file():
        raise RuntimeError("STORYBOARD_OFFLINE: 所选素材文件当前不可访问")
    if asset["media_kind"] not in {"video", "image"}:
        raise ValueError("STORYBOARD_ASSET: 只能选择视频或照片")
    task = db.connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (task_id,),
    ).fetchone()
    digital = task["task_type"] == "digital_human"
    existing = next((item for item in state["task_assets"] if item["asset_id"] == asset_id), None)
    if digital and existing is not None and existing["role"] == "digital_human":
        if source_segment_id is not None or source_start is not None:
            raise ValueError("STORYBOARD_DIGITAL_ANCHOR: 口播原画面的源时间由原声音画固定对齐")
        card.update(asset_id=asset_id, segment_id=None, mix_form="数字人全屏",
                    audio_policy_draft="keep", source_type="用户指定",
                    final_decision="用户在分镜页恢复口播原画面")
        _set_source(db.connection, card)
        return
    mix_form = "画中画" if digital and asset["media_kind"] == "image" else "broll全屏"
    role = _validate_visual_binding(
        state, asset_id, asset["media_kind"], mix_form, allow_unregistered=True,
    )
    if existing is None:
        state["task_assets"].append({
            "task_id": task_id, "asset_id": asset_id, "role": role,
            "created_at": db.connection.execute("SELECT datetime('now')").fetchone()[0],
        })
    card.update(asset_id=asset_id, segment_id=source_segment_id, mix_form=mix_form,
                audio_policy_draft="mute" if digital else "replace",
                source_type="用户指定", final_decision="用户在分镜页选择")
    _set_source(db.connection, card, source_start=source_start)


def _editable(db, task_id: str, expected_digest: str) -> None:
    if not isinstance(expected_digest, str) or not expected_digest:
        raise ValueError("STORYBOARD_DIGEST_REQUIRED: 请刷新分镜后再编辑")
    task = db.connection.execute(
        "SELECT task_type FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    if task["task_type"] not in {"ad_mix", "digital_human"}:
        raise ValueError("STORYBOARD_ROUTE: 当前路线不支持镜头编辑")
    from .rough_cut import _required_ad_ratio

    _required_ad_ratio(db.connection, task_id)
    if db.connection.execute("SELECT 1 FROM picture_lock WHERE task_id=?", (task_id,)).fetchone():
        raise RuntimeError("STORYBOARD_LOCKED: 画面已锁定，请从粗剪修改")
    stage=project_workflow(db, task_id)["current_stage"]
    if stage == 'material_sync':
        from .task_sources import require_source_readiness
        require_source_readiness(db,task_id)
    elif stage != 'ai_storyboard':
        raise RuntimeError("STORYBOARD_STAGE: 当前不在分镜编辑阶段")
    if storyboard_digest(db, task_id) != expected_digest:
        raise RuntimeError("STORYBOARD_CHANGED: 分镜在您查看后已变化，请刷新核对")


def apply_storyboard_edit(db, task_id: str, expected_digest: str, edit: dict) -> dict:
    if not isinstance(edit, dict) or edit.get("action") not in {"add", "delete", "replace", "trim"}:
        raise ValueError("STORYBOARD_ACTION: 未知镜头操作")
    action = edit["action"]
    with db.transaction(immediate=True) as connection:
        _editable(db, task_id, expected_digest)
        before = _snapshot_state(connection, task_id)
        after = copy.deepcopy(before)
        groups = _groups(db, task_id, after)
        shot_id = edit.get("shotId")
        chosen = next((card for card in after["cards"] if card["card_id"] == shot_id), None)
        if action == "add":
            group = groups.get(edit.get("scriptSegmentId"))
            if group is None:
                raise ValueError("STORYBOARD_SEGMENT: 文案段不存在")
            shots = group["shots"]
            new_card = copy.deepcopy(shots[-1])
            new_card.update(card_id=f"shot-{uuid.uuid4().hex}", shot_revision=1,
                            asset_id=None, segment_id=None, enter_tc=None, exit_tc=None,
                            source_type="用户指定", system_suggestion=None,
                            final_decision=None, confirm_status=0)
            shots.append(new_card)
            _reallocate(connection, shots, group)
            _candidate(db, task_id, after, new_card, str(edit.get("assetId") or ""),
                       edit.get("sourceSegmentId"), edit.get("sourceStart"))
            after["cards"].append(new_card)
        else:
            if chosen is None:
                raise ValueError("STORYBOARD_SHOT: 当前镜头不存在")
            group = groups[chosen["script_segment_id"]]
            shots = group["shots"]
            if action == "delete":
                if len(shots) <= 1:
                    raise ValueError("STORYBOARD_LAST_SHOT: 每段文案至少保留一个镜头")
                before["removed_feedback"] = [dict(row) for row in connection.execute(
                    "SELECT * FROM storyboard_feedback WHERE task_id=? AND card_id=?",
                    (task_id, chosen["card_id"]),
                )]
                shots.remove(chosen)
                after["cards"].remove(chosen)
                _reallocate(connection, shots, group)
            elif action == "replace":
                _candidate(db, task_id, after, chosen, str(edit.get("assetId") or ""),
                           edit.get("sourceSegmentId"), edit.get("sourceStart"))
            else:
                if not chosen.get("asset_id"):
                    raise ValueError("STORYBOARD_GAP: 缺素材镜头需先更换")
                if chosen["mix_form"] == "数字人全屏":
                    raise ValueError("STORYBOARD_DIGITAL_ANCHOR: 口播主画面源时间由原声音画固定对齐")
                start = _number(edit.get("sourceStart"), "取用起点")
                end = _number(edit.get("sourceEnd"), "取用终点")
                _set_source(connection, chosen, source_start=start, source_end=end)
        # Snapshots read task assets in asset-id order. A newly selected asset
        # can sort before existing ones; keep the saved edit snapshot canonical
        # so the immediate undo still matches the database after commit.
        after["task_assets"].sort(key=lambda asset: asset["asset_id"])
        old_cards = {card["card_id"]: card for card in before["cards"]}
        after["cards"].sort(key=lambda card: (card["narrative_start"], card["narrative_end"], card["card_id"]))
        now = connection.execute("SELECT datetime('now')").fetchone()[0]
        for index, card in enumerate(after["cards"]):
            card["idx"] = index
            old = old_cards.get(card["card_id"])
            if old is None:
                card["created_at"] = now
                card["updated_at"] = now
            elif any(card[key] != old[key] for key in _CARD_COLUMNS if key not in {"updated_at", "shot_revision"}):
                card["shot_revision"] = old["shot_revision"] + 1
                card["updated_at"] = now
        _validate(db, task_id, after)
        if _canonical(after) == _canonical(before):
            raise ValueError("STORYBOARD_NO_CHANGE: 镜头没有变化")
        _write_state(connection, task_id, after)
        edit_id = f"sbedit-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO storyboard_edits(edit_id,task_id,before_json,after_json,status) "
            "VALUES(?,?,?,?,'applied')",
            (edit_id, task_id, _canonical(before), _canonical(after)),
        )
        connection.execute("UPDATE tasks SET updated_at=datetime('now') WHERE task_id=?", (task_id,))
        return {"editId": edit_id, "action": action, "storyboardDigest": storyboard_digest(db, task_id),
                "shotId": new_card["card_id"] if action == "add" else shot_id}


def undo_storyboard_edit(db, task_id: str, expected_digest: str) -> dict:
    with db.transaction(immediate=True) as connection:
        _editable(db, task_id, expected_digest)
        current = _snapshot_state(connection, task_id)
        row = connection.execute(
            "SELECT * FROM storyboard_edits WHERE task_id=? AND status='applied' "
            "ORDER BY rowid DESC LIMIT 1", (task_id,),
        ).fetchone()
        if row is None or not _matches_snapshot(row["after_json"], current):
            raise RuntimeError("STORYBOARD_UNDO: 没有可撤销的当前镜头修改")
        before = json.loads(row["before_json"])
        _validate(db, task_id, before)
        _write_state(connection, task_id, before)
        connection.execute(
            "UPDATE storyboard_edits SET status='reverted',updated_at=datetime('now') WHERE edit_id=?",
            (row["edit_id"],),
        )
        connection.execute("UPDATE tasks SET updated_at=datetime('now') WHERE task_id=?", (task_id,))
        return {"editId": row["edit_id"], "status": "reverted",
                "storyboardDigest": storyboard_digest(db, task_id)}


def can_undo_storyboard(db, task_id: str) -> bool:
    row = db.connection.execute(
        "SELECT after_json FROM storyboard_edits WHERE task_id=? AND status='applied' "
        "ORDER BY rowid DESC LIMIT 1", (task_id,),
    ).fetchone()
    return row is not None and _matches_snapshot(
        row["after_json"], _snapshot_state(db.connection, task_id)
    )
