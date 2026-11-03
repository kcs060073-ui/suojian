"""Join explicit visible declarations to frame references; this does not verify pixels.

No persistence, ranking change, inference, frame reads, or semantic acceptance.
The adapter must supply current server-derived bindings/observations/frame hashes.
"""
import copy
import hashlib
import re

from .candidate_ranking import observed_text_match

METHOD_VERSION = "visible-claim-links-v1"
# Small local declaration vocabulary, independent of the provider's 7-field schema.
VISIBLE_LABELS = {
    "visible_subject": {"industrial_interior": "厂房内部", "office_interior": "办公楼",
                        "road": "道路", "green_space": "绿地", "person": "人物", "unknown": "未知"},
    "visible_color": {"red": "红色", "green": "绿色", "blue": "蓝色", "other": "其他颜色", "unknown": "未知"},
    "visible_setting": {"indoor": "室内", "outdoor": "室外", "unknown": "未知"},
}
_BINDING_KEYS = {"assetId", "assetFingerprint", "sourceStartUs", "sourceEndUs",
                 "observationKey", "origin", "methodVersion"}
_SHA = re.compile(r"[0-9a-f]{64}")


def _binding_valid(binding):
    return (isinstance(binding, dict) and set(binding)==_BINDING_KEYS
        and all(isinstance(binding[k], str) and 0<len(binding[k])<=256
                for k in ("assetId", "assetFingerprint", "observationKey", "methodVersion"))
        and binding["origin"] in ("human", "model")
        and type(binding["sourceStartUs"]) is int and type(binding["sourceEndUs"]) is int
        and 0<=binding["sourceStartUs"]<=binding["sourceEndUs"])


def _frame_valid(frame):
    return (isinstance(frame, dict) and set(frame)=={"atUs", "sha256"}
        and type(frame["atUs"]) is int and frame["atUs"]>=0
        and isinstance(frame["sha256"], str) and _SHA.fullmatch(frame["sha256"]) is not None)


def explain_visible_claim_links(query, *, current_binding, observation, claims_package, frames):
    """Explain existing literal matches using separately declared per-claim frames.

    'linked' proves consistent declarations only. Human/model origins are preserved;
    pixel correctness, park identity and document meaning remain unverified.
    A manual mark with no frame evidence remains unlinked; no historical fallback.
    """
    if not isinstance(query, str) or not isinstance(observation, dict):
        raise ValueError("CLAIM_LINK_INPUT_INVALID")
    text = observed_text_match(query, title=observation.get("title"),
        description=observation.get("description"), tags=observation.get("tags"),
        usage_status=observation.get("usageStatus"))
    result = {"method": METHOD_VERSION, "semanticVerified": False,
        "evidenceMeaning": "declared-reference-consistency",
        "querySha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "binding": copy.deepcopy(current_binding) if _binding_valid(current_binding) else None,
        "evidenceStatus": "unlinked", "claimSource": None,
        "textMatch": text, "linkedSupport": [], "unlinkedTerms": list(text["matchedTerms"]),
        "issues": []}

    def finish(status, issue):
        result["evidenceStatus"] = status
        result["issues"].append(issue)
        return result

    if not _binding_valid(current_binding) or not isinstance(claims_package, dict):
        return finish("invalid", "CLAIM_BINDING_INVALID")
    if not _binding_valid(claims_package.get("binding")):
        return finish("invalid", "CLAIM_BINDING_INVALID")
    if claims_package["binding"] != current_binding:
        return finish("stale", "CLAIM_BINDING_STALE")
    source = claims_package.get("source")
    allowed_sources = ("human_annotation",) if current_binding["origin"]=="human" else ("model_fixture", "model_output")
    if source not in allowed_sources:
        return finish("invalid", "CLAIM_ORIGIN_INVALID")
    result["claimSource"] = source
    if observation.get("status") != "observed":
        return finish("unlinked", "OBSERVATION_NOT_CURRENT")
    evidence_us = observation.get("evidenceUs")
    if (not isinstance(evidence_us, list) or len(evidence_us)>128
            or any(type(at) is not int for at in evidence_us)):
        return finish("invalid", "OBSERVATION_FRAMES_INVALID")
    if (not isinstance(frames, list) or len(frames)>128
            or any(not _frame_valid(frame) for frame in frames)
            or len({f["atUs"] for f in frames}) != len(frames)):
        return finish("invalid", "FRAME_MANIFEST_INVALID")
    claims = claims_package.get("claims")
    if not isinstance(claims, list) or len(claims)>32:
        return finish("invalid", "CLAIMS_INVALID")
    ids = set()
    for claim in claims:
        if not isinstance(claim, dict): return finish("invalid", "CLAIMS_INVALID")
        claim_id, kind = claim.get("claimId"), claim.get("kind")
        label, certainty = claim.get("label"), claim.get("certainty")
        refs = claim.get("frames")
        if (not isinstance(claim_id, str) or not 0<len(claim_id)<=128 or claim_id in ids
                or not isinstance(kind, str) or kind not in VISIBLE_LABELS
                or not isinstance(label, str) or label not in VISIBLE_LABELS[kind]
                or claim.get("text") != VISIBLE_LABELS[kind][label]
                or certainty not in ("visible", "uncertain", "denied")
                or (label=="unknown" and certainty=="visible")
                or not isinstance(refs, list) or len(refs)>128
                or any(not _frame_valid(ref) for ref in refs)
                or len({ref["atUs"] for ref in refs}) != len(refs)):
            return finish("invalid", "CLAIMS_INVALID")
        ids.add(claim_id)
    declared_text = "；".join(("未确认" if c["certainty"]=="uncertain" else "不是" if c["certainty"]=="denied" else "")
                              +c["text"] for c in claims)
    declarations = observed_text_match(query, description=declared_text)
    manifest = {f["atUs"]: f["sha256"] for f in frames}
    allowed_times = set(evidence_us)
    for claim in claims:
        if claim["certainty"] != "visible": continue
        refs = claim["frames"]
        if not refs:
            result["issues"].append("CLAIM_FRAME_MISSING"); continue
        if any(not current_binding["sourceStartUs"]<=ref["atUs"]<=current_binding["sourceEndUs"] for ref in refs):
            result["issues"].append("CLAIM_FRAME_OUTSIDE_WINDOW"); continue
        if any(ref["atUs"] not in allowed_times for ref in refs):
            result["issues"].append("CLAIM_FRAME_NOT_IN_OBSERVATION"); continue
        if any(manifest.get(ref["atUs"]) != ref["sha256"] for ref in refs):
            result["issues"].append("CLAIM_FRAME_NOT_IN_MANIFEST"); continue
        claim_match = observed_text_match(query, description=claim["text"])
        for term in text["matchedTerms"]:
            if term not in declarations["matchedTerms"] or term not in claim_match["matchedTerms"]: continue
            result["linkedSupport"].append({"term": term, "claimId": claim["claimId"],
                "kind": claim["kind"], "label": claim["label"], "text": claim["text"],
                "certainty": claim["certainty"],
                "frames": copy.deepcopy(refs)})
    linked_terms = {support["term"] for support in result["linkedSupport"]}
    result["unlinkedTerms"] = [term for term in text["matchedTerms"] if term not in linked_terms]
    if linked_terms:
        result["evidenceStatus"] = "partial" if result["unlinkedTerms"] else "linked"
    result["issues"] = list(dict.fromkeys(result["issues"]))
    return result
