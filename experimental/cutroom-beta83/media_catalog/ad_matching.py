"""Evidence-backed, whole-video allocation of ad source windows."""

from __future__ import annotations

from fractions import Fraction
import json
import math
import re

from .asset_index import _duration_seconds
from .segment_marks import current_marks
from .source_windows import usable_source_ranges
from .candidate_ranking import observed_text_match, _QUERY_SPLIT
from .quality_windows import quality_for_window, quality_source_ranges
from .duplicate_evidence import duplicates_for_window


TRANSITION_HANDLE_SECONDS = 0.4
MAX_SEARCH_STATES = 200_000


def _batches(values: list[str], size: int = 400):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def display_dimensions(kind: str, probe: dict) -> tuple[float, float] | None:
    """Dimensions after sample aspect, rotation and EXIF; shared by matching and UI."""
    if not isinstance(probe, dict):
        return None
    if kind == "image":
        width, height = probe.get("width"), probe.get("height")
        if probe.get("orientation") in {5, 6, 7, 8}:
            width, height = height, width
    else:
        streams = probe.get("streams") or []
        if not isinstance(streams, list):
            return None
        stream = next((item for item in streams
                       if isinstance(item, dict) and item.get("codec_type") == "video"), None)
        if stream is None:
            return None
        width, height = stream.get("width"), stream.get("height")
        try:
            sar = str(stream.get("sample_aspect_ratio") or "1:1").replace(":", "/")
            width = float(width) * float(Fraction(sar))
            side_data = stream.get("side_data_list") or []
            if not isinstance(side_data, list):
                return None
            rotations = [float(item["rotation"]) for item in side_data
                         if isinstance(item, dict) and "rotation" in item]
            tags = stream.get("tags", {})
            if not isinstance(tags, dict):
                return None
            if "rotate" in tags:
                rotations.append(float(tags["rotate"]))
            if rotations:
                quarters = [round(value / 90) for value in rotations]
                if any(abs(value - quarter * 90) > 1e-6
                       for value, quarter in zip(rotations, quarters, strict=True)):
                    return None
                if len({quarter % 4 for quarter in quarters}) != 1:
                    return None
                if quarters[0] % 2:
                    width, height = height, width
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            return None
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return None
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        return None
    return width, height


def _direction(kind: str, probe: dict) -> str | None:
    dimensions = display_dimensions(kind, probe)
    if dimensions is None or dimensions[0] == dimensions[1]:
        return None
    return "16:9" if dimensions[0] > dimensions[1] else "9:16"


