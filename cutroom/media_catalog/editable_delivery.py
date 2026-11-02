"""Build truthful, manifest-driven editable delivery components."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from .snapshots import file_content_signature


_COMPONENT_NAMES = {
    "base_video": "video-base",
    "subtitles": "subtitles",
    "voice_or_original_audio": "audio-voice",
    "bgm_stem": "audio-bgm",
    "sfx_stem": "audio-sfx",
}


def _contained(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        if path == root or root in path.parents:
            return True
    return False


def _signature_matches(path: Path, entry: dict) -> bool:
    expected = entry.get("sha256")
    if not isinstance(expected, str) or not path.is_file():
        return False
    return file_content_signature(path)["sha256"] == expected


def _frozen_entries(candidate: dict) -> list[dict]:
    dependency = candidate.get("dependency_snapshot") or {}
    entries = [dict(item) for item in dependency.get("files", []) if isinstance(item, dict)]
    for key, kind in (("ass", "ass"), ("subtitle_evidence", "subtitle_evidence")):
        item = dependency.get(key)
        if isinstance(item, dict) and item.get("path"):
            entries.append({"kind": kind, **item})
    for item in dependency.get("assets", []):
        if isinstance(item, dict) and item.get("path"):
            entries.append({"kind": f"asset-role:{item.get('role', '')}", **item})
    for collection in (
        (dependency.get("packaging") or {}).get("items", []),
        (dependency.get("packaging_compiled") or {}).get("items", []),
    ):
        for item in collection:
            if not isinstance(item, dict) or not item.get("file"):
                continue
            path = Path(item["file"]).resolve()
            match = next(
                (entry for entry in entries if entry.get("path") and Path(entry["path"]).resolve() == path),
                None,
            )
            if match is not None:
                entries.append({**match, "kind": f"packaging:{item.get('slot', '')}"})
    return entries


def _entry_component(entry: dict) -> str | None:
    kind = str(entry.get("kind") or "").lower()
    name = Path(str(entry.get("path") or "")).name.lower()
    if kind in {"base_video", "original_video"} or kind == "asset-role:digital_human":
        return "base_video"
    if kind in {"ass", "subtitle_evidence"} or "subtitle:ass" in kind or name.endswith((".ass", ".srt")):
        return "subtitles"
    if kind in {"voice", "narration", "original_audio"} or name.startswith(("voice.", "narration.")):
        return "voice_or_original_audio"
    if kind == "bgm" or kind.endswith(":bgm") or name.startswith(("bgm.", "music.")):
        return "bgm_stem"
    if kind == "sfx" or kind.endswith(":sfx") or name.startswith(("sfx.", "effects.")):
        return "sfx_stem"
    return None


def _copy_component(staging: Path, component: str, source: Path) -> dict:
    suffix = source.suffix.lower() or ".bin"
    target = staging / f"{_COMPONENT_NAMES[component]}{suffix}"
    shutil.copyfile(source, target)
    return {
        "status": "included",
        "path": target.name,
        "source_path": str(source),
        "signature": file_content_signature(target),
    }


def build_delivery_manifest(
    staging: str | Path,
    candidate: dict,
    trusted_roots: list[str | Path],
    component_overrides: dict | None = None,
) -> dict:
    """Copy available frozen components and declare unavailable ones explicitly."""
    destination = Path(staging).resolve()
    roots = [Path(root).resolve() for root in trusted_roots]
    overrides = component_overrides or {}
    unknown = set(overrides) - set(_COMPONENT_NAMES)
    if unknown:
        raise ValueError(f"EDITABLE_COMPONENT_UNKNOWN: {sorted(unknown)}")

    frozen = _frozen_entries(candidate)
    available: dict[str, Path] = {}
    for entry in frozen:
        component = _entry_component(entry)
        if component is None or component in available or not entry.get("path"):
            continue
        path = Path(entry["path"]).resolve()
        if _signature_matches(path, entry):
            available[component] = path

    for component, raw_path in overrides.items():
        path = Path(raw_path).resolve()
        if not path.is_file() or not _contained(path, roots):
            raise ValueError(
                f"EDITABLE_COMPONENT_UNTRUSTED: {component} 不在可信目录：{path}"
            )
        available[component] = path

    components = {
        "final_video": {
            "status": "included",
            "path": "video-final.mp4",
            "signature": file_content_signature(destination / "video-final.mp4"),
        }
    }
    for component in _COMPONENT_NAMES:
        source = available.get(component)
        if source is None:
            reason = (
                "原始/配音音频未作为独立冻结文件提供，声音保留在成片中"
                if component == "voice_or_original_audio"
                else "批准候选的冻结依赖中没有该组件"
            )
            components[component] = {"status": "not_available", "reason": reason}
        else:
            components[component] = _copy_component(destination, component, source)

    scene_cards = (candidate.get("dependency_snapshot") or {}).get("scene_cards")
    if isinstance(scene_cards, list) and scene_cards:
        timecodes = destination / "material-timecodes.json"
        timecodes.write_text(
            json.dumps({"scene_cards": scene_cards}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        components["material_timecodes"] = {
            "status": "included",
            "path": timecodes.name,
            "signature": file_content_signature(timecodes),
        }
    else:
        components["material_timecodes"] = {
            "status": "not_available",
            "reason": "批准候选的冻结依赖中没有镜头时间码",
        }

    manifest = {
        "schema": "cutroom.editable-delivery/v1",
        "candidate_id": candidate.get("candidate_id"),
        "candidate_label": candidate.get("version_label"),
        "route": (candidate.get("dependency_snapshot") or {}).get("route") or "digital_human",
        "components": components,
    }
    (destination / "delivery-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
