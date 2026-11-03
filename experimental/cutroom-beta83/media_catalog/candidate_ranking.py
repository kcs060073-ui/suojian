"""Explain current observed-text support; this local fallback is not semantic AI."""

from functools import lru_cache
import re

METHOD_VERSION = "observed-text-v3"
_QUERY_SPLIT = re.compile(r"[\s,，。；;、/|]+")
_CLAUSE_SPLIT = re.compile(r"[，,。；;！？!?\n\r]+")
# Explicit denials/unknown claims only. Bare 无/非 would misclassify 无柱厂房.
_UNSUPPORTED = re.compile(r"未观察到|未发现|未见|没有(?:看到|发现|观察到|拍到)?|不包含|不含|不是|并非|不属于|"
    r"不能(?:证实|证明|认定|确认)|无法(?:证实|证明|认定|确认|判断)")
_UNCERTAIN = re.compile(r"未核实|未确认|待核验|待确认|待核对|需要(?:后续)?核对")
_CORRECTION = re.compile(r"^(?:而是|实际是|实际为|确认为|已确认|已核实|核实为)")
_ABSENT_QUALIFIER = re.compile(r"^(?:遮挡|遮蔽|水印|字幕)的")


@lru_cache(maxsize=4096)
def _clauses(text):
    result=[]
    for raw in _CLAUSE_SPLIT.split(text):
        clause="".join(raw.casefold().split())
        spans=[]
        for cue in _UNSUPPORTED.finditer(clause):
            end=len(clause)
            # Absence qualifiers such as 没有遮挡的厂房 deny 遮挡, not 厂房.
            if cue.group().startswith(("没有","未见","未发现","未观察到","不含")):
                qualifier=_ABSENT_QUALIFIER.match(clause[cue.end():])
                if qualifier is not None:end=cue.end()+qualifier.end()-1
            spans.append((cue.end(),end))
        if _UNCERTAIN.search(clause):
            spans.append((0,len(clause)))  # Pending verification may follow the subject.
        if clause:result.append((clause,tuple(spans)))
    return tuple(result)


def observed_text_match(query: str, *, title="", description="", tags="", usage_status=None) -> dict:
    fields=[_clauses(str(value or "")) for value in (title,description,tags)]
    terms=list(dict.fromkeys("".join(term.casefold().split()) for term in _QUERY_SPLIT.split(query)
                              if len("".join(term.split()))>=2))
    matched=[];excluded=[];conflicted=[];support=[]
    for term in terms:
        states=[]
        for field_name,clauses in zip(("title","description","tags"),fields):
            state=None;last_clause=None
            for clause,spans in clauses:
                occurrences=list(re.finditer(re.escape(term),clause))
                if not occurrences:continue
                denied=all(any(start<=m.start()<end for start,end in spans) for m in occurrences)
                # A mere later mention does not override a denial; require a correction.
                if denied or state!="denied" or _CORRECTION.match(clause):
                    state="denied" if denied else "supported";last_clause=clause
            if state:states.append((state,field_name,last_clause))
        denied=[state for state in states if state[0]=="denied"]
        positive=[state for state in states if state[0]=="supported"]
        if denied:
            excluded.append(term)
            if positive:conflicted.append(term)
        elif positive:
            matched.append(term)
            support.extend({"term":term,"field":field,"text":text} for _,field,text in positive)
    limitations=list(dict.fromkeys(clause for clauses in fields for clause,spans in clauses if spans))
    reason=("当前观察文字命中："+"、".join(matched)) if matched else "未找到当前观察文字的对应依据，请预览判断"
    if excluded:reason+="；否定或未核实，未计相关分："+"、".join(excluded)
    if conflicted:reason+="；观察字段相互矛盾，需人工核对"
    if limitations:reason+="；局限："+"；".join(limitations[:2])
    if usage_status in ("待核验", "不建议使用"):
        excluded=list(dict.fromkeys([*excluded,*matched]))
        matched=[];support=[]
        reason=f"人工标记为{usage_status}，未计相关分；请先核对当前片段"
    return {"method":METHOD_VERSION,"score":sum(100+len(term) for term in matched),
            "matchedTerms":matched,"excludedTerms":excluded,"conflictedTerms":conflicted,
            "support":support,"limitations":limitations,"reason":reason}
