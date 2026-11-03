"""Auditable rejection, local reranking, and explicit scene-candidate application."""

from __future__ import annotations

import json
import re
import uuid

from .asset_index import list_asset_index
from .rough_cut import apply_edit
from .scene_cards import get_scene_card


DECISIONS = {"rejected", "correction", "reranked", "applied"}
_STOP_TOKENS = {
    "改用", "换成", "请用", "不要", "使用", "镜头", "画面", "素材",
    "这个", "当前", "推荐", "重新", "一个", "需要",
}


def _required_text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    return value.strip()


def _require_card(db, task_id: str, card_id: str) -> dict:
    card = get_scene_card(db, card_id)
    if card is None or card["task_id"] != task_id:
        raise KeyError(f"场景卡片不存在：{card_id}")
    return card


def _public_feedback(row) -> dict:
    try:
        candidates = json.loads(row["candidates_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        candidates = []
    return {
        "feedbackId": row["feedback_id"],
        "taskId": row["task_id"],
        "cardId": row["card_id"],
        "decision": row["decision"],
        "instruction": row["instruction"],
        "originalAssetId": row["original_asset_id"],
        "selectedAssetId": row["selected_asset_id"],
        "candidates": candidates if isinstance(candidates, list) else [],
        "createdAt": row["created_at"],
        "appliedAt": row["applied_at"],
    }


def record_feedback(
    db,
    task_id: str,
    card_id: str,
    decision: str,
    instruction: str | None = None,
    *,
    original_asset_id: str | None = None,
    selected_asset_id: str | None = None,
    candidates: list[dict] | None = None,
) -> dict:
    card = _require_card(db, task_id, card_id)
    if decision not in DECISIONS:
        raise ValueError("分镜反馈决定无效")
    if decision in {"rejected", "correction", "reranked"}:
        instruction = _required_text(instruction, "反馈说明")
    elif instruction is not None:
        instruction = _required_text(instruction, "反馈说明")
    for label, asset_id in (
        ("原始素材", original_asset_id),
        ("所选素材", selected_asset_id),
    ):
        if asset_id is not None and db.connection.execute(
            "SELECT 1 FROM assets WHERE asset_id=?",
            (asset_id,),
        ).fetchone() is None:
            raise KeyError(f"{label}不存在：{asset_id}")
    if candidates is not None and not isinstance(candidates, list):
        raise ValueError("候选列表无效")
    feedback_id = "storyboard-feedback-" + uuid.uuid4().hex
    applied_at = (
        db.connection.execute("SELECT datetime('now')").fetchone()[0]
        if decision == "applied" else None
    )
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO storyboard_feedback(
              feedback_id,task_id,card_id,decision,instruction,
              original_asset_id,selected_asset_id,candidates_json,applied_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                feedback_id,
                task_id,
                card_id,
                decision,
                instruction,
                original_asset_id if original_asset_id is not None else card.get("asset_id"),
                selected_asset_id,
                json.dumps(candidates or [], ensure_ascii=False, separators=(",", ":")),
                applied_at,
            ),
        )
    row = db.connection.execute(
        "SELECT * FROM storyboard_feedback WHERE feedback_id=?",
        (feedback_id,),
    ).fetchone()
    return _public_feedback(row)


def _tokens(text: str | None) -> set[str]:
    value = str(text or "").lower()
    result = set(re.findall(r"[a-z0-9]{2,}", value))
    for phrase in re.findall(r"[\u3400-\u9fff]+", value):
        if len(phrase) == 1:
            result.add(phrase)
        else:
            result.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return {token for token in result if token not in _STOP_TOKENS}


def _task_digital_assets(db, task_id: str) -> set[str]:
    return {
        row["asset_id"]
        for row in db.connection.execute(
            "SELECT asset_id FROM task_assets WHERE task_id=? AND role='digital_human'",
            (task_id,),
        ).fetchall()
    }


def rerank_scene_candidates(
    db,
    task_id: str,
    card_id: str,
    correction: str,
    *,
    limit: int = 12,
) -> dict:
    card = _require_card(db, task_id, card_id)
    correction = _required_text(correction, "纠正要求")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 24:
        raise ValueError("候选数量必须在 1 到 24 之间")

    instruction_tokens = _tokens(correction)
    narration_tokens = _tokens(card.get("narrative_text"))
    suggestion_tokens = _tokens(card.get("system_suggestion"))
    excluded = _task_digital_assets(db, task_id) | {card.get("asset_id")}
    indexed = []
    for kind in ("video", "image"):
        indexed.extend(list_asset_index(
            db,
            task_id=task_id,
            media_kind=kind,
            availability="available",
            limit=200,
        )["items"])

    ranked = []
    for asset in indexed:
        if asset["assetId"] in excluded:
            continue
        searchable = _tokens(
            f"{asset.get('relativePath') or ''} {asset.get('libraryName') or ''}"
        )
        correction_matches = sorted(instruction_tokens & searchable)
        narration_matches = sorted(narration_tokens & searchable)
        suggestion_matches = sorted(suggestion_tokens & searchable)
        score = (
            len(correction_matches) * 100
            + len(narration_matches) * 10
            + len(suggestion_matches) * 3
        )
        if score <= 0:
            continue
        if correction_matches:
            reason = "纠正要求匹配：" + "、".join(correction_matches[:4])
        elif narration_matches:
            reason = "旁白语义匹配：" + "、".join(narration_matches[:4])
        else:
            reason = "系统建议匹配：" + "、".join(suggestion_matches[:4])
        warnings = []
        if asset["authorizationStatus"] != "approved":
            warnings.append("授权状态尚未核验")
        if asset["durationSeconds"] is None and asset["mediaKind"] == "video":
            warnings.append("视频时长尚未探测")
        ranked.append({
            "assetId": asset["assetId"],
            "mediaKind": asset["mediaKind"],
            "sourceDirectory": asset["libraryName"],
            "libraryId": asset["libraryId"],
            "relativePath": asset["relativePath"],
            "previewUrl": asset["previewUrl"],
            "authorizationStatus": asset["authorizationStatus"],
            "durationSeconds": asset["durationSeconds"],
            "mixForm": (
                "信息卡" if asset["mediaKind"] == "image"
                else card.get("mix_form") if card.get("mix_form") in {"broll全屏", "画中画"}
                else "broll全屏"
            ),
            "score": score,
            "reason": reason,
            "warnings": warnings,
        })
    ranked.sort(key=lambda item: (-item["score"], item["relativePath"], item["assetId"]))
    candidates = ranked[:limit]
    feedback = record_feedback(
        db,
        task_id,
        card_id,
        "reranked",
        correction,
        candidates=candidates,
    )
    return {**feedback, "candidates": candidates}


def apply_feedback_candidate(
    db,
    task_id: str,
    card_id: str,
    feedback_id: str,
    asset_id: str,
    *,
    impact_token: str | None = None,
) -> dict:
    _require_card(db, task_id, card_id)
    row = db.connection.execute(
        """
        SELECT * FROM storyboard_feedback
        WHERE feedback_id=? AND task_id=? AND card_id=? AND decision='reranked'
        """,
        (feedback_id, task_id, card_id),
    ).fetchone()
    if row is None:
        raise KeyError("重荐记录不存在")
    feedback = _public_feedback(row)
    candidate = next(
        (item for item in feedback["candidates"] if item.get("assetId") == asset_id),
        None,
    )
    if candidate is None:
        raise ValueError("所选素材不属于这次重荐候选")
    edit = {
        "action": "replace",
        "card_id": card_id,
        "asset_id": asset_id,
        "mix_form": candidate.get("mixForm") or "broll全屏",
    }
    edit_result = apply_edit(
        db,
        task_id,
        edit,
        impact_token=impact_token,
    )
    applied = record_feedback(
        db,
        task_id,
        card_id,
        "applied",
        "应用本地重荐候选",
        original_asset_id=feedback["originalAssetId"],
        selected_asset_id=asset_id,
        candidates=[candidate],
    )
    return {"edit": edit_result, "feedback": applied}
