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
import math

from .provenance import latest_gate, record_gate, selection_digest

VALID_MIX_FORMS = (
    "数字人全屏",
    "broll全屏",
    "画中画",
    "信息卡",
)
VALID_AUDIO_POLICY = ("keep", "mute", "duck", "clean", "replace")
VALID_SOURCE_TYPES = ("用户指定", "系统推荐", "长期库", "缺口替代")


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
) -> dict:
    card_id = _card_id(task_id, idx)
    existing = get_scene_card(db, card_id)
    if existing is not None:
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
    if mix_form is not None and mix_form not in VALID_MIX_FORMS:
        raise ValueError(f"混剪形态必须是 {VALID_MIX_FORMS} 之一")
    if audio_policy_draft is not None and audio_policy_draft not in VALID_AUDIO_POLICY:
        raise ValueError(f"原声策略必须是 {VALID_AUDIO_POLICY} 之一")
    if source_type is not None and source_type not in VALID_SOURCE_TYPES:
        raise ValueError(f"来源类型必须是 {VALID_SOURCE_TYPES} 之一")
    if asset_id is not None:
        row = db.connection.execute(
            "SELECT asset_id FROM assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"素材未入册，不能挂到场景卡片：{asset_id}")
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO scene_cards(
              card_id, task_id, idx, narrative_text, narrative_start, narrative_end,
              asset_id, segment_id, mix_form, enter_tc, exit_tc,
              audio_policy_draft, source_type, system_suggestion, final_decision
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(card_id) DO UPDATE SET
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
              updated_at=datetime('now')
            """,
            (
                card_id, task_id, idx, narrative_text, narrative_start, narrative_end,
                asset_id, segment_id, mix_form, enter_tc, exit_tc,
                audio_policy_draft, source_type, system_suggestion, final_decision,
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
    return [
        dict(r)
        for r in db.connection.execute(
            """
            SELECT sc.*, al.relative_path,
                   s.start_sec AS segment_start,
                   s.end_sec AS segment_end
            FROM scene_cards sc
            LEFT JOIN asset_locations al ON al.asset_id=sc.asset_id AND al.state='available'
            LEFT JOIN segments s ON s.segment_id=sc.segment_id AND s.asset_id=sc.asset_id
            WHERE sc.task_id=?
            ORDER BY sc.idx
            """,
            (task_id,),
        ).fetchall()
    ]


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
    _reject_locked_legacy_write(db, card["task_id"])
    with db.transaction() as connection:
        connection.execute(
            "UPDATE scene_cards SET audio_policy_draft=?, updated_at=datetime('now') WHERE card_id=?",
            (audio_policy, card_id),
        )
    _touch_changed(db, card["task_id"])
    return get_scene_card(db, card_id)


def confirm_scene_card(db, card_id: str, final_decision: str | None = None) -> dict:
    if get_scene_card(db, card_id) is None:
        raise KeyError(f"场景卡片不存在：{card_id}")
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
) -> dict:
    row = db.connection.execute(
        "SELECT task_id FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")
    if latest_gate(db, task_id, "brief") is None:
        raise RuntimeError("BRIEF_GATE_MISSING: 必须先锁定文案")
    with db.transaction() as connection:
        unconfirmed = connection.execute(
            "SELECT COUNT(*) FROM scene_cards WHERE task_id=? AND confirm_status<>1",
            (task_id,),
        ).fetchone()[0]
        if unconfirmed and not confirm_all:
            raise RuntimeError(
                "SCENE_CONFIRMATION_REQUIRED: 存在未确认镜头，不能锁定画面"
            )
        if confirm_all:
            connection.execute(
                "UPDATE scene_cards SET confirm_status=1,updated_at=datetime('now') "
                "WHERE task_id=? AND confirm_status<>1",
                (task_id,),
            )
        digest, state = selection_digest(db, task_id)
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
        # 画面锁定对应 tasks.status 的 selection_locked
        connection.execute(
            "UPDATE tasks SET status='selection_locked', updated_at=datetime('now') WHERE task_id=?",
            (task_id,),
        )
    return get_picture_lock(db, task_id)


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
    with db.transaction() as connection:
        connection.execute(
            "UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?",
            (task_id,),
        )
