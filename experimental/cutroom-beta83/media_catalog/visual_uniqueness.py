"""Evidence-backed duplicate visual checks for one output timeline.

A shared asset identity, full content hash, physical file identity, or current
human-confirmed direct source-window pair can trigger a hard conflict.  Similar names and themes
are never proof that two shots show the same content.
"""

from __future__ import annotations

import math


class DuplicateVisualError(ValueError):
    def __init__(self, prior, current, evidence):
        self.card_ids = [prior['card_id'], current['card_id']]
        self.evidence = evidence
        super().__init__(f"DUPLICATE_VISUAL: 重复画面；镜头 {self.card_ids[0]} 与 {self.card_ids[1]}；{evidence}")


def validate_explicit_visual_duplicates(connection, uses: list[dict]) -> None:
    """Reject a repeated image or a substantial repeated displayed video span."""
    if len(uses) < 2:
        return
    asset_ids = sorted({use["asset_id"] for use in uses})
    placeholders = ",".join("?" for _ in asset_ids)
    rows = connection.execute(
        f"SELECT asset_id,full_sha256 FROM assets WHERE asset_id IN ({placeholders})",
        tuple(asset_ids),
    ).fetchall()
    hashes = {row["asset_id"]: row["full_sha256"] for row in rows}
    rows = connection.execute(
        f"SELECT asset_id,file_identity FROM asset_locations "
        f"WHERE asset_id IN ({placeholders}) AND state='available' "
        f"AND file_identity IS NOT NULL AND file_identity<>''",
        tuple(asset_ids),
    ).fetchall()
    file_ids: dict[str, set[str]] = {asset_id: set() for asset_id in asset_ids}
    for row in rows:
        file_ids[row["asset_id"]].add(row["file_identity"])

    def same_content(left: dict, right: dict) -> bool:
        a, b = left["asset_id"], right["asset_id"]
        return (a == b
                or bool(hashes.get(a) and hashes[a] == hashes.get(b))
                or bool(file_ids[a] & file_ids[b]))

    from types import SimpleNamespace
    from .duplicate_evidence import duplicates_for_window, confirmed_window_duplicate
    db = SimpleNamespace(connection=connection)
    # Re-read at the write gate: candidate lists and request-local caches are
    # not authority for subsequent human corrections or source changes.
    uses = [{**use, 'duplicate_evidence': duplicates_for_window(
        db, use['asset_id'], round((use.get('start') or 0)*1e6),
        round((use.get('end') or 0)*1e6) if use['kind'] == 'video' else 0)}
        for use in uses]
    for index, current in enumerate(uses):
        for prior in uses[:index]:
            if current["kind"] != prior["kind"]:
                continue
            if (current["kind"] == "image" and current.get("continuation_group")
                    and current["continuation_group"] == prior.get("continuation_group")):
                continue
            confirmed = confirmed_window_duplicate(current, prior)
            if confirmed is not None:
                raise DuplicateVisualError(prior, current,
                    f"\u4eba\u5de5\u786e\u8ba4\u540c\u4e00\u753b\u9762\u533a\u95f4\uff1b\u8bc1\u636e {confirmed['evidenceId']} / \u7248\u672c {confirmed['revision']}")
            if not same_content(current, prior):
                continue
            if current["kind"] == "video":
                overlap = min(current["end"], prior["end"]) - max(
                    current["start"], prior["start"]
                )
                shorter = min(current["end"] - current["start"],
                              prior["end"] - prior["start"])
                if overlap < 0.5 or overlap / shorter < 0.75:
                    continue
                evidence = (f"源区间 {prior['start']:.3f}-{prior['end']:.3f}s "
                            f"与 {current['start']:.3f}-{current['end']:.3f}s 重叠")
            else:
                evidence = "同一图片内容再次出现"
            raise DuplicateVisualError(prior, current, evidence)


