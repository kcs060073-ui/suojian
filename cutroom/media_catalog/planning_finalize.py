"""Shared canonical planning finalizers for standalone and Agent inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .match import rank
from .provenance import latest_gate
from .task_sources import list_eligible_assets
from .tasks import get_task
from .workflow import complete_stage


VALID_STORYBOARD_SOURCES = frozenset({"standalone_model", "agent_artifact"})


def _canonical_json(value) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "STORYBOARD_OUTPUT_INVALID: 规划结果必须是可序列化 JSON"
        ) from error


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        temporary.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_words(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "TRANSCRIPT_OUTPUT_INVALID: 词级时间戳文件无效"
        ) from error
    if not isinstance(value, list) or not value:
        raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 没有识别到词级时间戳")
    previous = -1.0
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("w"), str):
            raise RuntimeError(
                "TRANSCRIPT_OUTPUT_INVALID: 词级时间戳结构无效"
            )
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                "TRANSCRIPT_OUTPUT_INVALID: 词级时间戳结构无效"
            ) from error
        if start < previous or end < start:
            raise RuntimeError(
                "TRANSCRIPT_OUTPUT_INVALID: 词级时间戳顺序无效"
            )
        previous = end
    return value


def validate_digital_storyboard_content(
    content: dict,
    script: str | None = None,
) -> dict:
    if not isinstance(content, dict):
        raise RuntimeError("STORYBOARD_OUTPUT_INVALID: 规划结果必须是对象")
    brief = content.get("brief")
    plan = content.get("plan")
    if not isinstance(brief, dict) or not isinstance(brief.get("broll"), list):
        raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜输出结构无效")
    if not isinstance(plan, dict):
        raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件必须是对象")
    for item in brief["broll"]:
        if not isinstance(item, dict):
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜项结构无效")
        anchor = item.get("anchorText")
        if not isinstance(anchor, str) or not anchor:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜锚点无效")
        if script is not None and anchor not in script:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 锚点不属于锁定原文")
        if item.get("kind") not in {"broll", "image"}:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜素材类型无效")
        if not isinstance(item.get("query"), str):
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜缺少素材查询词")
    _canonical_json(content)
    return brief


def _anchor_time(
    script: str,
    words: list[dict],
    anchor: str,
) -> tuple[float, float]:
    anchor_offset = script.find(anchor)
    if anchor_offset < 0:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: AI 锚点不属于锁定文案")

    timeline = []
    for item in words:
        characters = [char for char in item["w"] if not char.isspace()]
        if not characters:
            continue
        start = float(item["start"])
        end = float(item["end"])
        span = (end - start) / len(characters)
        timeline.extend(
            {
                "start": start + span * index,
                "end": start + span * (index + 1),
            }
            for index in range(len(characters))
        )
    if not timeline:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: 没有可用词级时间")

    spoken_rank = {}
    rank_index = 0
    for index, character in enumerate(script):
        if re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff%]", character):
            spoken_rank[index] = rank_index
            rank_index += 1
    if rank_index == 0:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: 锁定文案没有可定位文字")

    anchor_ranks = [
        spoken_rank[index]
        for index in range(anchor_offset, anchor_offset + len(anchor))
        if index in spoken_rank
    ]
    if not anchor_ranks:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: AI 锚点没有可定位文字")

    def timing_at(position: int, edge: str) -> float:
        projected = (position / rank_index) * len(timeline)
        timeline_index = int(projected + 0.5)
        timeline_index = min(max(timeline_index, 0), len(timeline) - 1)
        return float(timeline[timeline_index][edge])

    start = max(0.0, timing_at(anchor_ranks[0], "start"))
    end = max(start + 0.5, timing_at(anchor_ranks[-1] + 1, "end"))
    return start, end


def build_digital_storyboard_cards(
    db,
    task_id: str,
    script: str,
    words: list[dict],
    brief: dict,
) -> tuple[list[dict], list[dict]]:
    digital = db.connection.execute(
        "SELECT asset_id FROM task_assets "
        "WHERE task_id=? AND role='digital_human'",
        (task_id,),
    ).fetchall()
    if len(digital) != 1:
        raise RuntimeError(
            "DIGITAL_HUMAN_COUNT_INVALID: 任务必须恰好绑定一个数字人口播源"
        )
    duration = float(words[-1]["end"])
    cards = [
        {
            "card_id": f"{task_id}#0",
            "idx": 0,
            "narrative_text": script,
            "narrative_start": float(words[0]["start"]),
            "narrative_end": duration,
            "asset_id": digital[0]["asset_id"],
            "role": "digital_human",
            "mix_form": "数字人全屏",
            "audio_policy": "keep",
            "source_type": "用户指定",
            "system_suggestion": "锁定的数字人口播主轴，声音和顺序不可切断",
        }
    ]
    eligible = list_eligible_assets(db, task_id)
    missing = []
    for item in brief["broll"]:
        expected_kind = "image" if item["kind"] == "image" else "video"
        candidates = [
            {
                "id": candidate["asset_id"],
                "text": candidate["relative_path"],
                "candidate": candidate,
            }
            for candidate in eligible
            if candidate["media_kind"] == expected_kind
            and candidate["asset_id"] != digital[0]["asset_id"]
        ]
        if not candidates:
            missing.append(dict(item))
            continue
        selected = rank(item["query"], candidates)[0]["candidate"]
        start, end = _anchor_time(script, words, item["anchorText"])
        is_image = selected["media_kind"] == "image"
        cards.append(
            {
                "card_id": f"{task_id}#{len(cards)}",
                "idx": len(cards),
                "narrative_text": item["anchorText"],
                "narrative_start": start,
                "narrative_end": end,
                "asset_id": selected["asset_id"],
                "role": "information_card" if is_image else "broll",
                "mix_form": "画中画" if is_image else "broll全屏",
                "audio_policy": "mute",
                "source_type": "系统推荐",
                "system_suggestion": (
                    f"{item['query']}：{item.get('prompt') or ''}".rstrip("：")
                ),
            }
        )
    return cards, missing


def _task_planning_inputs(
    db,
    task_id: str,
    project_root: Path,
) -> tuple[Path, Path, Path, str, list[dict]]:
    task = get_task(db, task_id)
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    if task.get("task_type") != "digital_human":
        raise ValueError("UNSUPPORTED_ROUTE: 当前任务不是数字人路线")
    if latest_gate(db, task_id, "brief") is None:
        raise RuntimeError("BRIEF_GATE_MISSING: 必须先锁定最终文案")
    if any(part in task_id for part in ("/", "\\")) or task_id in {".", ".."}:
        raise ValueError("INVALID_TASK_ID: 任务编号包含非法路径字符")
    tasks_root = (
        project_root / "runtime" / "workbench" / "tasks"
    ).resolve()
    task_root = (tasks_root / task_id).resolve()
    if not _contained(task_root, tasks_root):
        raise ValueError("INVALID_TASK_ID: 任务目录越界")
    brief = task.get("brief")
    script_record = brief.get("final_script") if isinstance(brief, dict) else None
    if not isinstance(script_record, dict):
        raise ValueError("BRIEF_INVALID: 任务缺少锁定文案")
    relative_path = script_record.get("relative_path")
    if not isinstance(relative_path, str):
        raise ValueError("BRIEF_INVALID: 锁定文案路径无效")
    script_path = (task_root / relative_path).resolve()
    if not _contained(script_path, task_root) or not script_path.is_file():
        raise ValueError("LOCKED_SCRIPT_MISSING: 锁定文案文件不存在")
    script_sha256 = _file_sha256(script_path)
    if script_sha256 != script_record.get("sha256"):
        raise RuntimeError("LOCKED_SCRIPT_CHANGED: 锁定文案内容已经变化")
    words_path = task_root / "artifacts" / "transcript.words.json"
    words = _validate_words(words_path)
    return task_root, script_path, words_path, script_sha256, words


def _expected_card_rows(cards: list[dict]) -> list[tuple]:
    return [
        (
            card["card_id"],
            card["idx"],
            card["narrative_text"],
            card["narrative_start"],
            card["narrative_end"],
            card["asset_id"],
            card["mix_form"],
            0.0,
            0.0,
            card["audio_policy"],
            card["source_type"],
            card["system_suggestion"],
            0,
        )
        for card in cards
    ]


def _persist_cards(db, task_id: str, cards: list[dict]) -> bool:
    existing_rows = db.connection.execute(
        "SELECT card_id,idx,narrative_text,narrative_start,narrative_end,"
        "asset_id,mix_form,enter_tc,exit_tc,audio_policy_draft,source_type,"
        "system_suggestion,confirm_status FROM scene_cards "
        "WHERE task_id=? ORDER BY idx,card_id",
        (task_id,),
    ).fetchall()
    if existing_rows:
        if [tuple(row) for row in existing_rows] != _expected_card_rows(cards):
            raise RuntimeError(
                "STORYBOARD_EXISTS: 任务已有不同分镜，不能静默覆盖"
            )
        return True
    with db.transaction(immediate=True) as connection:
        for card in cards:
            if card["asset_id"] is not None:
                connection.execute(
                    "INSERT INTO task_assets(task_id,asset_id,role) VALUES(?,?,?) "
                    "ON CONFLICT(task_id,asset_id) DO UPDATE SET role=excluded.role",
                    (task_id, card["asset_id"], card["role"]),
                )
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                  asset_id,mix_form,enter_tc,exit_tc,audio_policy_draft,
                  source_type,system_suggestion,confirm_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    card["card_id"],
                    task_id,
                    card["idx"],
                    card["narrative_text"],
                    card["narrative_start"],
                    card["narrative_end"],
                    card["asset_id"],
                    card["mix_form"],
                    0.0,
                    0.0,
                    card["audio_policy"],
                    card["source_type"],
                    card["system_suggestion"],
                ),
            )
    return False


def finalize_digital_storyboard(
    db,
    *,
    task_id: str,
    project_root: Path | str,
    content: dict,
    input_binding: dict,
    input_digest: str,
    source: str,
) -> dict:
    """Validate and persist one canonical digital-human storyboard."""
    if source not in VALID_STORYBOARD_SOURCES:
        raise ValueError(
            "STORYBOARD_SOURCE_INVALID: 规划来源必须是 standalone_model 或 agent_artifact"
        )
    if not isinstance(input_binding, dict) or input_binding.get("task_id") != task_id:
        raise ValueError("STORYBOARD_INPUT_INVALID: 规划输入未绑定当前任务")
    if not isinstance(input_digest, str) or not input_digest:
        raise ValueError("STORYBOARD_INPUT_INVALID: 规划输入摘要缺失")

    task_root, script_path, _words_path, script_sha256, words = (
        _task_planning_inputs(db, task_id, Path(project_root).resolve())
    )
    if input_binding.get("script_sha256") != script_sha256:
        raise RuntimeError("STORYBOARD_STALE: 规划输入未绑定当前锁定文案")
    script = script_path.read_text(encoding="utf-8")
    brief = validate_digital_storyboard_content(content, script)
    cards, missing = build_digital_storyboard_cards(
        db,
        task_id,
        script,
        words,
        brief,
    )

    storyboard_dir = task_root / "artifacts" / "storyboard"
    brief_path = storyboard_dir / "brief.json"
    plan_path = storyboard_dir / "plan.json"
    metadata_path = storyboard_dir / "recommendation.json"
    metadata = None
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("STORYBOARD_STALE: 分镜绑定记录无效") from error
        if metadata.get("input_digest") != input_digest:
            raise RuntimeError("STORYBOARD_STALE: 锁定文案、时间或素材范围已变化")
        recorded_source = metadata.get("source")
        if recorded_source is not None and recorded_source != source:
            raise RuntimeError("STORYBOARD_SOURCE_MISMATCH: 已有规划来源不一致")
        if not brief_path.is_file() or not plan_path.is_file():
            raise RuntimeError("STORYBOARD_PARTIAL: 分镜文件不完整")
        try:
            existing_content = {
                "brief": json.loads(brief_path.read_text(encoding="utf-8")),
                "plan": json.loads(plan_path.read_text(encoding="utf-8")),
            }
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("STORYBOARD_PARTIAL: 分镜文件无效") from error
        if _canonical_json(existing_content) != _canonical_json(content):
            raise RuntimeError("STORYBOARD_EXISTS: 已有规划内容不一致")
        if metadata.get("state") == "complete":
            current_count = db.connection.execute(
                "SELECT COUNT(*) FROM scene_cards WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            if current_count != metadata.get("scene_card_count"):
                raise RuntimeError("STORYBOARD_STALE: 分镜记录与任务主账本不一致")
            if recorded_source is None:
                metadata["source"] = source
                _atomic_json(metadata_path, metadata)
            complete_stage(
                db,
                task_id,
                "ai_storyboard",
                input_digest=input_digest,
                details={
                    "scene_card_count": current_count,
                    "missing_count": len(metadata.get("missing", [])),
                    "input_digest": input_digest,
                    "source": source,
                },
            )
            return {
                "reused": True,
                "scene_card_count": current_count,
                "missing_count": len(metadata.get("missing", [])),
                "brief_path": str(brief_path.resolve()),
                "source": source,
            }
    else:
        if brief_path.exists() or plan_path.exists():
            raise RuntimeError("STORYBOARD_PARTIAL: 检测到无绑定的分镜输出")
        storyboard_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(brief_path, content["brief"])
        _atomic_json(plan_path, content["plan"])
        metadata = {
            **input_binding,
            "input_digest": input_digest,
            "source": source,
            "state": "generated",
            "brief_sha256": _file_sha256(brief_path),
            "plan_sha256": _file_sha256(plan_path),
            "missing": [],
        }
        _atomic_json(metadata_path, metadata)

    recovered_existing = _persist_cards(db, task_id, cards)
    metadata.update(
        {
            "input_digest": input_digest,
            "source": source,
            "state": "complete",
            "scene_card_count": len(cards),
            "missing": missing,
        }
    )
    _atomic_json(metadata_path, metadata)
    complete_stage(
        db,
        task_id,
        "ai_storyboard",
        input_digest=input_digest,
        details={
            "scene_card_count": len(cards),
            "missing_count": len(missing),
            "input_digest": input_digest,
            "source": source,
        },
    )
    return {
        "reused": recovered_existing,
        "scene_card_count": len(cards),
        "missing_count": len(missing),
        "brief_path": str(brief_path.resolve()),
        "source": source,
    }
