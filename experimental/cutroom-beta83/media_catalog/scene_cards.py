"""P2 场景卡片 + 画面锁定。

场景卡片 = 设计文档里"文案—事实—素材"映射的可查记录：每张卡片记一段旁白、
用到的素材/片段、混剪形态（数字人全屏/B-roll全屏/画中画/信息卡）、进入退出时间、
原声策略草稿、来源类型、系统建议与最终决定、确认状态。

画面锁定 = 用户确认"画面与顺序"后落锁；之后若再改卡片，标记 changed_since_lock，
强制系统说明会重跑哪些环节（字幕/声音/包装/质检）。

四道人工门禁的"画面与顺序锁定"由本模块的 picture_lock 承载；
其余三道（文案锁定=brief_locked、包装确认=packaging_confirmed、完整观看=reviewed）
复用 tasks.status。
"""

from collections.abc import Callable
from contextlib import nullcontext
import math
from pathlib import Path

from .provenance import canonical_digest, latest_gate, record_gate, selection_digest
from .timeline_contract import (
    TimeWindow, TimelineContractError, seconds_to_us, validate_coverage,
)
from .visual_uniqueness import validate_task_scene_uniqueness

VALID_MIX_FORMS = (
    "数字人全屏",
    "broll全屏",
    "画中画",
    "信息卡",
)
VALID_AUDIO_POLICY = ("keep", "mute", "duck", "clean", "replace")
VALID_SOURCE_TYPES = ("用户指定", "系统推荐", "长期库", "缺口替代")


class RoughNarrationUnavailable(RuntimeError):
    code = "ROUGH_NARRATION_UNAVAILABLE"


def _finite_time(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} 必须是有限数字")
    if value < 0:
        raise ValueError(f"{label} 不能为负数")
    return float(value)


def _validate_time_window(
    start: float | None,
    end: float | None,
    label: str,
    *,
    allow_zero_zero: bool = False,
    require_complete: bool = False,
) -> None:
    start = _finite_time(start, f"{label}开始时间")
    end = _finite_time(end, f"{label}结束时间")
    if start is None or end is None:
        if require_complete and (start is not None or end is not None):
            raise ValueError(f"{label}时间窗必须同时提供开始和结束时间")
        return
    if allow_zero_zero and start == end == 0:
        return
    if end <= start:
        raise ValueError(f"{label}时间顺序无效：结束时间必须晚于开始时间")


def _reject_locked_legacy_write(db, task_id: str) -> None:
    if db.connection.execute(
        "SELECT 1 FROM picture_lock WHERE task_id=?", (task_id,)
    ).fetchone() is not None:
        raise RuntimeError(
            "IMPACT_PREVIEW_REQUIRED: 画面锁定后请走 "
            "media_catalog.rough_cut.preview_edit_impact/apply_edit"
        )


def _card_id(task_id: str, idx: int) -> str:
    return f"{task_id}#{idx}"