def load_observed_ad_windows(db, assets: list[dict]) -> list[dict]:
    """Read current human/model observations in the task's eligible asset scope."""
    eligible = {item["asset_id"]: item for item in assets
                if item.get("media_kind") in {"video", "image"}
                and item.get("task_role") != "digital_human"}
    if not eligible:
        return []
    ids = sorted(eligible)
    observations = []
    marked_ids = set()
    for batch in _batches(ids):
        placeholders = ",".join("?" for _ in batch)
        observations.extend(db.connection.execute(
            f"""SELECT observation.* FROM visual_observations observation
                JOIN assets asset USING(asset_id)
                WHERE observation.asset_id IN ({placeholders})
                  AND observation.asset_fingerprint=asset.quick_fingerprint
                ORDER BY observation.asset_id,observation.source_start_us,
                         observation.source_end_us,
                         CASE observation.origin WHEN 'human' THEN 0 ELSE 1 END,
                         observation.observation_id DESC""",
            tuple(batch),
        ).fetchall())
        marked_ids.update(row[0] for row in db.connection.execute(
            f"""SELECT mark.asset_id FROM catalog_segment_marks mark
                JOIN assets asset USING(asset_id)
                WHERE mark.asset_id IN ({placeholders})
                  AND mark.asset_fingerprint=asset.quick_fingerprint""",
            tuple(batch),
        ).fetchall())

    effective_marks = {asset_id: current_marks(db, asset_id)
                       for asset_id in sorted(marked_ids)}
    replaced = {(asset_id, mark["base_start_us"], mark["base_end_us"])
                for asset_id, marks in effective_marks.items()
                for mark in marks.values()}
    preferred = {}
    refined = [row for row in observations if row["origin"] == "model"
               and row["method_version"].startswith("visual-content-v2-")]
    for row in observations:
        key = (row["asset_id"], row["source_start_us"], row["source_end_us"])
        # A broad historic summary must not reintroduce content across newer
        # boundaries. Human marks retain their existing precedence.
        superseded = row["origin"] == "model" and any(
            fine["asset_id"] == row["asset_id"]
            and (not row["method_version"].startswith("visual-content-v2-")
                 or fine["observation_id"] > row["observation_id"]) and (
                row["source_start_us"] == row["source_end_us"] == fine["source_start_us"] == fine["source_end_us"]
                or fine["source_start_us"] < row["source_end_us"] and fine["source_end_us"] > row["source_start_us"])
            for fine in refined)
        if key not in replaced and not superseded:
            preferred.setdefault(key, row)

    observed_ids = sorted({key[0] for key in preferred} | marked_ids)
    probes = {}
    hashes = {}
    fingerprints = {}
    file_ids = {}
    for batch in _batches(observed_ids):
        placeholders = ",".join("?" for _ in batch)
        probes.update({row["asset_id"]: row["result_json"]
                       for row in db.connection.execute(
                           f"SELECT asset_id,result_json FROM analysis_runs "
                           f"WHERE asset_id IN ({placeholders}) "
                           "AND stage='technical_probe' AND status='completed'",
                           tuple(batch),
                       ).fetchall()})
        for row in db.connection.execute(
                f"SELECT asset_id,full_sha256,quick_fingerprint FROM assets "
                f"WHERE asset_id IN ({placeholders})",tuple(batch)):
            hashes[row["asset_id"]]=row["full_sha256"]
            fingerprints[row["asset_id"]]=row["quick_fingerprint"]
        for row in db.connection.execute(
            f"SELECT asset_id,file_identity FROM asset_locations "
            f"WHERE asset_id IN ({placeholders}) AND state='available' "
            "AND file_identity IS NOT NULL AND file_identity<>''",
            tuple(batch),
        ).fetchall():
            file_ids.setdefault(row["asset_id"], row["file_identity"])

    metadata = {}
    for asset_id in observed_ids:
        try:
            probe = json.loads(probes.get(asset_id) or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(probe, dict):
            continue
        kind = eligible[asset_id]["media_kind"]
        direction = _direction(kind, probe)
        duration = _duration_seconds(probes.get(asset_id)) if kind == "video" else None
        if direction is None or kind == "video" and duration is None:
            continue
        metadata[asset_id] = {
            "direction": direction,
            "duration": duration,
            "identity_key": ("sha:" + hashes[asset_id] if hashes.get(asset_id)
                             else "file:" + file_ids[asset_id] if asset_id in file_ids
                             else "asset:" + asset_id),
        }

    from .shot_candidate_evidence import RequestEvidence
    # One reader belongs to this invocation only: repeated windows and duplicate
    # pairs share source checks, but another refresh always revalidates sources.
    evidence = RequestEvidence(db, [{**eligible[aid], "quick_fingerprint":fingerprints[aid]}
                                    for aid in metadata])
    windows = []

    def append(asset_id, start_us, end_us, title, description, segment_id=None,
               tags="", usage_status=None, motion=None):
        meta = metadata.get(asset_id)
        if meta is None or not (title or description or tags):
            return
        kind = eligible[asset_id]["media_kind"]
        if kind == "image":
            if (start_us, end_us) != (0, 0):
                return
            if any(mark["usage_status"] == "不建议使用"
                   for mark in effective_marks.get(asset_id, {}).values()):
                return
            safe_ranges = [(None, None)]
        else:
            if (start_us < 0 or end_us <= start_us
                    or end_us / 1_000_000 > meta["duration"] + 0.001):
                return
            safe_ranges = [piece for a, b in quality_source_ranges(db, asset_id, start_us, end_us, request_evidence=evidence)
                           for piece in usable_source_ranges(a, b, effective_marks.get(asset_id, {}).values())]
        for piece_start, piece_end in safe_ranges:
            windows.append({
                "asset_id": asset_id,
                "media_kind": kind,
                "relative_path": eligible[asset_id]["relative_path"],
                "source_mode": eligible[asset_id].get("source_mode"),
                "source_start": None if piece_start is None else piece_start / 1_000_000,
                "source_end": None if piece_end is None else piece_end / 1_000_000,
                "segment_id": segment_id,
                "title": title or "",
                "description": description or "",
                "tags": tags or "",
                "usage_status": usage_status,
                "direction": meta["direction"],
                "identity_key": meta["identity_key"],
                "motion_observation": motion or {},
                "quality_evidence": quality_for_window(db, asset_id, piece_start or 0, piece_end or 0, request_evidence=evidence),
                "duplicate_evidence": duplicates_for_window(db, asset_id, piece_start or 0, piece_end or 0, request_evidence=evidence),
            })

    def motion_record(row):
        row = dict(row)
        return {"status": "observed", "cameraMotion": row["camera_motion"],
                "entryMotion": row.get("entry_motion"), "exitMotion": row.get("exit_motion"),
                "sourceStartUs": row["source_start_us"], "sourceEndUs": row["source_end_us"],
                "observationId": row.get("observation_id")}

    for (asset_id, start, end), row in preferred.items():
        append(asset_id, start, end, row["title"], row["description"],
               row["source_segment_id"], motion=motion_record(row))
    for asset_id, marks in effective_marks.items():
        for mark in marks.values():
            if mark["usage_status"] == "不建议使用":
                continue
            append(asset_id, mark["source_start_us"], mark["source_end_us"],
                   mark["title"], mark["description"], tags=mark["tags"],
                   usage_status=mark["usage_status"], motion=motion_record(mark))
    windows.sort(key=lambda item: (item["asset_id"], item["source_start"] or 0,
                                   item["source_end"] or 0))
    return windows


def _content_score(query: str, candidate: dict) -> int:
    """Require complete text support plus title/tag support, not semantic proof."""
    evidence = observed_text_match(query, usage_status=candidate.get("usage_status"),
                                   **{key: candidate.get(key) or ""
                                         for key in ("title", "description", "tags")})
    terms = {"".join(term.casefold().split()) for term in _QUERY_SPLIT.split(query)
             if len("".join(term.split())) >= 2}
    if (not terms or evidence["excludedTerms"] or terms != set(evidence["matchedTerms"])
            or not any(support["field"] in {"title", "tags"} for support in evidence["support"])):
        return 0
    # Quality is a preference only after all requested text has evidence.
    return max(1, evidence["score"] - candidate.get("quality_evidence", {}).get("penalty", 0))


def _secondary_content_score(query: str, candidate: dict) -> int:
    """Full textual support can rank related options, never prove their subject.

    Partial multi-term hits cannot disambiguate terms such as 行车/吊车 and
    must not receive a relevance bonus over neutral park fallback footage.
    """
    evidence = observed_text_match(query, usage_status=candidate.get("usage_status"),
                                   **{key: candidate.get(key) or ""
                                         for key in ("title", "description", "tags")})
    terms = {"".join(term.casefold().split()) for term in _QUERY_SPLIT.split(query)
             if len("".join(term.split())) >= 2}
    return (evidence["score"] if terms and not evidence["excludedTerms"]
            and terms == set(evidence["matchedTerms"]) else 0)


def _conflicts(left: tuple[dict, float], right: tuple[dict, float],
               transition_handle_seconds: float) -> bool:
    a, a_duration = left
    b, b_duration = right
    from .duplicate_evidence import confirmed_window_duplicate
    def use(item, duration):
        start = item.get('source_start') or 0
        return {**item, 'kind': item['media_kind'], 'start': start,
                'end': start+duration}
    if confirmed_window_duplicate(use(a, a_duration), use(b, b_duration)) is not None:
        return True
    if a["identity_key"] != b["identity_key"]:
        return False
    if a["media_kind"] == "image" or b["media_kind"] == "image":
        return True
    a_start, b_start = a["source_start"], b["source_start"]
    a_end = a_start + a_duration + transition_handle_seconds
    b_end = b_start + b_duration + transition_handle_seconds
    overlap = min(a_end, b_end) - max(a_start, b_start)
    shorter = min(a_end - a_start, b_end - b_start)
    return overlap >= 0.5 and overlap / shorter >= 0.75


def _resource_key(item: dict) -> tuple:
    return (item["identity_key"], item["media_kind"],
            item["source_start"], item["source_end"])


def _independent_source_windows(options: list[list[tuple[int, dict]]], *, check_confirmed=True) -> bool:
    """Detect when identity windows can be modeled as independent resources."""
    by_identity = {}
    asset_ids = {item['asset_id'] for shot in options for _score, item in shot}
    for shot_options in options:
        for _score, item in shot_options:
            # Direct pairs are not interchangeable resources (A=B and B=C
            # never proves A=C). Keep them in the pair-aware assignment.
            if check_confirmed and any(r.get('decision') == 'same' and r.get('otherAssetId') in asset_ids
                   for r in item.get('duplicate_evidence', ())):
                return False
            by_identity.setdefault(item["identity_key"], set()).add(
                (item["media_kind"], item["source_start"], item["source_end"])
            )
    for windows in by_identity.values():
        if len(windows) <= 1:
            continue
        if any(kind != "video" for kind, _start, _end in windows):
            return False
        ordered = sorted(windows, key=lambda item: (item[1], item[2]))
        if any(left[2] > right[1] + 1e-6
               for left, right in zip(ordered, ordered[1:])):
            return False
    return True


def _match_independent_windows(options: list[list[tuple[int, dict]]]) -> list[dict | None]:
    """Maximize coverage, then relevance, for disjoint source windows."""
    if len({score for shot in options for score, _item in shot}) > 1:
        return _match_weighted_independent_windows(options)
    by_shot = []
    for shot_options in options:
        seen_keys = set()
        unique = []
        for _score, item in shot_options:
            key = _resource_key(item)
            if key not in seen_keys:
                seen_keys.add(key)
                unique.append((key, item))
        by_shot.append(unique)
    order = sorted(range(len(options)), key=lambda index: (len(by_shot[index]), index))
    owner_by_key = {}
    key_by_shot = [None] * len(options)
    selected = [None] * len(options)

    def bind(shot_index: int, key: tuple, item: dict) -> None:
        old_key = key_by_shot[shot_index]
        if old_key is not None and owner_by_key.get(old_key) == shot_index:
            del owner_by_key[old_key]
        owner_by_key[key] = shot_index
        key_by_shot[shot_index] = key
        selected[shot_index] = item

    def augment(shot_index: int, seen: set[tuple]) -> bool:
        # Preserve already selected timeline order when a free candidate exists.
        for key, item in by_shot[shot_index]:
            if key in seen:
                continue
            if key not in owner_by_key:
                seen.add(key)
                bind(shot_index, key, item)
                return True
        for key, item in by_shot[shot_index]:
            if key in seen:
                continue
            seen.add(key)
            owner = owner_by_key.get(key)
            if owner is not None and augment(owner, seen):
                bind(shot_index, key, item)
                return True
        return False

    for shot_index in order:
        augment(shot_index, set())
    return selected


def _match_weighted_independent_windows(
    options: list[list[tuple[int, dict]]],
) -> list[dict | None]:
    """Exact rectangular assignment with a private gap column for each shot."""
    shot_count = len(options)
    resource_columns = {}
    choices = []
    max_score = 0
    for shot_options in options:
        row = {}
        for score, item in shot_options:
            key = _resource_key(item)
            column = resource_columns.setdefault(key, len(resource_columns) + 1)
            if column not in row or score > row[column][0]:
                row[column] = (score, item)
            max_score = max(max_score, score)
        choices.append(row)
    resource_count = len(resource_columns)
    column_count = resource_count + shot_count
    # One filled shot must outrank every possible gain in relevance elsewhere.
    coverage_bonus = shot_count * max_score + 1
    unavailable = (shot_count + 1) * (coverage_bonus + max_score) + 1
    row_potential = [0] * (shot_count + 1)
    column_potential = [0] * (column_count + 1)
    owner = [0] * (column_count + 1)
    previous = [0] * (column_count + 1)
    for shot_index in range(1, shot_count + 1):
        owner[0] = shot_index
        column = 0
        shortest = [unavailable] * (column_count + 1)
        visited = [False] * (column_count + 1)
        while True:
            visited[column] = True
            active_shot = owner[column]
            next_column = 0
            delta = unavailable
            row = choices[active_shot - 1]
            for candidate_column in range(1, column_count + 1):
                if visited[candidate_column]:
                    continue
                choice = row.get(candidate_column)
                cost = -(coverage_bonus + choice[0]) if choice else (
                    0 if candidate_column > resource_count else unavailable
                )
                reduced = cost - row_potential[active_shot] - column_potential[candidate_column]
                if reduced < shortest[candidate_column]:
                    shortest[candidate_column] = reduced
                    previous[candidate_column] = column
                if shortest[candidate_column] < delta:
                    delta = shortest[candidate_column]
                    next_column = candidate_column
            for candidate_column in range(column_count + 1):
                if visited[candidate_column]:
                    row_potential[owner[candidate_column]] += delta
                    column_potential[candidate_column] -= delta
                else:
                    shortest[candidate_column] -= delta
            column = next_column
            if owner[column] == 0:
                break
        while column:
            prior = previous[column]
            owner[column] = owner[prior]
            column = prior
    result = [None] * shot_count
    for column in range(1, resource_count + 1):
        if owner[column] and column in choices[owner[column] - 1]:
            result[owner[column] - 1] = choices[owner[column] - 1][column][1]
    return result


def _match_pair_constrained_windows(options, timing_segments, handle):
    """Bound direct-pair search by exact independent weighted assignments.

    Each relaxation already accounts for occupied resources, coverage and score.
    Branch on a proven conflicting pair, never on a transitive duplicate group.
    """
    durations = [float(t['dur']) for t in timing_segments]
    choice_key = lambda item: (item['asset_id'], _resource_key(item))
    uses_by_choice = {}
    scores = []
    for index, shot in enumerate(options):
        scores.append({id(item): score for score, item in shot})
        for _score, item in shot:
            uses_by_choice.setdefault(choice_key(item), []).append((index, item))
    best, best_value = [None]*len(options), (-1, -1)
    pending = [(frozenset(), frozenset())]
    visited = set()
    universal_conflicts = {}
    while pending:
        banned, banned_at = pending.pop()
        state = (banned, banned_at)
        if state in visited:
            continue
        visited.add(state)
        if len(visited) > MAX_SEARCH_STATES:
            raise RuntimeError("AD_MATCH_SEARCH_LIMIT: \u5019\u9009\u7ec4\u5408\u8fc7\u591a\uff0c\u8bf7\u7f29\u5c0f\u7d20\u6750\u8303\u56f4\u6216\u4eba\u5de5\u9009\u6750")
        filtered = [[(score, item) for score, item in shot
                     if choice_key(item) not in banned
                     and (i, choice_key(item)) not in banned_at]
                    for i, shot in enumerate(options)]
        proposed = _match_independent_windows(filtered)
        value = (sum(item is not None for item in proposed),
                 sum(scores[i][id(item)] for i, item in enumerate(proposed) if item is not None))
        if value <= best_value:
            continue
        conflict = next(((i, j) for i, a in enumerate(proposed) if a is not None
                         for j, b in enumerate(proposed[:i]) if b is not None
                         and _conflicts((a, durations[i]), (b, durations[j]), handle)), None)
        if conflict is None:
            best, best_value = proposed, value
            continue
        i, j = conflict
        a_key, b_key = choice_key(proposed[i]), choice_key(proposed[j])
        pair = frozenset((a_key, b_key))
        if pair not in universal_conflicts:
            # Global exclusion is sound only if every available shot-duration
            # combination conflicts. Otherwise exclude just this shot choice.
            universal_conflicts[pair] = all(
                _conflicts((a, durations[ai]), (b, durations[bi]), handle)
                for ai, a in uses_by_choice[a_key]
                for bi, b in uses_by_choice[b_key] if ai != bi)
        if universal_conflicts[pair]:
            pending.extend((banned | {key}, banned_at) for key in (a_key, b_key))
        else:
            pending.extend((banned, banned_at | {(index, key)})
                           for index, key in ((i, a_key), (j, b_key)))
    return best


_GENERIC_SOURCES = {"selected_generic", "default_generic"}
_PROJECT_REFERENCE = re.compile(r"(?:本|该|此)(?:园区|项目|厂区|基地|厂房|仓库|园)|实拍|实景")
_PROJECT_NEGATION = re.compile(r"(?:并非|不是|不属于|不代表|非)(?:(?:本|该|此)(?:园区|项目|厂区|基地|厂房|仓库|园))?\s*$")


def _requires_project_source(planned: dict) -> bool:
    """Explicit project claims require project footage; negated examples do not."""
    for field in ("query", "text"):
        text = str(planned.get(field) or "")
        for reference in _PROJECT_REFERENCE.finditer(text):
            if not _PROJECT_NEGATION.search(text[max(0, reference.start()-8):reference.start()]):
                return True
    return False


def match_ad_shots(plan_segments: list[dict], timing_segments: list[dict],
                   candidates: list[dict], aspect_ratio: str, *,
                   transition_handle_seconds: float = TRANSITION_HANDLE_SECONDS,
                   _score_candidate=None, _reserved=(), _motion_context=None
                   ) -> list[dict | None]:
    """Allocate evidence-backed windows across the whole ad, preserving gaps."""
    if not isinstance(transition_handle_seconds, (int, float)) or \
            isinstance(transition_handle_seconds, bool) or \
            not math.isfinite(transition_handle_seconds) or transition_handle_seconds < 0:
        raise ValueError("AD_MATCH_TRANSITION_INVALID: 转场预留时长无效")
    if len(plan_segments) != len(timing_segments):
        raise ValueError("AD_MATCH_TIMING_INVALID: 分镜与配音时间表数量不一致")
    from .motion_ranking import fixed_reservations
    _reserved = tuple(_reserved) + tuple(fixed_reservations(_motion_context))
    options = []
    for planned, timed in zip(plan_segments, timing_segments, strict=True):
        duration = float(timed["dur"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("AD_MATCH_TIMING_INVALID: 配音时间无效")
        eligible = []
        for item in candidates:
            if item.get("source_mode") in _GENERIC_SOURCES and _requires_project_source(planned):
                continue
            if item["media_kind"] != planned["type"] or item["direction"] != aspect_ratio:
                continue
            if item["media_kind"] == "video" and (
                item["source_start"] is None or item["source_end"] is None
                or item["source_end"] - item["source_start"] + 1e-6
                < duration + transition_handle_seconds
            ):
                continue
            if any(_conflicts((item, duration), prior, transition_handle_seconds)
                   for prior in _reserved):
                continue
            scorer = _content_score if _score_candidate is None else _score_candidate
            score = scorer(str(planned.get("query") or ""), item)
            if score:
                evidence = observed_text_match(str(planned.get("query") or ""),
                    usage_status=item.get("usage_status"),
                    **{key: item.get(key) or "" for key in ("title", "description", "tags")})
                evidence["reason"] += "；仅为观察文字匹配，仍需人工核对画面主体与旁白语义"
                if item.get("source_mode") in _GENERIC_SOURCES:
                    evidence["sourceUse"] = "illustration"
                    evidence["sourceMode"] = item["source_mode"]
                    evidence["reason"] += "；通用示意，不能作为本项目实拍或项目事实的证明"
                eligible.append((score, {**item, "match_evidence": evidence}))
        eligible.sort(key=lambda pair: (-pair[0], pair[1]["asset_id"],
                                        pair[1]["source_start"] or 0))
        options.append(eligible)

    def finish(best):
        from .motion_ranking import prefer_motion, subsequence_context
        # A refresh retains unmatched old cards. Freeze those sources before
        # allocating the remaining slots; every retry removes editable slots.
        retained = {i for i, item in enumerate(best) if item is None
                    and _motion_context is not None
                    and _motion_context['items'][_motion_context['positions'][i]] is not None}
        if retained:
            remaining = [i for i in range(len(best)) if i not in retained]
            result = [None] * len(best)
            if remaining:
                replacements = match_ad_shots(
                    [plan_segments[i] for i in remaining],
                    [timing_segments[i] for i in remaining], candidates, aspect_ratio,
                    transition_handle_seconds=transition_handle_seconds,
                    _score_candidate=_score_candidate, _reserved=_reserved,
                    _motion_context=subsequence_context([None] * len(best), timing_segments, remaining, _motion_context))
                for i, item in zip(remaining, replacements, strict=True):
                    result[i] = item
            return result
        best = prefer_motion(best, options, timing_segments, _conflicts,
                             transition_handle_seconds, context=_motion_context)
        return _selected_windows(best, timing_segments)

    if _independent_source_windows(options):
        best = _match_independent_windows(options)
        return finish(best)
    if _independent_source_windows(options, check_confirmed=False):
        best = _match_pair_constrained_windows(options, timing_segments, transition_handle_seconds)
        return finish(best)

    order = sorted(range(len(plan_segments)), key=lambda index: (len(options[index]), index))
    best_count = -1
    best_score = -1
    best = [None] * len(plan_segments)
    current = [None] * len(plan_segments)
    states = 0
    optimistic_scores = [max((score for score, _ in options[index]), default=0)
                         for index in order]
    suffix_scores = [0] * (len(order) + 1)
    for index in range(len(order) - 1, -1, -1):
        suffix_scores[index] = suffix_scores[index + 1] + optimistic_scores[index]

    def visit(position: int, filled: int, score: int) -> None:
        nonlocal best_count, best_score, best, states
        states += 1
        if states > MAX_SEARCH_STATES:
            raise RuntimeError("AD_MATCH_SEARCH_LIMIT: 候选组合过多，请缩小素材范围或人工选材")
        remaining = len(order) - position
        if filled + remaining < best_count or (
            filled + remaining == best_count
            and score + suffix_scores[position] <= best_score
        ):
            return
        if position == len(order):
            if filled > best_count or filled == best_count and score > best_score:
                best_count, best_score = filled, score
                best = current.copy()
            return
        shot_index = order[position]
        duration = float(timing_segments[shot_index]["dur"])
        for candidate_score, item in options[shot_index]:
            if any(existing is not None and _conflicts(
                (item, duration),
                (existing, float(timing_segments[other]["dur"])),
                transition_handle_seconds,
            ) for other, existing in enumerate(current)):
                continue
            current[shot_index] = item
            visit(position + 1, filled + 1, score + candidate_score)
            if best_count == len(order) and best_score == suffix_scores[0]:
                # Complete coverage at the unconstrained score upper bound is
                # globally optimal; do not enumerate equivalent assignments.
                return
        current[shot_index] = None
        visit(position + 1, filled, score)

    visit(0, 0, 0)
    return finish(best)


def _selected_windows(best: list[dict | None], timing_segments: list[dict]
                      ) -> list[dict | None]:
    return [
        None if item is None else {
            **item,
            "source_end": (
                item["source_start"] + float(timing_segments[index]["dur"])
                if item["media_kind"] == "video" else None
            ),
        }
        for index, item in enumerate(best)
    ]


def _related_park_windows(candidates, selected, timing_segments, handle):
    """Keep quality-checked park ranges outside the exact layer's occupied spans."""
    result = []
    for item in candidates:
        quality = item.get("quality_evidence", {})
        if (item.get("source_mode") != "primary"
                or item.get("usage_status") in {"不建议使用", "待核验"}
                or quality.get("status") != "measured" or quality.get("blocked")):
            continue
        same = [(i, other) for i, other in enumerate(selected)
                if other is not None and other["identity_key"] == item["identity_key"]]
        if item["media_kind"] == "image":
            if not same:
                result.append(item)
            continue
        if item.get("source_start") is None or item.get("source_end") is None:
            continue
        if any(other["media_kind"] == "image" for _, other in same):
            continue
        pieces = [(item["source_start"], item["source_end"])]
        for i, other in same:
            occupied_start = other["source_start"]
            occupied_end = occupied_start + float(timing_segments[i]["dur"]) + handle
            remaining = []
            for start, end in pieces:
                if end <= occupied_start or start >= occupied_end:
                    remaining.append((start, end))
                else:
                    if start < occupied_start:
                        remaining.append((start, occupied_start))
                    if end > occupied_end:
                        remaining.append((occupied_end, end))
            pieces = remaining
        result.extend({**item, "source_start": start, "source_end": end}
                      for start, end in pieces)
    return result


def _fill_related_park_windows(plan, timing, candidates, aspect_ratio, selected, handle, motion_context=None):
    missing = [i for i, item in enumerate(selected) if item is None]
    if not missing:
        return
    from .motion_ranking import subsequence_context
    related = match_ad_shots(
        [plan[i] for i in missing], [timing[i] for i in missing],
        _related_park_windows(candidates, selected, timing, handle), aspect_ratio,
        transition_handle_seconds=handle, _score_candidate=_secondary_content_score,
        _reserved=[(item, float(timing[i]['dur'])) for i, item in enumerate(selected)
                   if item is not None],
        _motion_context=subsequence_context(selected, timing, missing, motion_context))
    for index, item in zip(missing, related, strict=True):
        if item is None:
            continue
        evidence = observed_text_match(str(plan[index].get("query") or ""),
            usage_status=item.get("usage_status"),
            **{key: item.get(key) or "" for key in ("title", "description", "tags")})
        selected[index] = {**item, "match_evidence": {
            **evidence, "tier": "park_secondary", "matchLevel": "observed_related",
            "requestedQuery": str(plan[index].get("query") or ""),
            "reason": evidence["reason"] + "；本园区相关镜头次级填充，全部请求词有文字依据但主体未确认；"
                      "仍需人工核对画面主体与旁白语义，不能作为旁白所述具体设施或事实的证明",
            "basis": item.get("title") or item.get("description")}}


def fill_park_gaps(plan_segments, timing_segments, candidates, aspect_ratio, matches,
                   *, transition_handle_seconds=TRANSITION_HANDLE_SECONDS, motion_context=None):
    """Fill unmatched slots from observed primary-park sources, never replace matches.

    The caller supplies current task-scoped candidates. Primary scope is the
    declared park source; generic libraries cannot establish park membership.
    """
    if not len(plan_segments) == len(timing_segments) == len(matches):
        raise ValueError("AD_MATCH_TIMING_INVALID: 分镜与配音时间表数量不一致")
    selected = list(matches)
    # Reserve complete textual support globally before neutral park fallback can
    # consume it for an unrelated (or ambiguous partial-term) request.
    _fill_related_park_windows(plan_segments, timing_segments, candidates,
                               aspect_ratio, selected, transition_handle_seconds, motion_context)
    order=sorted(range(len(plan_segments)),key=lambda i:(-float(timing_segments[i]['dur']),i))
    for index in order:
        planned,timed=plan_segments[index],timing_segments[index]
        if selected[index] is not None:
            continue
        duration = float(timed['dur'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("AD_MATCH_TIMING_INVALID: 配音时间无效")
        from .motion_ranking import fixed_reservations, retained_reservations
        reserved = ([(item,float(timing_segments[i]['dur'])) for i,item in enumerate(selected)
                     if item is not None] + fixed_reservations(motion_context)
                    + retained_reservations(selected, timing_segments, motion_context, except_index=index))
        used = {item['identity_key'] for item,_duration in reserved}
        options = []
        for item in candidates:
            if item.get('source_mode') != 'primary' or item.get('usage_status') == '不建议使用':
                continue
            if not (item.get('title') or item.get('description')):
                continue
            if item['media_kind'] != planned['type'] or item['direction'] != aspect_ratio:
                continue
            quality = item.get('quality_evidence', {})
            if quality.get('status') != 'measured' or quality.get('blocked'):
                continue
            if item['media_kind'] == 'video' and (item.get('source_start') is None or
                    item.get('source_end') is None or
                    item['source_end'] - item['source_start'] + 1e-6 < duration + transition_handle_seconds):
                continue
            # Subtract already-used source ranges, including their transition handles.
            occupied=[]
            same=[(o,d) for o,d in reserved if o['identity_key']==item['identity_key']]
            if item['media_kind']=='image':
                if not same and not any(_conflicts((item, duration),
                        (other, other_duration), transition_handle_seconds)
                        for other, other_duration in reserved):
                    options.append(item)
                continue
            if any(o['media_kind']=='image' for o,_ in same):continue
            for other,other_duration in same:
                occupied.append((other['source_start'],other['source_start']+other_duration+transition_handle_seconds))
            starts=[item['source_start'],*[end for _,end in occupied if item['source_start']<end<item['source_end']]]
            for start in sorted(set(starts)):
                end=start+duration+transition_handle_seconds
                if end>item['source_end']+1e-6 or any(min(end,b)>max(start,a)+1e-6 for a,b in occupied):continue
                proposed = {**item, 'source_start': start}
                if any(_conflicts((proposed, duration),
                        (other, other_duration), transition_handle_seconds)
                        for other, other_duration in reserved):
                    continue
                options.append(proposed)
                break
        from .motion_ranking import preference_for_slot
        def merit(item):
            return (item['identity_key'] in used,
                    item.get('quality_evidence',{}).get('penalty',0),
                    -_secondary_content_score(str(planned.get('query') or ''), item))
        options.sort(key=lambda item: (merit(item), item['asset_id'],item.get('source_start') or 0))
        if options:
            baseline=options[0]
            peers=[item for item in options if merit(item)==merit(baseline)
                   and item.get('usage_status')==baseline.get('usage_status')]
            item=min(peers,key=lambda item:(
                -preference_for_slot(item,index,selected,timing_segments,context=motion_context),
                item['asset_id'],item.get('source_start') or 0))
            selected[index]={**item,'match_evidence':{
                'tier':'park_secondary','matchedTerms':[], 'requestedQuery':str(planned.get('query') or ''),
                'reason':'缺少精准镜头，使用本园区'+('未使用素材' if item['identity_key'] not in used else '已用素材的未重复区间')+
                         '次级填充；此画面不能作为旁白所述具体设施或事实的证明',
                'basis':item.get('title') or item.get('description')}}
    return _selected_windows(selected,timing_segments)


def match_task_ad_shots(db, task_id, plan_segments, timing_segments, candidates, aspect_ratio, *, motion_context=None):
    """Production entry: exact observed selection, then safe park-only fallback."""
    from .visual_batch_preview import _privacy_pending
    safe=[item for item in candidates if not _privacy_pending(db,item['asset_id'],
        round((item.get('source_start') or 0)*1e6),round((item.get('source_end') or 0)*1e6))]
    exact=match_ad_shots(plan_segments,timing_segments,safe,aspect_ratio,_motion_context=motion_context)
    return fill_park_gaps(plan_segments,timing_segments,safe,aspect_ratio,exact,motion_context=motion_context)
