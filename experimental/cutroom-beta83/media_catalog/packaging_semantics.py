"""Resolve model supplied packaging text against measured speech and scene time."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from .ad_artifact_binding import ad_artifact_hashes_match
from .planning_finalize import _anchor_time, _validate_words


def resolve_measured_span(scenes: list[dict], start: float, end: float) -> dict | None:
    """Bind an intact speech interval to one shot or a continuous shot range.

    Timeline anchors already support cross-shot rendering. Never stretch, clip,
    or bridge missing/overlapping picture time just to place a recommendation.
    """
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) for v in (start, end)) or not 0 <= start < end:
        return None
    try:
        ordered = sorted(scenes, key=lambda scene: float(scene["narrative_start"]))
        covering = []
        cursor = start
        for scene in ordered:
            a, b = float(scene["narrative_start"]), float(scene["narrative_end"])
            if not all(math.isfinite(v) for v in (a, b)) or not 0 <= a < b:
                return None
            if b <= start or a >= end:
                continue
            left, right = max(a, start), min(b, end)
            if abs(left - cursor) > .000001 or not scene.get("card_id"):
                return None
            covering.append(scene["card_id"])
            cursor = right
        if not covering or abs(cursor - end) > .000001 or len(set(covering)) != len(covering):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {"anchor_kind": "scene" if len(covering) == 1 else "timeline",
            "anchor_ref": covering[0] if len(covering) == 1 else None,
            "scene_ids": covering}


def _model_title_sfx(package: object, resolved: dict[str, dict]) -> None:
    """Keep only a model-selected sound tied to a verified visible title event."""
    if not isinstance(package, dict) or "title_card" not in resolved:
        return
    selected = package.get("sfx")
    if not isinstance(selected, dict) or selected.get("event") != "title_card":
        return
    asset_ref = selected.get("assetRef")
    if not isinstance(asset_ref, str) or not asset_ref.strip():
        return
    resolved["sfx"] = {
        "asset_ref": asset_ref.strip(), "follow_role": "title_card",
        "evidence_text": resolved["title_card"]["evidence_text"],
        "reason": "模型为已验证标题入场选择音效；时间跟随标题实例并由源文件时长限定",
    }


def load_bound_digital_evidence(db, project_root: Path, task_id: str) -> tuple | None:
    """Use only a completed model artifact bound to the current locked script."""
    if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
        return None
    row = db.connection.execute(
        "SELECT task_type,brief_json FROM tasks WHERE task_id=?", (task_id,),
    ).fetchone()
    if row is None or row["task_type"] not in {"digital_human", "digital-human", "数字人混剪"}:
        return None
    try:
        task_brief = json.loads(row["brief_json"] or "{}")
        script_record = task_brief["final_script"]
        relative = Path(script_record["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            return None
        root = (Path(project_root).resolve() / "runtime/workbench/tasks" / task_id).resolve()
        trusted = (Path(project_root).resolve() / "runtime/workbench/tasks").resolve()
        if not root.is_relative_to(trusted):
            return None
        script_path = (root / relative).resolve()
        if not script_path.is_relative_to(root):
            return None
        script_bytes = script_path.read_bytes()
        script_sha = hashlib.sha256(script_bytes).hexdigest()
        storyboard = root / "artifacts/storyboard"
        brief_path = storyboard / "brief.json"
        metadata = json.loads((storyboard / "recommendation.json").read_text(encoding="utf-8"))
        brief_bytes = brief_path.read_bytes()
        if (metadata.get("state") != "complete"
                or script_sha != script_record.get("sha256")
                or script_sha != metadata.get("script_sha256")
                or hashlib.sha256(brief_bytes).hexdigest() != metadata.get("brief_sha256")):
            return None
        script = script_bytes.decode("utf-8")
        brief = json.loads(brief_bytes.decode("utf-8"))
        words = _validate_words(root / "artifacts/transcript.words.json")
        if not isinstance(brief, dict):
            return None
        from .speech_alignment import reviewed_words
        from .transcript_reconciliation import project_transcript_state
        from .tasks import get_task
        review = project_transcript_state(Path(project_root), get_task(db, task_id))
        words = reviewed_words(project_root, script, words, review or {})
        return script, words, brief
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, RuntimeError):
        return None


def suggest_digital_instances(
    script: str, words: list[dict], brief: dict, scenes: list[dict],
) -> dict[str, dict]:
    """Return only suggestions with a real script or scene association.

    The model supplies the meaning and literal quote. This function owns seconds
    and scene IDs. Missing or unverifiable model output remains unset.
    """
    if not isinstance(script, str) or not isinstance(brief, dict) or not words:
        return {}
    ordered = sorted(
        (scene for scene in scenes
         if float(scene["narrative_end"]) > float(scene["narrative_start"])),
        key=lambda scene: float(scene["narrative_start"]),
    )
    if not ordered:
        return {}
    result = {}
    title = brief.get("title")
    if isinstance(title, dict):
        title_text = title.get("text")
        requested = title.get("durationSec")
        first = ordered[0]
        if (isinstance(title_text, str) and title_text.strip()
                and len(title_text.strip()) <= 40
                and str(first.get("narrative_text") or "").strip()
                and isinstance(requested, (int, float)) and not isinstance(requested, bool)
                and 0 < requested <= 10):
            start = float(first["narrative_start"])
            end = min(float(first["narrative_end"]), start + float(requested))
            if end > start:
                result["title_card"] = {
                    "content_text": title_text.strip(), "anchor_kind": "scene",
                    "anchor_ref": first["card_id"], "start_seconds": start,
                    "end_seconds": end,
                    "evidence_text": str(first.get("narrative_text") or "").strip(),
                    "reason": "模型依据锁定文案概括主题；时间由开场镜头确定",
                }
    quotes = brief.get("punchLines")
    if not isinstance(quotes, list):
        _model_title_sfx(brief.get("packaging"), result)
        return result
    for quote in quotes:
        if (not isinstance(quote, str) or not quote.strip()
                or len(quote.strip()) > 40 or script.count(quote) != 1):
            continue
        try:
            source_start, source_end = _anchor_time(script, words, quote)
        except (RuntimeError, TypeError, ValueError):
            continue
        # The digital-human render uses the locked 1.25 playback rate.
        start, end = source_start / 1.25, source_end / 1.25
        placement = resolve_measured_span(ordered, start, end)
        if placement is None:
            continue
        result["fancy_text"] = {
            "content_text": quote, **placement, "start_seconds": start,
            "end_seconds": end, "evidence_text": quote,
            "reason": "模型选取锁定文案原句；完整区间由词级时间戳定位，可跨连续镜头",
        }
        break
    _model_title_sfx(brief.get("packaging"), result)
    return result


def load_bound_ad_evidence(db, project_root: Path, task_id: str) -> tuple | None:
    """Read only a finalized ad model plan bound to the current script and TTS."""
    if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
        return None
    row = db.connection.execute(
        "SELECT task_type,brief_json FROM tasks WHERE task_id=?", (task_id,),
    ).fetchone()
    if row is None or row["task_type"] not in {"ad_mix", "information_flow_ad", "ad", "信息流广告"}:
        return None
    try:
        brief = json.loads(row["brief_json"] or "{}")
        record = brief["final_script"]
        relative = Path(record["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            return None
        root = (Path(project_root).resolve() / "runtime/workbench/tasks" / task_id).resolve()
        trusted = (Path(project_root).resolve() / "runtime/workbench/tasks").resolve()
        if not root.is_relative_to(trusted):
            return None
        script_path = (root / relative).resolve()
        if not script_path.is_relative_to(root):
            return None
        script_bytes = script_path.read_bytes()
        script_sha = hashlib.sha256(script_bytes).hexdigest()
        ad_dir = root / "artifacts/ad"
        metadata = json.loads((ad_dir / "recommendation.json").read_text(encoding="utf-8"))
        from .voice_ownership import read_voice_ownership
        ownership=read_voice_ownership(db,task_id)
        managed=ownership.branch!='legacy'
        if (script_sha != record.get("sha256")
                or script_sha != metadata.get("script_sha256")
                or not isinstance(metadata.get("artifact_sha256"), dict)
                or (not managed and not ad_artifact_hashes_match(ad_dir, metadata))):
            return None
        script = script_bytes.decode("utf-8")
        if managed:
            from .voice_bundle_service import resolve_ad_render_inputs
            current=resolve_ad_render_inputs(db,Path(project_root),task_id,script,str(brief.get('aspect_ratio')))
            return script,current['plan'],current['timing']
        plan = json.loads((ad_dir / "adplan.json").read_text(encoding="utf-8"))
        timing = json.loads((ad_dir / "adtiming.json").read_text(encoding="utf-8"))
        planned = plan.get("segments")
        timed = timing.get("segments")
        if (not isinstance(planned, list) or not planned
                or not isinstance(timed, list) or len(planned) != len(timed)
                or "".join(item.get("text", "") for item in planned) != script
                or any(a.get("text") != b.get("text") for a, b in zip(planned, timed))):
            return None
        previous = 0.0
        for segment in timed:
            start, end = float(segment["start"]), float(segment["end"])
            if (not math.isfinite(start) or not math.isfinite(end)
                    or abs(start - previous) > 0.02 or end <= start):
                return None
            previous = end
        if abs(float(timing["total"]) - previous) > 0.03:
            return None
        return script, plan, timing
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, RuntimeError, AttributeError):
        return None


def suggest_ad_instances(
    script: str, plan: dict, timing: dict, segments: list[dict], scenes: list[dict],
) -> dict[str, dict]:
    """Resolve the model's literal sentence anchor to measured TTS and current shots."""
    if not isinstance(plan, dict) or not isinstance(timing, dict):
        return {}
    package = plan.get("packaging")
    planned = plan.get("segments")
    timed = timing.get("segments")
    if (not isinstance(package, dict) or not isinstance(planned, list)
            or not isinstance(timed, list) or len(planned) != len(timed)
            or len(planned) != len(segments)
            or "".join(item.get("text", "") for item in planned) != script):
        return {}
    for index, (model_segment, measured, stored) in enumerate(zip(planned, timed, segments)):
        if (stored.get("idx") != index or model_segment.get("text") != measured.get("text")
                or stored.get("text") != model_segment.get("text")):
            return {}
    result = {}
    for role, field in (("title_card", "title"), ("fancy_text", "fancyText")):
        item = package.get(field)
        if not isinstance(item, dict):
            continue
        content, anchor = item.get("text"), item.get("anchorText")
        if (not isinstance(content, str) or not content.strip() or len(content.strip()) > 40
                or not isinstance(anchor, str) or not anchor.strip()
                or script.count(anchor) != 1):
            continue
        candidates = [i for i, segment in enumerate(planned)
                      if anchor in segment.get("text", "")]
        if len(candidates) != 1:
            continue
        index = candidates[0]
        sentence = planned[index]["text"]
        parts = [text.strip() for text in re.split(r"[。！？；\n]+", sentence) if text.strip()]
        clean = lambda text: text.strip().rstrip("。！？；\n").strip()
        # Only a complete, single spoken sentence owns this measured interval.
        if len(parts) != 1 or clean(anchor) != parts[0]:
            continue
        if role == "fancy_text" and clean(content) != parts[0]:
            continue
        try:
            start, end = float(timed[index]["start"]), float(timed[index]["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        linked = [scene for scene in scenes
                  if scene.get("script_segment_id") == segments[index].get("segment_id")
                  and scene.get("narrative_text") == sentence
                  and float(scene["narrative_end"]) > float(scene["narrative_start"])]
        linked.sort(key=lambda scene: float(scene["narrative_start"]))
        if not linked:
            continue
        placement = resolve_measured_span(linked, start, end)
        if placement is None:
            continue
        result[role] = {
            "content_text": content.strip(), **placement,
            "start_seconds": start, "end_seconds": end, "evidence_text": anchor,
            "reason": "模型依据锁定广告文案选句；使用完整配音句级区间，可跨连续镜头",
        }
    _model_title_sfx(package, result)
    return result
