"""Verified original-text anchors on actual ASR word intervals."""
import json
import math
import re
import subprocess
import unicodedata
from pathlib import Path


def _canonical(text, mapping):
    text = "".join(mapping.get(c, c) for c in unicodedata.normalize("NFKC", text)).lower()
    text = "".join(c for c in text if not c.isspace())
    return "".join(c for i, c in enumerate(text)
                   if c not in ",.。!?;:、\"'「」『』“”‘’（）()"
                   or (c in ":,." and i > 0 and i+1 < len(text)
                       and text[i-1].isdigit() and text[i+1].isdigit()))


def _mapping(project_root, text):
    chars = sorted(set(unicodedata.normalize("NFKC", text)))
    try:
        proc = subprocess.run(["node", str(Path(project_root)/"scripts/simplify-speech.cjs")],
            input=json.dumps(chars), capture_output=True, text=True, encoding="utf-8",
            timeout=10, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}  # Exact text remains usable; unavailable conversion is not guessed.



def _tokens(words, mapping):
    raw = [(v, index) for index, word in enumerate(words)
           for c in unicodedata.normalize("NFKC", word["w"])
           for v in mapping.get(c, c).lower() if not v.isspace()]
    return [(c, owner) for i, (c, owner) in enumerate(raw)
            if c not in ",.。!?;:、\"'「」『』“”‘’（）()"
            or (c in ":,." and i > 0 and i+1 < len(raw)
                and raw[i-1][0].isdigit() and raw[i+1][0].isdigit())]

def _checked(words):
    result = []
    previous = 0.
    for word in words:
        a, b = word["start"], word["end"]
        if (not isinstance(word.get("w"), str) or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (a, b))
            or a < previous or b < a):
            raise ValueError("SPEECH_WORD_TIMING_INVALID")
        previous = b
        result.append(dict(word))
    return result


def reviewed_words(project_root, script, words, review):
    """Apply only observed, unique corrections inside their confirmed interval.

    Returned words are derived data; original ASR files and original script stay
    unchanged. A replacement keeps whole source-word bounds, never invented times.
    """
    result = _checked(words)
    if not review or review.get("status") != "reviewed" or not review.get("digest"):
        return result
    mapping = _mapping(project_root, script + "".join(w["w"] for w in result))
    norm = lambda value: _canonical(value, mapping)
    for finding in review.get("findings", []):
        expected, observed = norm(finding.get("script_excerpt", "")), norm(finding.get("recognized_excerpt", ""))
        a, b = finding.get("audio_start"), finding.get("audio_end")
        if (not expected or not observed or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (a, b))
            or a < 0 or b <= a):
            continue
        indices = [i for i, w in enumerate(result) if w["start"] >= a-.001 and w["end"] <= b+.001]
        if not indices:
            continue
        lo, hi = indices[0], indices[-1]+1
        tokens = _tokens(result[lo:hi], mapping)
        window = "".join(c for c, _ in tokens)
        if norm(script).count(expected) == 1 and window.count(observed) == 1:
            offset = window.index(observed)
            owners = [i for _, i in tokens]
            first, last = owners[offset], owners[offset+len(observed)-1]
            begin = owners.index(first)
            finish = len(owners)-owners[::-1].index(last)
            changed = window[begin:offset] + expected + window[offset+len(observed):finish]
        elif re.fullmatch(r"[0-9.%/]+", expected) and re.fullmatch(r"[0-9.%/]+", observed):
            # Review findings use reconciliation's numeric notation. Resolve
            # back to original characters before replacing measured words.
            from .transcript_reconciliation import _normalize_with_positions
            normalized, bounds = _normalize_with_positions(script, [(i,i+1) for i in range(len(script))])
            raw = "".join(w["w"] for w in result[lo:hi])
            heard, heard_bounds = _normalize_with_positions(raw, [(i,i+1) for i in range(len(raw))])
            if normalized.count(expected) != 1 or heard.count(observed) != 1:
                continue
            x, y = normalized.index(expected), heard.index(observed)
            replacement = script[bounds[x][0]:bounds[x+len(expected)-1][1]]
            start, end = heard_bounds[y][0], heard_bounds[y+len(observed)-1][1]
            owners = [i for i,w in enumerate(result[lo:hi]) for _ in w["w"]]
            first, last = owners[start], owners[end-1]
            begin = owners.index(first)
            finish = len(owners)-owners[::-1].index(last)
            changed = raw[begin:start] + replacement + raw[end:finish]
        else:
            continue
        left, right = lo+first, lo+last+1
        result[left:right] = [{"w": changed, "start": float(result[left]["start"]),
                              "end": float(result[right-1]["end"])}]
    return result


def _anchor(script, words, anchor, mapping):
    norm = lambda value: _canonical(value, mapping)
    target, confirmed = norm(anchor), norm(script)
    if not target or confirmed.count(target) != 1:
        raise ValueError("SPEECH_ANCHOR_AMBIGUOUS")
    tokens = _tokens(words, mapping)
    observed = "".join(c for c, _ in tokens)
    if observed.count(target) != 1:
        raise ValueError("SPEECH_ANCHOR_UNOBSERVED")
    owners = [owner for _, owner in tokens]
    lo, hi = observed.index(target), observed.index(target)+len(target)
    if ((target[0].isdigit() and lo and observed[lo-1] in "0123456789.,:/") or
        (target[-1].isdigit() and hi < len(observed) and observed[hi] in "0123456789.,:/%")):
        raise ValueError("SPEECH_ANCHOR_PARTIAL_NUMBER")
    if (lo and owners[lo-1] == owners[lo]) or (hi < len(owners) and owners[hi-1] == owners[hi]):
        raise ValueError("SPEECH_ANCHOR_PARTIAL_WORD")
    a, b = words[owners[lo]]["start"], words[owners[hi-1]]["end"]
    if b <= a:
        raise ValueError("SPEECH_ANCHOR_EMPTY")
    return float(a), float(b)


def verified_anchor(project_root, script, words, anchor, review=None):
    fixed = reviewed_words(project_root, script, words, review)
    mapping = _mapping(project_root, script + anchor + "".join(w["w"] for w in fixed))
    return _anchor(script, fixed, anchor, mapping)


def aligned_phrases(project_root, script, words, review):
    try:
        fixed = reviewed_words(project_root, script, words, review)
        mapping = _mapping(project_root, script + "".join(w["w"] for w in fixed))
    except (ValueError, TypeError, KeyError):
        return []
    result = []
    for text in [p.strip() for p in re.split(r"[。！？；\n]+", script) if p.strip()]:
        if len(text) > 40:
            continue
        try:
            a, b = _anchor(script, fixed, text, mapping)
        except (ValueError, TypeError, KeyError):
            continue
        result.append({"text":text,"start":a/1.25,"end":b/1.25,
            "basis":"normalized original sentence and actual ASR word bounds; only window-bound reviewed corrections",
            "review_digest":review.get("digest") if review.get("status")=="reviewed" else None})
    return result