def add_scene_card(
    db,
    task_id: str,
    idx: int,
    narrative_text: str | None = None,
    narrative_start: float | None = None,
    narrative_end: float | None = None,
    asset_id: str | None = None,
    segment_id: str | None = None,
    mix_form: str | None = None,
    enter_tc: float | None = None,
    exit_tc: float | None = None,
    audio_policy_draft: str | None = None,
    source_type: str | None = None,
    system_suggestion: str | None = None,
    final_decision: str | None = None,
    shot_id: str | None = None,
    script_segment_id: str | None = None,
) -> dict:
    if shot_id is not None and (not isinstance(shot_id, str) or not shot_id.strip()):
        raise ValueError("SHOT_ID: 镜头编号不能为空")
    card_id = shot_id if shot_id is not None else _card_id(task_id, idx)
    existing = get_scene_card(db, card_id)
    if existing is not None:
        if existing["task_id"] != task_id:
            raise ValueError("SHOT_TASK: 镜头编号已属于另一任务")
        _reject_locked_legacy_write(db, task_id)
    next_narrative_start = (
        narrative_start if narrative_start is not None
        else None if existing is None else existing["narrative_start"]
    )
    next_narrative_end = (
        narrative_end if narrative_end is not None
        else None if existing is None else existing["narrative_end"]
    )
    next_enter = enter_tc if enter_tc is not None else None if existing is None else existing["enter_tc"]
    next_exit = exit_tc if exit_tc is not None else None if existing is None else existing["exit_tc"]
    _validate_time_window(
        next_narrative_start, next_narrative_end, "叙事", require_complete=True
    )
    _validate_time_window(
        next_enter, next_exit, "源", allow_zero_zero=True, require_complete=True
    )
    next_script_segment_id = script_segment_id if script_segment_id is not None else (
        None if existing is None else existing["script_segment_id"]
    )
    if next_script_segment_id is not None:
        script_segment = db.connection.execute(
            "SELECT * FROM script_segments WHERE segment_id=?",
            (next_script_segment_id,),
        ).fetchone()
        if script_segment is None or script_segment["task_id"] != task_id:
            raise ValueError("SEGMENT_TASK: 文案段不存在或不属于该任务")
        next_text = narrative_text if narrative_text is not None else (
            None if existing is None else existing["narrative_text"]
        )
        if next_text is not None and next_text != script_segment["text"]:
            raise ValueError("SEGMENT_TEXT: 镜头文案必须与所属文案段一致")
        if next_narrative_start is None or next_narrative_end is None:
            raise ValueError("SEGMENT_WINDOW: 镜头必须有完整的输出时间窗")
        if (seconds_to_us(next_narrative_start) < script_segment["output_start_us"]
                or seconds_to_us(next_narrative_end) > script_segment["output_end_us"]):
            raise ValueError("SEGMENT_WINDOW: 镜头输出时间超出文案段")
        if narrative_text is None and next_text is None:
            narrative_text = script_segment["text"]
    if mix_form is not None and mix_form not in VALID_MIX_FORMS:
        raise ValueError(f"混剪形态必须是 {VALID_MIX_FORMS} 之一")
    if audio_policy_draft is not None and audio_policy_draft not in VALID_AUDIO_POLICY:
        raise ValueError(f"原声策略必须是 {VALID_AUDIO_POLICY} 之一")
    if source_type is not None and source_type not in VALID_SOURCE_TYPES:
        raise ValueError(f"来源类型必须是 {VALID_SOURCE_TYPES} 之一")
    from .rough_cut import _required_ad_ratio, _validate_ad_asset_direction

    _required_ad_ratio(db.connection, task_id)
    effective_asset_id = asset_id if asset_id is not None else (
        existing["asset_id"] if existing is not None else None
    )
    if effective_asset_id is not None:
        row = db.connection.execute(
            "SELECT media_kind FROM assets WHERE asset_id=?", (effective_asset_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"素材未入册，不能挂到场景卡片：{effective_asset_id}")
        _validate_ad_asset_direction(
            db.connection, task_id, effective_asset_id, row["media_kind"],
        )
    supplied = {
        "narrative_text": narrative_text,
        "narrative_start": narrative_start,
        "narrative_end": narrative_end,
        "asset_id": asset_id,
        "segment_id": segment_id,
        "mix_form": mix_form,
        "enter_tc": enter_tc,
        "exit_tc": exit_tc,
        "audio_policy_draft": audio_policy_draft,
        "source_type": source_type,
        "system_suggestion": system_suggestion,
        "final_decision": final_decision,
        "script_segment_id": script_segment_id,
    }
    if existing is not None and existing["idx"] == idx and all(
        value is None or existing[column] == value for column, value in supplied.items()
    ):
        return existing
    transaction = nullcontext(db.connection) if db.connection.in_transaction else db.transaction()
    with transaction as connection:
        connection.execute(
            """
            INSERT INTO scene_cards(
              card_id, task_id, idx, narrative_text, narrative_start, narrative_end,
              asset_id, segment_id, mix_form, enter_tc, exit_tc,
              audio_policy_draft, source_type, system_suggestion, final_decision,
              script_segment_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(card_id) DO UPDATE SET
              idx=excluded.idx,
              narrative_text=COALESCE(excluded.narrative_text, scene_cards.narrative_text),
              narrative_start=COALESCE(excluded.narrative_start, scene_cards.narrative_start),
              narrative_end=COALESCE(excluded.narrative_end, scene_cards.narrative_end),
              asset_id=COALESCE(excluded.asset_id, scene_cards.asset_id),
              segment_id=COALESCE(excluded.segment_id, scene_cards.segment_id),
              mix_form=COALESCE(excluded.mix_form, scene_cards.mix_form),
              enter_tc=COALESCE(excluded.enter_tc, scene_cards.enter_tc),
              exit_tc=COALESCE(excluded.exit_tc, scene_cards.exit_tc),
              audio_policy_draft=COALESCE(excluded.audio_policy_draft, scene_cards.audio_policy_draft),
              source_type=COALESCE(excluded.source_type, scene_cards.source_type),
              system_suggestion=COALESCE(excluded.system_suggestion, scene_cards.system_suggestion),
              final_decision=COALESCE(excluded.final_decision, scene_cards.final_decision),
              script_segment_id=COALESCE(excluded.script_segment_id, scene_cards.script_segment_id),
              shot_revision=scene_cards.shot_revision+1,
              updated_at=datetime('now')
            """,
            (
                card_id, task_id, idx, narrative_text, narrative_start, narrative_end,
                asset_id, segment_id, mix_form, enter_tc, exit_tc,
                audio_policy_draft, source_type, system_suggestion, final_decision,
                script_segment_id,
            ),
        )
    _touch_changed(db, task_id)
    return get_scene_card(db, card_id)


def get_scene_card(db, card_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM scene_cards WHERE card_id=?", (card_id,)
    ).fetchone()
    return None if row is None else dict(row)


def list_scene_cards(db, task_id: str) -> list[dict]:
    # A catalog asset may have several physical copies. Keep shot identity and
    # row count independent of those locations; reuse the shared scope resolver.
    from .task_sources import scoped_asset_location, task_has_source_configuration

    cards = [dict(row) for row in db.connection.execute(
        """
        SELECT sc.*, s.start_sec AS segment_start, s.end_sec AS segment_end
        FROM scene_cards sc
        LEFT JOIN segments s ON s.segment_id=sc.segment_id AND s.asset_id=sc.asset_id
        WHERE sc.task_id=? ORDER BY sc.idx
        """, (task_id,),
    ).fetchall()]
    scoped = task_has_source_configuration(db, task_id)
    paths = {None: None}
    for card in cards:
        asset_id = card["asset_id"]
        if asset_id not in paths:
            if scoped:
                location = scoped_asset_location(db, task_id, asset_id)
            else:
                # Old tasks without scope settings retain their historical
                # location fallback, deterministically and without duplication.
                location = db.connection.execute(
                    """SELECT relative_path FROM asset_locations
                       WHERE asset_id=? AND state='available'
                       ORDER BY location_id LIMIT 1""", (asset_id,),
                ).fetchone()
            paths[asset_id] = location["relative_path"] if location else None
        card["relative_path"] = paths[asset_id]
    return cards


_PICTURE_VIEW_FIELDS = (
    "card_id", "task_id", "idx", "narrative_text", "narrative_start",
    "narrative_end", "asset_id", "segment_id", "mix_form", "enter_tc",
    "exit_tc", "audio_policy_draft", "source_type", "system_suggestion",
    "final_decision", "confirm_status", "script_segment_id", "shot_revision",
    "segment_start", "segment_end",
)


def picture_view_digest(db, task_id: str, cards: list[dict] | None = None) -> str:
    """Cheap identity for the reviewed shot order, trims and card decisions.

    Media bytes have a separate per-card version gate. Avoid full-file hashing
    here because task detail and CLI inspect both need this value promptly.
    """
    if cards is None:
        cards = list_scene_cards(db, task_id)
    reviewed = [
        {key: card.get(key) for key in _PICTURE_VIEW_FIELDS}
        for card in cards
    ]
    return canonical_digest({"taskId": task_id, "sceneCards": reviewed})


def set_mix_form(
    db,
    card_id: str,
    mix_form: str,
    enter_tc: float | None = None,
    exit_tc: float | None = None,
) -> dict:
    if mix_form not in VALID_MIX_FORMS:
        raise ValueError(f"混剪形态必须是 {VALID_MIX_FORMS} 之一")
    card = get_scene_card(db, card_id)
    if card is None:
        raise KeyError(f"场景卡片不存在：{card_id}")
    from .rough_cut import _required_ad_ratio

    _required_ad_ratio(db.connection, card["task_id"])
    _reject_locked_legacy_write(db, card["task_id"])
    next_enter = card["enter_tc"] if enter_tc is None else enter_tc
    next_exit = card["exit_tc"] if exit_tc is None else exit_tc
    _validate_time_window(
        next_enter, next_exit, "源", allow_zero_zero=True, require_complete=True
    )
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE scene_cards
            SET mix_form=?, enter_tc=COALESCE(?, enter_tc),
                exit_tc=COALESCE(?, exit_tc), updated_at=datetime('now')
            WHERE card_id=?
            """,
            (mix_form, enter_tc, exit_tc, card_id),
        )
    _touch_changed(db, card["task_id"])
    return get_scene_card(db, card_id)


def set_audio_policy(db, card_id: str, audio_policy: str) -> dict:
    if audio_policy not in VALID_AUDIO_POLICY:
        raise ValueError(f"原声策略必须是 {VALID_AUDIO_POLICY} 之一")
    card = get_scene_card(db, card_id)
    if card is None:
        raise KeyError(f"场景卡片不存在：{card_id}")
    from .rough_cut import _required_ad_ratio

    _required_ad_ratio(db.connection, card["task_id"])
    _reject_locked_legacy_write(db, card["task_id"])
    with db.transaction() as connection:
        connection.execute(
            "UPDATE scene_cards SET audio_policy_draft=?, updated_at=datetime('now') WHERE card_id=?",
            (audio_policy, card_id),
        )
    _touch_changed(db, card["task_id"])
    return get_scene_card(db, card_id)


def confirm_scene_card(db, card_id: str, final_decision: str | None = None) -> dict:
    card = get_scene_card(db, card_id)
    if card is None:
        raise KeyError(f"场景卡片不存在：{card_id}")
    from .rough_cut import _required_ad_ratio

    _required_ad_ratio(db.connection, card["task_id"])
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE scene_cards
            SET confirm_status=1,
                final_decision=COALESCE(?, final_decision),
                updated_at=datetime('now')
            WHERE card_id=?
            """,
            (final_decision, card_id),
        )
    return get_scene_card(db, card_id)


