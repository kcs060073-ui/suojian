"""Conservative, evidence-bound checks between a locked script and Whisper words.

This is a review aid, not proof that ASR text is the spoken truth. It flags changes
that could alter meaning and leaves ordinary character substitutions to listening.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
import uuid

from .task_script import read_task_script


_TRADITIONAL = str.maketrans({
    "綠": "绿", "電": "电", "綜": "综", "合": "合", "價": "价",
    "園": "园", "區": "区", "數": "数", "據": "据", "萬": "万",
    "億": "亿", "貳": "二", "兩": "两", "貴": "贵", "費": "费",
    "沒": "没", "無": "无", "別": "别", "頂": "顶",
})
_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
           "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
_NUMERAL = r"[零〇一二两三四五六七八九十百千万亿]+"
_NEGATIONS = set("不没无未非别勿")
_NUMBER_CHARS = set("0123456789.%/")


def _number_value(token: str) -> int:
    if all(char in _DIGITS for char in token):
        return int("".join(str(_DIGITS[char]) for char in token))
    total = section = digit = 0
    for char in token:
        if char in _DIGITS:
            digit = _DIGITS[char]
        elif char in {"十", "百", "千"}:
            section += (digit or 1) * _UNITS[char]
            digit = 0
        elif char in {"万", "亿"}:
            total += (section + digit) * _UNITS[char]
            section = digit = 0
    return total + section + digit


def _normalize_with_positions(text: str, positions: list[tuple]) -> tuple[str, list[tuple]]:
    chars = []
    bounds = []
    for char, bound in zip(text, positions):
        for converted in unicodedata.normalize("NFKC", char).translate(_TRADITIONAL).lower():
            chars.append(converted)
            bounds.append(bound)
    text = "".join(chars)

    def replace(pattern, replacement):
        nonlocal text, bounds
        out, mapped, last = [], [], 0
        for match in re.finditer(pattern, text):
            out.append(text[last:match.start()])
            mapped.extend(bounds[last:match.start()])
            value = replacement(match)
            out.append(value)
            matched = bounds[match.start():match.end()]
            mapped.extend([(matched[0][0], matched[-1][1])] * len(value))
            last = match.end()
        out.append(text[last:])
        mapped.extend(bounds[last:])
        text, bounds = "".join(out), mapped

    replace(
        rf"百分之({_NUMERAL}|\d+)",
        lambda match: f"{_number_value(match[1]) if not match[1].isdigit() else match[1]}%",
    )
    replace(
        rf"({_NUMERAL})分之({_NUMERAL})",
        lambda match: f"{_number_value(match[2])}/{_number_value(match[1])}",
    )
    replace(_NUMERAL, lambda match: str(_number_value(match[0])))
    selected = [(char, bound) for char, bound in zip(text, bounds)
                if char.isalnum() or char in "%./"]
    return "".join(char for char, _ in selected), [bound for _, bound in selected]


def _normalize(text: str) -> str:
    return _normalize_with_positions(text, [(None, None)] * len(text))[0]


def _numeric_span(text: str, index: int) -> tuple[int, int] | None:
    if not text:
        return None
    index = min(max(index, 0), len(text) - 1)
    if text[index] not in _NUMBER_CHARS:
        index = min(max(index - 1, 0), len(text) - 1)
    if text[index] not in _NUMBER_CHARS:
        return None
    left = right = index
    while left and text[left - 1] in _NUMBER_CHARS:
        left -= 1
    while right + 1 < len(text) and text[right + 1] in _NUMBER_CHARS:
        right += 1
    return left, right + 1


def _numeric_token(text: str, index: int) -> str:
    span = _numeric_span(text, index)
    return text[span[0]:span[1]] if span is not None else ""


def reconcile_transcript(script: str, words: list[dict]) -> dict:
    """Return located material疑点; time bounds refer to observed Whisper words."""
    expected = _normalize(script)
    raw_observed = ""
    raw_positions = []
    for word in words:
        raw_observed += word["w"]
        raw_positions.extend(
            [(float(word["start"]), float(word["end"]))] * len(word["w"])
        )
    observed, positions = _normalize_with_positions(raw_observed, raw_positions)
    alignment = SequenceMatcher(None, expected, observed, autojunk=False)
    # A character diff cannot locate meaningful word errors across different
    # long-form scripts. Keep this blocked for review, with a pairing-level
    # explanation instead of spurious number/negation locations.
    if min(len(expected), len(observed)) >= 80 and alignment.ratio() < 0.55:
        return {
            "status": "needs_review",
            "findings": [{
                "kind": "content_mismatch",
                "script_excerpt": script.strip()[:160],
                "recognized_excerpt": raw_observed.strip()[:160],
                "audio_start": positions[0][0] if positions else None,
                "audio_end": positions[-1][1] if positions else None,
                "location_note": "整体内容难以对应，不能逐词可靠定位",
            }],
            "method": "whisper_pairing_check_v1",
            "note": "口播识别内容与文案整体差异较大。请先检查是否选错视频或文案；识别也可能有误，请播放原视频核实。",
        }
    findings = []
    for tag, i1, i2, j1, j2 in alignment.get_opcodes():
        if tag == "equal":
            continue
        script_part, heard_part = expected[i1:i2], observed[j1:j2]
        script_number = _numeric_token(expected, i1)
        heard_number = _numeric_token(observed, j1)
        if (any(char in _NUMBER_CHARS for char in script_part + heard_part)
                and (script_number or heard_number)
                and script_number != heard_number):
            kind = "number_mismatch"
            script_excerpt = script_number or script_part
            heard_excerpt = heard_number or heard_part
        elif any(char in _NEGATIONS for char in script_part + heard_part):
            kind = "negation_mismatch"
            script_excerpt, heard_excerpt = script_part, heard_part
        elif tag == "delete" and len(script_part) >= 3:
            kind = "missing_script_span"
            script_excerpt, heard_excerpt = script_part, heard_part
        else:
            continue
        if positions:
            # A deletion at the end of a number points to the next word in
            # SequenceMatcher. Seek the whole observed number, not that suffix.
            heard_span = _numeric_span(observed, j1) if kind == "number_mismatch" else None
            near = heard_span[0] if heard_span is not None else min(j1, len(positions) - 1)
            end = heard_span[1] - 1 if heard_span is not None else min(max(j2 - 1, near), len(positions) - 1)
            audio_start, audio_end = positions[near][0], positions[end][1]
        else:
            audio_start = audio_end = None
        findings.append({
            "kind": kind,
            "script_excerpt": script_excerpt,
            "recognized_excerpt": heard_excerpt,
            "audio_start": audio_start,
            "audio_end": audio_end,
            "location_note": "依据识别词时间戳估计；请回听原声核对",
        })
    return {
        "status": "needs_review" if findings else "aligned",
        "findings": findings,
        "method": "whisper_word_alignment_v2",
        "note": "机器识别不能证明原声与文案完全一致；关键差异需回听核对。",
    }


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _task_artifacts(project_root: Path, task: dict) -> Path | None:
    task_id = task.get("task_id")
    if (not isinstance(task_id, str) or not task_id
            or Path(task_id).name != task_id or task_id in {".", ".."}):
        return None
    root = Path(project_root).resolve() / "runtime" / "workbench" / "tasks"
    task_root = (root / task_id).resolve()
    if not task_root.is_relative_to(root):
        return None
    return task_root / "artifacts"


_PASSAGE_MAPPINGS = {}


def _passage_normalize(text, positions, mapping):
    converted, bounds = [], []
    for char, bound in zip(text, positions):
        value = mapping.get(char, char)
        converted.append(value)
        bounds.extend([bound] * len(value))
    return _normalize_with_positions("".join(converted), bounds)


def transcript_passages(script: str, words: list[dict], project_root=None) -> list[dict]:
    """Original text with conservative seeks into measured, unsped source audio.

    Unmatched text remains visible. Never distribute time by character count or
    confuse the original source clock with the downstream 1.25x editing clock.
    """
    from .speech_alignment import _checked
    rows = [{"id": _digest({"start": m.start(), "text": m.group()}),
             "text": m.group(), "script_start": m.start(), "script_end": m.end(),
             "audio_start": None, "audio_end": None,
             "location_note": "尚不能可靠定位，请通过原视频回听"}
            for m in re.finditer(r"[^。！？；\n]+[。！？；\n]*", script) if m.group().strip()]
    try:
        checked = _checked(words)
    except (ValueError, TypeError, KeyError):
        return rows
    raw = "".join(w["w"] for w in checked)
    mapping = {}
    if project_root is not None:
        from .speech_alignment import _mapping
        key = (str(Path(project_root).resolve()), "".join(sorted(set(script + raw))))
        mapping = _PASSAGE_MAPPINGS.get(key)
        if mapping is None:
            mapping = _mapping(project_root, script + raw)
            # Bounded cache of successful text conversions; failures are retryable.
            if mapping:
                if len(_PASSAGE_MAPPINGS) >= 16:
                    _PASSAGE_MAPPINGS.pop(next(iter(_PASSAGE_MAPPINGS)))
                _PASSAGE_MAPPINGS[key] = mapping
    expected, script_bounds = _passage_normalize(script, [(i, i + 1) for i in range(len(script))], mapping)
    observed, audio_bounds = _passage_normalize(raw,
        [(float(w["start"]), float(w["end"])) for w in checked for _ in w["w"]], mapping)
    alignment = SequenceMatcher(None, expected, observed, autojunk=False)
    if not expected or not observed or alignment.ratio() < .55:
        return rows
    opcodes = alignment.get_opcodes()
    for row in rows:
        indices = [i for i, (start, end) in enumerate(script_bounds)
                   if start >= row["script_start"] and end <= row["script_end"]]
        if not indices:
            continue
        start, end = indices[0], indices[-1] + 1
        target = expected[start:end]
        # Repeated phrases are unambiguous only when the full ordered text is
        # observed. If one occurrence is missing, do not guess which was spoken.
        if expected != observed and (expected.count(target) != 1 or observed.count(target) > 1):
            continue
        measured = []
        matched = 0
        for tag, i1, i2, j1, j2 in opcodes:
            lo, hi = max(start, i1), min(end, i2)
            if tag == "equal" and hi > lo:
                matched += hi - lo
                measured.extend(range(j1 + lo - i1, j1 + hi - i1))
            elif tag == "replace" and start <= i1 < i2 <= end:
                measured.extend(range(j1, j2))
            elif tag == "insert" and start < i1 < end:
                measured.extend(range(j1, j2))
        # Local ASR typos may still have a reliable surrounding phrase. These
        # are listening targets, never evidence that the words are correct.
        if not measured or matched / (end - start) < .65:
            continue
        a = audio_bounds[min(measured)][0]
        b = audio_bounds[max(measured)][1]
        if b > a:
            row.update(audio_start=a, audio_end=b,
                       location_note="依据原视频识别词的实际时间定位；请回听核对")
    return rows


def project_transcript_state(
    project_root: Path, task: dict, *, verify_source_content: bool = False, include_passages: bool = True
) -> dict | None:
    """Read the current pairing only; stale artifacts cannot grant approval."""
    if task.get("task_type") != "digital_human":
        return None
    artifacts = _task_artifacts(project_root, task)
    if artifacts is None:
        return {"status": "stale", "digest": None, "findings": []}
    metadata_path = artifacts / "transcription.json"
    words_path = artifacts / "transcript.words.json"
    srt_path = artifacts / "transcript.srt"
    if not metadata_path.is_file() or not words_path.is_file() or not srt_path.is_file():
        return {"status": "pending", "digest": None, "findings": []}
    script = read_task_script(Path(project_root), task)
    if not isinstance(script, str) or not script.strip():
        return {"status": "stale", "digest": None, "findings": []}
    brief = task.get("brief") or {}
    source_record = brief.get("digital_human_source") or {}
    try:
        source_path = Path(source_record["path"]).resolve(strict=True)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        words = json.loads(words_path.read_text(encoding="utf-8"))
        stat = source_path.stat()
        script_sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
        words_sha = _sha256(words_path)
        if (not isinstance(metadata, dict) or not srt_path.read_bytes().strip()
                or metadata.get("srt_sha256") not in (None, _sha256(srt_path))
                or not isinstance(words, list) or not words
                or any(not isinstance(item, dict) or not isinstance(item.get("w"), str)
                       or not isinstance(item.get("start"), (int, float))
                       or not isinstance(item.get("end"), (int, float)) for item in words)
                or script_sha != brief["final_script"]["sha256"]
                or metadata.get("script_sha256") != script_sha
                or metadata.get("source_path") != str(source_path)
                or metadata.get("source_size_bytes") != stat.st_size
                or metadata.get("source_mtime_ns") != stat.st_mtime_ns
                or (verify_source_content and metadata.get("source_sha256") is not None
                    and metadata["source_sha256"] != _sha256(source_path))
                or metadata.get("words_sha256") not in (None, words_sha)):
            raise ValueError("transcription binding changed")
        check = reconcile_transcript(script, words)
        binding = {
            "task_id": task["task_id"],
            "script_sha256": script_sha,
            "source_path": str(source_path),
            "source_size_bytes": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_sha256": metadata.get("source_sha256"),
            "words_sha256": words_sha,
            "transcription_digest": metadata.get("input_digest"),
            "check": check,
        }
        digest = _digest(binding)
        state = {"status": check["status"], "digest": digest,
                 "findings": check["findings"], "note": check["note"]}
        receipt_path = artifacts / "transcript-review.json"
        if check["status"] == "needs_review" and receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("invalid transcript review receipt")
            if (receipt.get("digest") == digest
                    and receipt.get("decision") == "matches_audio"):
                state["status"] = "reviewed"
                state["reviewedAt"] = receipt.get("reviewed_at")
        state["reviewRevision"] = 0
        decisions = {}
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("invalid transcript review receipt")
            if receipt.get("digest") == digest:
                revision = receipt.get("revision", 0)
                decisions = receipt.get("findings", {})
                if (type(revision) is not int or revision < 0 or not isinstance(decisions, dict)
                        or any(not isinstance(v, dict) or v.get("decision") not in {"matches_audio", "needs_fix"}
                               or not isinstance(v.get("note", ""), str) for v in decisions.values())):
                    raise ValueError("invalid transcript finding receipt")
                state["reviewRevision"] = revision
        state["findings"] = [{**finding, "id": _digest({"index": i, "finding": finding}),
            "review": decisions.get(_digest({"index": i, "finding": finding}))}
            for i, finding in enumerate(check["findings"])]
        if any((item.get("review") or {}).get("decision") == "needs_fix" for item in state["findings"]):
            state["status"] = "needs_review"
            state.pop("reviewedAt", None)
        if include_passages:
            state["passages"] = transcript_passages(script, words, project_root)
        return state
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"status": "stale", "digest": None, "findings": []}


def confirm_transcript_review(
    project_root: Path, task: dict, expected_digest: str, confirm_listened: bool
) -> dict:
    state = project_transcript_state(
        project_root, task, verify_source_content=True
    )
    if state is None or state["status"] not in {"needs_review", "reviewed"}:
        raise ValueError("TRANSCRIPT_REVIEW_UNAVAILABLE: 当前无可确认的疑点")
    if not isinstance(expected_digest, str) or expected_digest != state["digest"]:
        raise ValueError("TRANSCRIPT_REVIEW_STALE: 核对依据已变化，请刷新后回听")
    if confirm_listened is not True:
        raise ValueError("LISTEN_CONFIRMATION_REQUIRED: 请回听原声后确认")
    if any((item.get("review") or {}).get("decision") == "needs_fix" for item in state["findings"]):
        raise ValueError("TRANSCRIPT_FINDINGS_UNRESOLVED: 还有记录为需修改的内容，请先处理或回听核对")
    if state["status"] == "reviewed":
        return state
    artifacts = _task_artifacts(project_root, task)
    artifacts.mkdir(parents=True, exist_ok=True)
    receipt = {"digest": expected_digest, "decision": "matches_audio",
               "reviewed_at": datetime.now(timezone.utc).isoformat(),
               "revision": state["reviewRevision"] + 1,
               "findings": {item["id"]: item["review"] for item in state["findings"] if item.get("review")}}
    temporary = artifacts / f".transcript-review.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(receipt, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, artifacts / "transcript-review.json")
    finally:
        temporary.unlink(missing_ok=True)
    return project_transcript_state(project_root, task)
