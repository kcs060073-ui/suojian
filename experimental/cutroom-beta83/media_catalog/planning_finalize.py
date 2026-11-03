"""Shared canonical planning finalizers for standalone and Agent inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .ad_matching import load_observed_ad_windows, match_ad_shots
from .digital_render_profile import probe_digital_render_profile
from .provenance import latest_gate
from .script_segments import create_script_segment
from .task_sources import list_eligible_assets
from .tasks import get_task
from .timeline_contract import PlaybackRate, seconds_to_us
from .transcript_reconciliation import project_transcript_state
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
    segments = brief.get("scriptSegments")
    if segments is not None and (
        not isinstance(segments, list)
        or not segments
        or any(not isinstance(part, str) or not part.strip() for part in segments)
        or (script is not None and "".join(segments) != script)
    ):
        raise RuntimeError("STORYBOARD_SCRIPT_SEGMENTS_INVALID: 文案段须逐字覆盖锁定原文")
    _canonical_json(content)
    return brief


def _semantic_segment_windows(
    script: str, words: list[dict], parts: list[str], output_duration_us: int,
) -> list[dict]:
    """Project exact text partitions onto measured speech, keeping the media tail."""
    spoken = lambda text: sum(bool(re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff%]", char))
                              for char in text)
    total = spoken(script)
    timeline = []
    for word in words:
        characters = [char for char in word["w"] if not char.isspace()]
        if not characters:
            continue
        span = (float(word["end"]) - float(word["start"])) / len(characters)
        timeline.extend(float(word["start"]) + index * span
                        for index in range(len(characters)))
    if total <= 0 or not timeline or any(spoken(part) <= 0 for part in parts):
        raise RuntimeError("STORYBOARD_SCRIPT_SEGMENTS_INVALID: 文案段缺少可定位口播文字")
    boundaries = [0]
    consumed = 0
    rate = PlaybackRate(5, 4)
    for part in parts[:-1]:
        consumed += spoken(part)
        index = min(max(int(consumed / total * len(timeline) + .5), 0), len(timeline) - 1)
        boundary = rate.output_us(seconds_to_us(timeline[index]))
        if boundary <= boundaries[-1] or boundary >= output_duration_us:
            raise RuntimeError("STORYBOARD_SCRIPT_SEGMENTS_INVALID: 文案段无法对应独立的音画时间窗")
        boundaries.append(boundary)
    boundaries.append(output_duration_us)
    return [
        {"index": index, "text": part, "start_us": boundaries[index],
         "end_us": boundaries[index + 1]}
        for index, part in enumerate(parts)
    ]


def _anchor_time(
    script: str,
    words: list[dict],
    anchor: str,
) -> tuple[float, float]:
    from .speech_alignment import verified_anchor
    try:
        return verified_anchor(Path(__file__).resolve().parents[1], script, words, anchor)
    except (ValueError, TypeError, KeyError) as error:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: 锚点缺少唯一、完整的真实词时依据，请核对转写或调整锚点") from error


def _unobserved_context_gap(script: str, words: list[dict], item: dict,
                            segments: list[dict], error: RuntimeError) -> dict | None:
    """Keep an optional request visible without claiming a precise insert time."""
    cause = error.__cause__
    if (item.get("kind") not in {"broll", "image"} or not isinstance(cause, ValueError)
            or str(cause) != "SPEECH_ANCHOR_UNOBSERVED"):
        return None
    anchor = item["anchorText"]
    if script.count(anchor) != 1 or script.find(anchor) != script.rfind(anchor):
        return None
    from .speech_alignment import _canonical, _mapping, _tokens
    mapping = _mapping(Path(__file__).resolve().parents[1],
                       script + anchor + "".join(word["w"] for word in words))
    if not mapping:
        return None  # A failed conversion cannot establish that a phrase is absent.
    target = _canonical(anchor, mapping)
    observed = "".join(char for char, _owner in _tokens(words, mapping))
    # The guard also uses UNOBSERVED for repeated ASR text; that must still reject.
    confirmed = _canonical(script, mapping)
    if (not target or observed.count(target) != 0
            or confirmed.find(target) != confirmed.rfind(target)):
        return None
    owners = [segment for segment in segments if anchor in segment["text"]]
    if len(owners) != 1:
        return None
    segment = owners[0]
    return {**item, "outputStartUs": segment["start_us"],
            "outputEndUs": segment["end_us"],
            "timingBasis": "confirmed_script_segment",
            "reason": "当前转写未完整观察该唯一原稿锚点；精确锚点时间待补，保留数字人口播，按当前文案整段列为待核缺口"}


def build_digital_storyboard_cards(
    db,
    task_id: str,
    script: str,
    words: list[dict],
    brief: dict,
    source_duration_us: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    digital = db.connection.execute(
        "SELECT asset_id FROM task_assets "
        "WHERE task_id=? AND role='digital_human'",
        (task_id,),
    ).fetchall()
    if len(digital) != 1:
        raise RuntimeError(
            "DIGITAL_HUMAN_COUNT_INVALID: 任务必须恰好绑定一个数字人口播源"
        )
    if type(source_duration_us) is not int or source_duration_us <= 0:
        raise RuntimeError("DIGITAL_DURATION_MISSING: 口播源缺少可用时长")
    rate = PlaybackRate(5, 4)
    if seconds_to_us(words[-1]["end"]) > source_duration_us + 50_000:
        raise RuntimeError("TRANSCRIPT_TIMING_OUT_OF_BOUNDS: 转写词超出口播音画时长")
    output_duration_us = rate.output_us(source_duration_us)
    inserts = []
    eligible = [asset for asset in list_eligible_assets(db, task_id)
                if asset["asset_id"] != digital[0]["asset_id"]]
    observed_windows = load_observed_ad_windows(db, eligible)
    parts = brief.get("scriptSegments") or [script]
    segments = _semantic_segment_windows(script, words, parts, output_duration_us)
    missing = []
    requests = []
    for item in brief["broll"]:
        try:
            source_start, source_end = _anchor_time(script, words, item["anchorText"])
        except RuntimeError as error:
            context_gap = _unobserved_context_gap(script, words, item, segments, error)
            if context_gap is None:
                raise
            missing.append(context_gap)
            continue
        if seconds_to_us(source_end) > source_duration_us + 50_000:
            raise RuntimeError("STORYBOARD_TIMING_OUT_OF_BOUNDS: 分镜锚点超出口播音画时长")
        start_us = rate.output_us(seconds_to_us(source_start))
        end_us = rate.output_us(min(seconds_to_us(source_end), source_duration_us))
        if not 0 <= start_us < end_us <= output_duration_us:
            raise RuntimeError("STORYBOARD_TIMING_OUT_OF_BOUNDS: 分镜锚点区间无效")
        requests.append((item, start_us, end_us))
    requested_windows = sorted((start_us, end_us) for _item, start_us, end_us in requests)
    if any(right_start < left_end for (_left_start, left_end),
           (right_start, _right_end) in zip(requested_windows, requested_windows[1:])):
        raise RuntimeError("STORYBOARD_OVERLAP: 补充画面锚点重叠，需重新规划")
    matches = match_ad_shots(
        [{"query": item["query"],
          "type": "image" if item["kind"] == "image" else "video"}
         for item, _start_us, _end_us in requests],
        [{"dur": (end_us - start_us) / 1_000_000,
          "start": start_us / 1_000_000, "end": end_us / 1_000_000}
         for _item, start_us, end_us in requests],
        observed_windows, "9:16", transition_handle_seconds=0,
    )
    for (item, start_us, end_us), selected in zip(requests, matches, strict=True):
        if selected is None:
            missing.append({**item, "outputStartUs": start_us, "outputEndUs": end_us, "reason": "当前任务范围没有可分配的内容、画幅和时长均合格的已观察素材"})
            continue
        is_image = selected["media_kind"] == "image"
        inserts.append(
            {
                "narrative_text": item["anchorText"],
                "narrative_start": start_us / 1_000_000,
                "narrative_end": end_us / 1_000_000,
                "asset_id": selected["asset_id"],
                "source_start": 0.0 if is_image else selected["source_start"],
                "role": "information_card" if is_image else "broll",
                "mix_form": "画中画" if is_image else "broll全屏",
                "audio_policy": "mute",
                "source_type": "系统推荐",
                "system_suggestion": (
                    f"画面需求：{item['query']}；画面依据："
                    f"{selected['title']} {selected['description']}；画幅和时长已核对"
                ),
            }
        )
    inserts.sort(key=lambda card: (card["narrative_start"], card["narrative_end"]))
    cards = []
    cursor_us = 0

    def presenter_card(start_us: int, end_us: int) -> dict:
        return {
            "narrative_text": script,
            "narrative_start": start_us / 1_000_000,
            "narrative_end": end_us / 1_000_000,
            "asset_id": digital[0]["asset_id"],
            "role": "digital_human",
            "mix_form": "数字人全屏",
            "audio_policy": "keep",
            "source_type": "用户指定",
            "system_suggestion": "锁定的数字人口播主轴，声音和顺序不可切断",
        }

    for insert in inserts:
        start_us = seconds_to_us(insert["narrative_start"])
        end_us = seconds_to_us(insert["narrative_end"])
        if start_us < cursor_us:
            raise RuntimeError("STORYBOARD_OVERLAP: 补充画面锚点重叠，需重新规划")
        if start_us > cursor_us:
            cards.append(presenter_card(cursor_us, start_us))
        cards.append(insert)
        cursor_us = end_us
    if cursor_us < output_duration_us:
        cards.append(presenter_card(cursor_us, output_duration_us))
    grouped_cards = []
    for segment in segments:
        for card in cards:
            card_start = seconds_to_us(card["narrative_start"])
            card_end = seconds_to_us(card["narrative_end"])
            start_us = max(card_start, segment["start_us"])
            end_us = min(card_end, segment["end_us"])
            if start_us >= end_us:
                continue
            piece = dict(card)
            piece["narrative_text"] = segment["text"]
            piece["narrative_start"] = start_us / 1_000_000
            piece["narrative_end"] = end_us / 1_000_000
            piece["script_segment_index"] = segment["index"]
            if piece["role"] == "broll":
                piece["source_start"] = card["source_start"] + (start_us - card_start) / 1_000_000
                piece["source_end"] = card["source_start"] + (end_us - card_start) / 1_000_000
            else:
                piece["source_start"] = piece["source_end"] = 0.0
            grouped_cards.append(piece)
    for index, card in enumerate(grouped_cards):
        card["card_id"] = f"{task_id}#{index}"
        card["idx"] = index
    return grouped_cards, missing, segments


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
    transcript_state = project_transcript_state(
        project_root, task, verify_source_content=True
    )
    if transcript_state["status"] == "needs_review":
        raise RuntimeError(
            "TRANSCRIPT_REVIEW_REQUIRED: 口播与锁定文案存在关键疑点，请回听核对"
        )
    if transcript_state["status"] not in {"aligned", "reviewed"}:
        raise RuntimeError("TRANSCRIPTION_STALE: 当前转写未绑定锁定文案和底片")
    from .speech_alignment import reviewed_words
    words = reviewed_words(project_root, script_path.read_text(encoding="utf-8"), words, transcript_state)
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
            card["source_start"],
            card["source_end"],
            card["audio_policy"],
            card["source_type"],
            card["system_suggestion"],
            0,
            card["script_segment_id"],
        )
        for card in cards
    ]


def _persist_cards(db, task_id: str, script_sha256: str,
                   cards: list[dict], segments: list[dict]) -> bool:
    with db.transaction(immediate=True) as connection:
        segment_ids = {}
        for segment in segments:
            row = create_script_segment(
                db, task_id, script_sha256, segment["index"], segment["text"],
                segment["start_us"], segment["end_us"],
            )
            segment_ids[segment["index"]] = row["segment_id"]
        for card in cards:
            card["script_segment_id"] = segment_ids[card["script_segment_index"]]
        existing_rows = connection.execute(
            "SELECT card_id,idx,narrative_text,narrative_start,narrative_end,"
            "asset_id,mix_form,enter_tc,exit_tc,audio_policy_draft,source_type,"
            "system_suggestion,confirm_status,script_segment_id FROM scene_cards "
            "WHERE task_id=? ORDER BY idx,card_id",
            (task_id,),
        ).fetchall()
        if existing_rows:
            if [tuple(row) for row in existing_rows] != _expected_card_rows(cards):
                raise RuntimeError("STORYBOARD_EXISTS: 任务已有不同分镜，不能静默覆盖")
            return True
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
                  source_type,system_suggestion,confirm_status,script_segment_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
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
                    card["source_start"],
                    card["source_end"],
                    card["audio_policy"],
                    card["source_type"],
                    card["system_suggestion"],
                    card["script_segment_id"],
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
    presenter = (get_task(db, task_id).get("brief") or {}).get("digital_human_source") or {}
    profile = probe_digital_render_profile(presenter.get("path", ""))
    cards, missing, segments = build_digital_storyboard_cards(
        db,
        task_id,
        script,
        words,
        brief,
        profile["source_duration_us"],
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

    recovered_existing = _persist_cards(db, task_id, script_sha256, cards, segments)
    from .creative_assistance import register_digital_missing
    with db.transaction(immediate=True):
        register_digital_missing(db, task_id, script_sha256, missing)
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