def validate_scene_card_uniqueness(connection, cards) -> None:
    """Project stored scene cards to actual displayed spans, then check once."""
    cards = [dict(card) for card in cards]
    asset_ids = sorted({card["asset_id"] for card in cards if card.get("asset_id")})
    if not asset_ids:
        return
    placeholders = ",".join("?" for _ in asset_ids)
    kinds = {
        row["asset_id"]: row["media_kind"] for row in connection.execute(
            f"SELECT asset_id,media_kind FROM assets WHERE asset_id IN ({placeholders})",
            tuple(asset_ids),
        ).fetchall()
    }
    from .segment_marks import resolve_source_segment
    segments = {}
    for card in cards:
        if card.get("segment_id") and card.get("asset_id"):
            try:
                segments[card["segment_id"]] = resolve_source_segment(connection, card["asset_id"], card["segment_id"])
            except (KeyError, ValueError) as error:
                raise ValueError("SOURCE_REFERENCE_MISMATCH: 镜头引用的素材片段不存在或已失效") from error
    uses = []
    prior_card = None
    prior_image_group = None
    for card in cards:
        asset_id = card.get("asset_id")
        if not asset_id or card.get("mix_form") == "数字人全屏":
            prior_card = card
            prior_image_group = None
            continue
        kind = kinds.get(asset_id)
        if kind not in {"video", "image"}:
            continue
        segment = segments.get(card.get("segment_id"))
        if card.get("segment_id") and segment is None:
            raise ValueError("SOURCE_REFERENCE_MISMATCH: 镜头引用的素材片段不存在")
        if segment is not None and segment["asset_id"] != asset_id:
            raise ValueError("SOURCE_REFERENCE_MISMATCH: 素材片段与镜头不一致")
        raw_start, raw_end = card.get("enter_tc"), card.get("exit_tc")
        if (raw_start is None) != (raw_end is None):
            raise ValueError("SOURCE_WINDOW_INVALID: 源时间窗必须同时有入点和出点")
        if raw_start is not None:
            try:
                raw_start, raw_end = float(raw_start), float(raw_end)
            except (TypeError, ValueError) as error:
                raise ValueError("SOURCE_WINDOW_INVALID: 源时间窗必须是有限数字") from error
            if (not math.isfinite(raw_start) or not math.isfinite(raw_end)
                    or raw_start < 0 or raw_end < 0
                    or (raw_start, raw_end) != (0, 0) and raw_end <= raw_start):
                raise ValueError("SOURCE_WINDOW_INVALID: 源时间窗顺序或边界无效")
        if raw_start is None or raw_start == raw_end == 0:
            source_start = float(segment["start_sec"]) if segment is not None else 0.0
        else:
            source_start = raw_start
        if kind == "image" and raw_start is not None and (raw_start, raw_end) != (0, 0):
            raise ValueError("SOURCE_WINDOW_INVALID: 图片不能有视频源时间窗")
        try:
            duration = float(card["narrative_end"]) - float(card["narrative_start"])
        except (TypeError, ValueError) as error:
            raise ValueError("INVALID_TIMECODE: 镜头展示时长无效") from error
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("INVALID_TIMECODE: 镜头展示时长无效")
        image_group = None
        if kind == "image":
            continuous = (prior_card is not None
                          and prior_card.get("asset_id") == asset_id
                          and kinds.get(asset_id) == "image"
                          and abs(float(prior_card["narrative_end"])
                                  - float(card["narrative_start"])) < 1e-6)
            image_group = prior_image_group if continuous else card["card_id"]
        uses.append({
            "card_id": card["card_id"], "asset_id": asset_id, "kind": kind,
            "start": source_start,
            "end": source_start + duration if kind == "video" else None,
            "continuation_group": image_group,
        })
        prior_card = card
        prior_image_group = image_group
    validate_explicit_visual_duplicates(connection, uses)


def validate_task_scene_uniqueness(connection, task_id: str) -> None:
    cards = connection.execute(
        "SELECT card_id,asset_id,segment_id,mix_form,narrative_start,"
        "narrative_end,enter_tc,exit_tc FROM scene_cards WHERE task_id=? "
        "ORDER BY idx,card_id", (task_id,),
    ).fetchall()
    validate_scene_card_uniqueness(connection, cards)
