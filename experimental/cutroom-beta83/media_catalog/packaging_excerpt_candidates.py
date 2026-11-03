"""Small, explainable excerpt inventory bound to unmodified confirmed speech.

This is deliberately not a general semantic rewriting system. Questions,
conditions, negation and quantitative claims are omitted from automatic cards.
Manual text remains available through the existing editor.
"""
import re

VERSION = "refined-excerpts-v1"
RESTRICTED = re.compile(r"[?？\d%％]|不|未|无|没|非|仅|只|才|除|若|如|符合|条件|最高|最低|至少|至多|以内|以上|以下|以内|限于|可申请|须|必须|并非|怎么|如何|是否|能否|吗|想要|想了解|想把")
INTRO = re.compile(r"^(?:来看看|来看一下|了解一下|看看)")


def source_context(script, start, end):
    left = max([script.rfind(c, 0, start) for c in "。！？!?；;\n"] + [-1]) + 1
    right = end if script[end-1:end] in "。！？!?；;\n" else next((i + 1 for i in range(end, len(script)) if script[i] in "。！？!?；;\n"), len(script))
    return script[left:right]


def candidates(script, phrases):
    result = {}
    for ref, phrase in phrases.items():
        raw = phrase.get("raw_text") or phrase["text"]
        needle = raw.strip()
        start = script.find(needle)
        if start < 0:
            needle = phrase["text"]; start = script.find(needle)
        if start < 0 or script.count(needle) != 1:
            continue
        context = source_context(script, start, start + len(needle))
        if RESTRICTED.search(context):
            continue
        value = INTRO.sub("", needle).strip().rstrip("，,。；;：:\n").strip()
        if not 2 <= len(value) <= 24 or value not in needle:
            continue
        # No internal deletion or paraphrase. Context and exact offsets travel
        # with the candidate, and timing remains the measured whole sentence.
        offset = start + needle.index(value)
        result.setdefault(ref, []).append({"text": value, "source_ref": ref,
            "script_start": offset, "script_end": offset + len(value),
            "source_text": needle, "context": context,
            "start": phrase["start"], "end": phrase["end"],
            "layout": content_layout(value)})
    return result


def content_layout(text):
    layout = {"version": "refined-v1", "text": text, "body": text}
    # Explicit grammatical separators, never a fixed last-N-character split.
    title = re.fullmatch(r"([^的，。！？]{2,8})的(现成|标准|独立|多层)?([^，。！？]{2,12})", text)
    if title:
        layout.update(label=title[1], kicker=title[2] or "", body=title[3])
    else:
        fact = re.fullmatch(r"(装修|空间|交通|设备|厂房|配套)(也能|可以|能够)(.+)", text)
        if fact:
            layout.update(label=fact[1], body=fact[2] + fact[3])
    return layout


def budget(choices):
    """Preserved human elements consume the budget; they are never deleted."""
    preserved = choices.get("instances", [])
    if not choices.get("requested_roles"):
        preserved = [i for i in preserved if i.get("origin") == "manual"]
    return {"opening": max(0, 1 - sum(i["role"] == "title_card" for i in preserved)),
            "info": max(0, 2 - sum(i["role"] == "info_card" for i in preserved))}