def set_picture_lock(
    db,
    task_id: str,
    lock_version: str = "picture-lock-v1",
    *,
    confirm_all: bool = False,
    project_root: Path | None = None,
    expected_view_digest: str | None = None,
    expected_media_versions: dict | None = None,
) -> dict:
    row = db.connection.execute(
        "SELECT task_id,task_type FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")
    if latest_gate(db, task_id, "brief") is None:
        raise RuntimeError("BRIEF_GATE_MISSING: 必须先锁定文案")
    narration_duration = (
        _require_current_narration(db, task_id, row["task_type"], project_root)
        if project_root is not None else None
    )
    with db.transaction(immediate=True) as connection:
        if (expected_view_digest is not None
                and picture_view_digest(db, task_id) != expected_view_digest):
            raise RuntimeError(
                "PICTURE_VIEW_CHANGED: 镜头顺序、时间或裁切已变化，请重新查看画面。"
            )
        unconfirmed = connection.execute(
            "SELECT COUNT(*) FROM scene_cards WHERE task_id=? AND confirm_status<>1",
            (task_id,),
        ).fetchone()[0]
        if unconfirmed and not confirm_all:
            raise RuntimeError(
                "SCENE_CONFIRMATION_REQUIRED: 存在未确认镜头，不能锁定画面"
            )
        validate_task_scene_uniqueness(connection, task_id)
        if narration_duration is not None:
            _require_full_picture_timeline(connection, task_id, narration_duration)
        if row["task_type"] == "ad_mix":
            from .rough_cut import validate_ad_source_windows

            validate_ad_source_windows(connection, task_id)
        elif row["task_type"] in {"digital_human", "数字人混剪"}:
            from .rough_cut import validate_task_visual_directions

            validate_task_visual_directions(connection, task_id)
        if expected_media_versions is not None:
            from .rough_media import assert_picture_media_available

            assert_picture_media_available(
                db, task_id, expected_media_versions, verify_decoding=False,
            )
        if confirm_all:
            connection.execute(
                "UPDATE scene_cards SET confirm_status=1,updated_at=datetime('now') "
                "WHERE task_id=? AND confirm_status<>1",
                (task_id,),
            )
        digest, state = selection_digest(db, task_id)
        if expected_media_versions is not None:
            assert_picture_media_available(
                db, task_id, expected_media_versions, verify_decoding=False,
            )
        current_lock = connection.execute(
            "SELECT * FROM picture_lock WHERE task_id=?", (task_id,)
        ).fetchone()
        current_gate = latest_gate(db, task_id, "picture_lock")
        if (current_lock is not None and not current_lock["changed_since_lock"]
                and current_lock["lock_version"] == lock_version
                and current_lock["selection_digest"] == digest
                and current_gate is not None and current_gate["gate_digest"] == digest):
            from .workflow import project_gates

            stages = connection.execute(
                "SELECT status FROM task_stage_states WHERE task_id=? "
                "AND stage_key IN ('rough_cut','picture_order','picture_lock')", (task_id,)
            ).fetchall()
            # A retry of an effective human lock must not advance gate ordering or
            # overwrite a completed task. Formal rework still needs fresh evidence.
            if (len(stages) == 3 and all(stage["status"] == "completed" for stage in stages)
                    and project_gates(db, task_id)["picture_lock"]):
                return get_picture_lock(db, task_id)
        connection.execute(
            """
            INSERT INTO picture_lock(task_id, lock_version, changed_since_lock, selection_digest)
            VALUES(?, ?, 0, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              lock_version=excluded.lock_version, locked_at=datetime('now'),
              changed_since_lock=0, selection_digest=excluded.selection_digest
            """,
            (task_id, lock_version, digest),
        )
        record_gate(connection, task_id, "picture_lock", digest, state)
        # A new successful human lock is fresh evidence; preserved old gates
        # alone may never clear an invalidation from a voice or picture edit.
        from .workflow import complete_stage
        for stage_key in ("rough_cut", "picture_order", "picture_lock"):
            complete_stage(db, task_id, stage_key, input_digest=digest,
                details={"picture_lock_version": lock_version, "selection_digest": digest})
        # 画面锁定对应 tasks.status 的 selection_locked
        connection.execute(
            "UPDATE tasks SET status='selection_locked', updated_at=datetime('now') WHERE task_id=?",
            (task_id,),
        )
    return get_picture_lock(db, task_id)


