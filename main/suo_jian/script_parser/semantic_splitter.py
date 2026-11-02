from __future__ import annotations

import re


SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;\n])")
CLAUSE_BREAK = re.compile(r"(?<=[，,、：:])")
SOFT_BREAK = re.compile(r"(?=并且|同时|其中|另外|此外|适合|支持|可享|周边|项目|园区|选择|还是|能上|想要|点击|预约|一层|单层|全一楼|层高|承重|可租|免租|最小|最大)")
PROTECTED_UNIT = re.compile(r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+)(?:到(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+))?(?:万平方米|平方米|平米|㎡|元(?:/㎡)?|公里|分钟|个月|米|吨|亩|万|%)")


def visible_chars(text: str) -> int:
    return len(re.sub(r"[\s，。！？、；：,.!?;:]", "", text))


def _split_long(text: str, max_chars: int) -> list[str]:
    # A name introducing a list is a complete cue, even when the full phrase fits.
    # Exclude clock-like numeric colons and URL schemes.
    intro = re.search(r"(?<!\d)[：:](?![/\\])", text)
    if intro:
        head, body = text[:intro.end()], text[intro.end():]
        return [head] + [part for clause in CLAUSE_BREAK.split(body) if clause.strip()
                         for part in _split_long(clause.strip(), max_chars)]
    if visible_chars(text) <= max_chars:
        return [text]
    clauses = [x.strip() for x in CLAUSE_BREAK.split(text) if x.strip()]
    if len(clauses) == 1:
        clauses = [x.strip() for x in SOFT_BREAK.split(text) if x.strip()]
    if len(clauses) == 1:
        return [text]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        pieces = _split_long(clause, max_chars) if visible_chars(clause) > max_chars and clause != text else [clause]
        for piece in pieces:
            proposed = f"{current}{piece}"
            if current and visible_chars(proposed) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = proposed
    if current:
        chunks.append(current)
    return chunks


def semantic_split(text: str, target_chars: int = 12, max_chars: int = 20) -> list[str]:
    """Split Chinese promo copy without breaking numeric/unit expressions."""
    normalized = re.sub(r"[\t\r ]+", "", text or "").strip()
    sentences = [x.strip() for x in SENTENCE_BREAK.split(normalized) if x.strip()]
    chunks: list[str] = []
    for sentence in sentences:
        chunks.extend(_split_long(sentence, max_chars))

    merged: list[str] = []
    for chunk in chunks:
        if merged and not re.search(r"[：:、]$", merged[-1]) and not re.search(r"[：:、]$", chunk) and visible_chars(chunk) < max(4, target_chars // 2):
            candidate = f"{merged[-1]}{chunk}"
            if visible_chars(candidate) <= max_chars:
                merged[-1] = candidate
                continue
        merged.append(chunk)
    return merged


def fit_block_count(blocks: list[str], target_count: int, max_chars: int = 20) -> list[str]:
    """Best-effort strict-mode count fitting without rewriting the user's wording."""
    result = list(blocks)
    while len(result) > target_count and len(result) > 1:
        index = min(range(len(result) - 1), key=lambda i: visible_chars(result[i]) + visible_chars(result[i + 1]))
        result[index:index + 2] = [f"{result[index]}{result[index + 1]}"]
    while len(result) < target_count:
        candidates = [(visible_chars(value), index) for index, value in enumerate(result)]
        if not candidates:
            break
        _, index = max(candidates)
        parts = _split_long(result[index], max(6, max_chars // 2))
        if len(parts) < 2:
            value = result[index]
            midpoint = max(1, len(value) // 2)
            for match in PROTECTED_UNIT.finditer(value):
                if match.start() < midpoint < match.end():
                    midpoint = match.start() if match.start() > 0 else match.end()
            if midpoint <= 0 or midpoint >= len(value):
                break
            parts = [value[:midpoint], value[midpoint:]]
        result[index:index + 1] = parts[:2]
    return result
