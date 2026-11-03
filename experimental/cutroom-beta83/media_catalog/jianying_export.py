"""Jianying delivery preflight; never relabel component packages as native projects.

Resolved paths are an export-only map. Catalog mounts and approved recipes are
not changed here. The caller supplies its task-scoped trusted roots explicitly.
"""
from pathlib import Path
import re

from .jianying_compatibility import inspect_jianying_compatibility
from .provenance import canonical_digest
from .snapshots import file_content_signature


def _fail(code, detail):
    raise ValueError(f"JY_{code}: {detail}")


def _time(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _window(item, start_key, end_key, limit=None):
    start, end = item.get(start_key), item.get(end_key)
    if not _time(start) or not _time(end) or start >= end or (limit is not None and end > limit):
        _fail("TIMELINE_INVALID", f"{start_key}/{end_key}")


def _references(recipe):
    if recipe.get("recipe_version") != 1:
        _fail("RECIPE_VERSION_UNSUPPORTED", "expected v1")
    duration = recipe.get("duration_us")
    if not _time(duration) or duration == 0:
        _fail("TIMELINE_INVALID", "duration_us")
    shots = recipe.get("shots")
    if not isinstance(shots, list) or not shots:
        _fail("TIMELINE_INVALID", "shots")
    references = []
    cursor = 0
    for index, shot in enumerate(shots):
        _window(shot, "output_start_us", "output_end_us", duration)
        if shot["output_start_us"] != cursor:
            _fail("TIMELINE_INVALID", "gap/overlap")
        cursor = shot["output_end_us"]
        if shot.get("media_kind") not in {"video", "presenter", "image"}:
            _fail("TIMELINE_INVALID", "media_kind")
        if shot["media_kind"] == "image":
            if "source_start_us" in shot or "source_end_us" in shot:
                _fail("TIMELINE_INVALID", "image source window")
        else:
            _window(shot, "source_start_us", "source_end_us")
            if shot["source_end_us"] - shot["source_start_us"] < shot["output_end_us"] - shot["output_start_us"]:
                _fail("TIMELINE_INVALID", "source window too short")
        references.append((f"shot:{index}", shot.get("src")))
        if (shot.get("render_value") or {}).get("src"):
            references.append((f"shot-render:{index}", shot["render_value"]["src"]))
    if cursor != duration:
        _fail("TIMELINE_INVALID", "coverage")
    audio = recipe.get("main_audio") or {}
    _window(audio, "source_start_us", "source_end_us")
    _window(audio, "output_start_us", "output_end_us", duration)
    if audio["output_start_us"] != 0 or audio["output_end_us"] != duration:
        _fail("TIMELINE_INVALID", "main audio coverage")
    numerator = audio.get("playback_rate_num", 1)
    denominator = audio.get("playback_rate_den", 1)
    if not _time(numerator) or not _time(denominator) or numerator == 0 or denominator == 0:
        _fail("TIMELINE_INVALID", "main audio playback rate")
    if (audio["source_end_us"] - audio["source_start_us"]) * denominator < duration * numerator:
        _fail("TIMELINE_INVALID", "main audio source too short")
    references.append(("main_audio", audio.get("src")))
    for cue in recipe.get("subtitle_cues", []):
        _window(cue, "start_us", "end_us", duration)
        if not isinstance(cue.get("text"), str) or not cue["text"].strip():
            _fail("TIMELINE_INVALID", "empty subtitle")
    ass_path = (recipe.get("subtitle_style") or {}).get("ass_path")
    if ass_path:
        references.append(("subtitle_ass", ass_path))
    for index, item in enumerate(recipe.get("packaging_instances", [])):
        if "start_us" in item or "end_us" in item:
            _window(item, "start_us", "end_us", duration)
        source = (item.get("render_value") or {}).get("src")
        if source:
            references.append((f"packaging:{index}", source))
    return references


def _path(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        _fail("MANIFEST_INVALID", "absolute local path required")
    return str(Path(value).resolve())


def _entries(entries):
    if not isinstance(entries, list) or not entries:
        _fail("MANIFEST_INVALID", "nonempty frozen manifest required")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("MANIFEST_INVALID", "entry")
        path = _path(entry.get("path"))
        sha = entry.get("sha256")
        size = entry.get("size_bytes")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) or not _time(size):
            _fail("MANIFEST_INVALID", "full sha256 and size_bytes required")
        identity = {"sha256": sha, "size_bytes": size}
        if path in result and result[path] != identity:
            _fail("MANIFEST_MISMATCH", path)
        result[path] = identity
    return result


def verify_jianying_assets(recipe, asset_manifest, trusted_roots, *, relinks=None):
    """Verify frozen identities and explicit relocations; no writes or DB access.

    This checks timeline arithmetic and file identity, not codec decodability or
    actual media duration. Real source-window probing belongs to native preflight.
    """
    references = _references(recipe)
    frozen = _entries(recipe.get("asset_manifest"))
    supplied = _entries(asset_manifest)
    if frozen != supplied:
        _fail("MANIFEST_MISMATCH", "caller manifest differs from frozen recipe")
    roots = [Path(_path(str(root))) for root in trusted_roots]
    if not roots:
        _fail("ASSET_UNTRUSTED", "no task-scoped roots")
    mapping = {_path(key): _path(value) for key, value in (relinks or {}).items()}
    if set(mapping) - set(frozen):
        _fail("RELINK_UNKNOWN", "mapping key is not a frozen source")
    roles = {path: [] for path in frozen}
    for role, raw in references:
        path = _path(raw)
        if path not in frozen:
            _fail("REFERENCE_UNBOUND", role)
        roles[path].append(role)
    assets = []
    for original, identity in frozen.items():
        resolved = Path(mapping.get(original, original)).resolve()
        if not any(resolved == root or root in resolved.parents for root in roots):
            _fail("ASSET_UNTRUSTED", str(resolved))
        if not resolved.is_file():
            _fail("ASSET_MISSING", str(resolved))
        actual = file_content_signature(resolved)
        if actual != identity:
            _fail("ASSET_CHANGED", str(resolved))
        assets.append({"source_path": original, "resolved_path": str(resolved),
                       **identity, "roles": sorted(roles[original]),
                       "relinked": str(resolved) != original})
    return {"schema": "cutroom.jianying-assets/v1", "assets": assets,
            "recipe_digest": canonical_digest(recipe),
            "resolution_digest": canonical_digest(assets),
            "native_project_verified": False, "approval_transferred": False}


def export_jianying_project(recipe, asset_manifest, output_dir, target_version,
                            accepted_degradations):
    """Fail closed without creating output until the native probe is accepted.

    Kept at the agreed public signature for the coordinator. No current caller
    can override compatibility using degradation consent or a forged manifest.
    """
    report = inspect_jianying_compatibility(recipe, target_version)
    _fail("TARGET_UNVERIFIED", report["blockers"][0]["message"])