def _require_current_narration(db, task_id: str, task_type: str, project_root: Path) -> float:
    """Fail the user-facing lock when its continuous preview voice is unavailable."""
    from .tasks import get_task

    task = get_task(db, task_id)
    duration = None
    if task_type == "ad_mix":
        from .ad_voice_preview import current_ad_voice, measured_voice_duration

        duration = measured_voice_duration(current_ad_voice(db, project_root, task))
    elif task_type == "digital_human":
        from .digital_render_profile import current_digital_duration

        duration = current_digital_duration(task)
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise RoughNarrationUnavailable(
            "ROUGH_NARRATION_UNAVAILABLE: 当前完整配音或数字人口播音轨不可用，"
            "请恢复主声并重新试听后锁定画面"
        )
    return duration


def _require_full_picture_timeline(connection, task_id: str, duration: float) -> None:
    rows = connection.execute(
        "SELECT narrative_start,narrative_end FROM scene_cards WHERE task_id=? "
        "ORDER BY narrative_start,card_id", (task_id,),
    ).fetchall()
    if not rows:
        raise RuntimeError("ROUGH_TIMELINE_INCOMPLETE: 至少需要一个镜头覆盖主声")
    try:
        windows = [TimeWindow(seconds_to_us(row["narrative_start"]),
                              seconds_to_us(row["narrative_end"])) for row in rows]
        validate_coverage(windows, TimeWindow(0, windows[-1].end_us))
        # Encoded MP3 padding and source frame boundaries can differ from the
        # edit timeline by a few frames. The renderer already uses 100 ms.
        if abs(windows[-1].end_us - seconds_to_us(duration)) > 100_000:
            raise TimelineContractError(
                "TIMELINE_COVERAGE_MISMATCH", "镜头未覆盖完整主声时间",
            )
    except TimelineContractError as error:
        raise RuntimeError(
            "ROUGH_TIMELINE_INCOMPLETE: 镜头必须从头到尾无缺口覆盖当前主声"
        ) from error


def get_picture_lock(db, task_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM picture_lock WHERE task_id=?", (task_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _touch_changed(db, task_id: str) -> None:
    """画面锁定后若再改卡片，标记 changed_since_lock，强制说明重跑范围。"""
    row = db.connection.execute(
        "SELECT task_id FROM picture_lock WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        return
    transaction = nullcontext(db.connection) if db.connection.in_transaction else db.transaction()
    with transaction as connection:
        connection.execute(
            "UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?",
            (task_id,),
        )
