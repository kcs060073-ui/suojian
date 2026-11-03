"""Build truthful, manifest-driven editable delivery components."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from .snapshots import file_content_signature
from .provenance import canonical_digest


EDITABLE_EXPORT_VERSION = "audio-sources-v2"


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
        return "bgm_source"
    if kind == "sfx" or kind.endswith(":sfx") or name.startswith(("sfx.", "effects.")):
        return "sfx_source"
    return None


def _copy_component(staging: Path, component: str, source: Path, copy_verified=None) -> dict:
    suffix = source.suffix.lower() or ".bin"
    target = staging / f"{_COMPONENT_NAMES[component]}{suffix}"
    if copy_verified is None:
        shutil.copyfile(source, target)
    else:
        copy_verified(source, target)
    return {
        "status": "included",
        "path": target.name,
        "source_path": str(source),
        "signature": file_content_signature(target),
    }



def _frozen_recipe(candidate: dict) -> dict | None:
    """Read the exact signed plan, never reconstruct from mutable catalog state."""
    dependency = candidate.get("dependency_snapshot") or {}
    signed_plan = dependency.get("plan_file")
    plans = [signed_plan] if signed_plan else [
        entry for entry in dependency.get("files", [])
        if str(entry.get("path", "")).lower().endswith(".json")
    ]
    for entry in plans:
        path = Path(entry["path"])
        if not _signature_matches(path, entry):
            raise ValueError(f"EDITABLE_AUDIO_PLAN_CHANGED: 冻结计划已变化：{path.name}")
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
            encoded = plan.get("renderRecipeJson") if isinstance(plan, dict) else None
            if encoded is None:
                continue
            recipe = json.loads(encoded)
            if (not isinstance(recipe, dict) or recipe.get("recipe_version") != 1
                    or canonical_digest(recipe) != plan.get("recipe_digest")
                    or dependency.get("recipe_digest", plan["recipe_digest"]) != plan["recipe_digest"]):
                raise ValueError("配方摘要不一致")
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise ValueError("EDITABLE_AUDIO_PLAN_INVALID: 冻结声音配方无效") from error
        return recipe
    return None


def _export_audio_sources(destination: Path, candidate: dict, frozen: list[dict], copy_verified) -> dict:
    recipe = _frozen_recipe(candidate)
    requested = {}
    if recipe is not None:
        main = recipe.get("main_audio") or {}
        if main.get("src"):
            requested[str(Path(main["src"]).resolve())] = {"main_audio"}
        for instance in recipe.get("packaging_instances", []):
            value = instance.get("render_value") or {}
            if instance.get("kind") in {"bgm", "sfx"} and value.get("src"):
                requested.setdefault(str(Path(value["src"]).resolve()), set()).add(instance["kind"])
        for shot in recipe.get("shots", []):
            value = shot.get("render_value") or {}
            if value.get("audio") in {"keep", "duck", "clean"} and value.get("src"):
                requested.setdefault(str(Path(value["src"]).resolve()), set()).add("ambient")
        signed = {str(Path(item["path"]).resolve()): item for item in recipe.get("asset_manifest", [])}
    else:
        # Legacy candidates lack a frozen render recipe: disclose incomplete timing.
        signed = {}
        for entry in frozen:
            role = _entry_component(entry)
            if role in {"bgm_source", "sfx_source", "voice_or_original_audio"} and entry.get("path"):
                path = str(Path(entry["path"]).resolve())
                requested.setdefault(path, set()).add(role)
                signed[path] = entry
    sources = []
    for index, (source_path, roles) in enumerate(sorted(requested.items())):
        source = Path(source_path)
        entry = signed.get(source_path)
        if entry is None or not _signature_matches(source, entry):
            raise ValueError(f"EDITABLE_AUDIO_SOURCE_CHANGED: 冻结声音素材缺失或变化：{source.name}")
        target = destination / f"audio-source-{index + 1:03d}{source.suffix.lower()}"
        if copy_verified is None:
            shutil.copyfile(source, target)
        else:
            copy_verified(source, target)
        if not _signature_matches(target, entry):
            raise ValueError("EDITABLE_AUDIO_SOURCE_CHANGED: 复制期间声音素材发生变化")
        sources.append({"source_path": source_path, "path": target.name,
                        "roles": sorted(roles), "signature": file_content_signature(target)})
    mapping = {
        "schema": "cutroom.audio-source-map/v1",
        "candidate_id": candidate.get("candidate_id"),
        "status": "complete" if recipe is not None else "timeline_unavailable",
        "semantics": "原素材和冻结配方，不是时间线分轨；导入时按 sources 将配方源路径映射到包内文件。",
        "sources": sources,
        "recipe": recipe,
        "mixing": {
            "voice": "按 main_audio 倍速和 audio_policy.voiceFilter 处理全长主音轨",
            "bgm": "全长循环；gain 缺省数字人 0.18、广告 0.12；duck 缺省开启，检测源为处理后的主音轨",
            "sfx": "按每个 render_value 的 start/end/sourceStart/gain/fadeIn/fadeOut；缺省 sourceStart=0、gain=0.35、fadeIn=0；有 end 时 fadeOut=min(0.08,end-start)，无 end 时为0",
            "ambient": "shots.render_value 中 keep/duck/clean 按源入点及输出区间；增益0.7；clean 加 highpass=80Hz/afftdn=12；主音轨侧链 threshold=0.015,ratio=8,attack=15,release=250",
            "sum": "全长先处理，再以 amix normalize=0 合并、应用 audio_policy.limiterFilter；成片参考 video-final.mp4",
        },
        "limitations": ["没有独立混音 stem，不保证其他剪辑器 DSP 与 FFmpeg 逐采样一致"]
            + ([] if recipe is not None else ["旧候选缺少冻结配方，无法还原完整声音时间线"]),
    }
    target = destination / "audio-source-map.json"
    target.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "included", "path": target.name, "signature": file_content_signature(target),
            "timeline_status": mapping["status"], "source_count": len(sources), "semantics": "original_sources"}


def build_delivery_manifest(
    staging: str | Path,
    candidate: dict,
    trusted_roots: list[str | Path],
    component_overrides: dict | None = None,
    copy_verified=None,
) -> dict:
    """Copy available frozen components and declare unavailable ones explicitly."""
    destination = Path(staging).resolve()
    roots = [Path(root).resolve() for root in trusted_roots]
    overrides = component_overrides or {}
    unknown = set(overrides) - set(_COMPONENT_NAMES)
    if unknown:
        raise ValueError(f"EDITABLE_COMPONENT_UNKNOWN: {sorted(unknown)}")

    if set(overrides) & {"bgm_stem", "sfx_stem"}:
        raise ValueError("EDITABLE_STEM_UNVERIFIED: 原素材不能作为已批准的时间线分轨")

    frozen = _frozen_entries(candidate)
    available: dict[str, Path] = {}
    for entry in frozen:
        component = _entry_component(entry)
        if component not in _COMPONENT_NAMES or component in available or not entry.get("path"):
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
            components[component] = _copy_component(destination, component, source, copy_verified)

    components["audio_sources"] = _export_audio_sources(destination, candidate, frozen, copy_verified)
    for component in ("bgm_stem", "sfx_stem"):
        components[component] = {
            "status": "not_available",
            "reason": "未保存候选绑定的时间线分轨；原素材及实例映射见 audio_sources，不等同混音 stem",
        }

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
        "export_version": EDITABLE_EXPORT_VERSION,
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
