"""Durable packaging-preview artifacts and their trusted media identity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import uuid

from .path_utils import filesystem_path
from .provenance import canonical_digest
from .packaging_plan import (
    canonical_packaging_gate_payload,
    confirm_plan,
    validate_plan_for_render,
)
from . import packaging_plan
from .packaging_plan import active_instances, active_items
from .packaging_layers import PackagingTimingContext, compile_packaging_layers
from .provenance import latest_gate
from .render_output_profile import PreviewOutputProfile, packaging_preview_output
from .snapshots import file_content_signature
from .timeline_contract import seconds_to_us
from .tools import default_ffprobe_search_roots, find_ffmpeg, find_ffprobe


PREVIEW_CACHE_RELATIVE_ROOT = Path(
    "runtime/workbench/cache/packaging-previews"
)
PACKAGING_PREVIEW_RENDERER_VERSION = "packaging-preview-v1"


@dataclass(frozen=True)
class PreviewIdentity:
    task_id: str
    plan_id: str
    picture_lock_digest: str
    plan_digest: str
    renderer_version: str
    preview_profile: str = "portrait-540p-v1"
    input_digest: str = ""


@dataclass(frozen=True)
class PackagingPreviewInput:
    identity: PreviewIdentity
    duration_seconds: float
    task_type: str
    plan: dict
    coverage_targets: tuple[str, ...]
    timeline_map: tuple[dict, ...] = ()


def select_preview_windows(duration_seconds: float, instances: list[dict], *, include_music: bool = False) -> tuple[dict, ...]:
    """Select short real-media windows that include every element and the ending."""
    duration_ms = round(float(duration_seconds) * 1000)
    if duration_ms <= 0:
        raise ValueError("PACKAGING_PREVIEW_TIMELINE_EMPTY: 预览时间轴必须大于零")
    if duration_ms <= 5000:
        return ({"fullStart": 0.0, "fullEnd": duration_ms / 1000,
                 "previewStart": 0.0, "previewEnd": duration_ms / 1000},)
    ranges = [(0, min(2500, duration_ms)), (max(0, duration_ms - 2500), duration_ms)]
    if include_music:
        middle = max(0, (duration_ms - 8000) // 2)
        ranges.append((middle, min(duration_ms, middle + 8000)))
    for instance in instances:
        start = round(float(instance["start_seconds"]) * 1000)
        end = round(float(instance["end_seconds"]) * 1000)
        if not (0 <= start < end <= duration_ms):
            raise ValueError("PACKAGING_PREVIEW_INSTANCE_RANGE: 包装元素超出全片时间轴")
        exit_end = min(duration_ms, end + 350)
        entrance_end = min(exit_end, start + 2650)
        ranges.append((max(0, start - 350), entrance_end))
        if exit_end > entrance_end:
            ranges.append((max(start, end - 2300), exit_end))
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 150:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    mapped = []
    cursor = 0
    for start, end in merged:
        length = end - start
        mapped.append({
            "fullStart": start / 1000, "fullEnd": end / 1000,
            "previewStart": cursor / 1000, "previewEnd": (cursor + length) / 1000,
        })
        cursor += length
    return tuple(mapped)


def render_preview_montage(
    source: Path, output: Path, timeline_map: tuple[dict, ...], runner=None,
) -> Path:
    """Cut audio and video from one fully compiled render with the same time map."""
    if not timeline_map:
        raise ValueError("PACKAGING_PREVIEW_MAP_EMPTY: 短预览缺少时间映射")
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or not filesystem_path(source).is_file() or filesystem_path(output).exists():
        raise RuntimeError("PACKAGING_PREVIEW_MONTAGE_INPUT: 预览输入或输出路径无效")
    ffmpeg = find_ffmpeg(os.environ, search_roots=default_ffprobe_search_roots())
    filters = []
    streams = []
    for index, window in enumerate(timeline_map):
        start, end = window["fullStart"], window["fullEnd"]
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"fps=30,format=yuv420p[v{index}]"
        )
        filters.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=async=1:first_pts=0[a{index}]"
        )
        streams.append(f"[v{index}][a{index}]")
    filters.append("".join(streams) + f"concat=n={len(timeline_map)}:v=1:a=1[v][a]")
    partial = output.with_name(f"{output.stem}.part-{uuid.uuid4().hex}{output.suffix}")
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-n", "-i", str(source),
        "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k", "-movflags", "+faststart", str(partial),
    ]
    from .workbench_actions import _default_process_runner, _run_checked

    try:
        _run_checked(
            runner or _default_process_runner, command,
            cwd=source.parent, timeout_seconds=1800,
            max_output_bytes=1024 * 1024,
        )
        if not filesystem_path(partial).is_file() or filesystem_path(partial).stat().st_size <= 0:
            raise RuntimeError("PACKAGING_PREVIEW_MONTAGE_FAILED: 无法生成含画面和声音的短预览")
        filesystem_path(partial).replace(filesystem_path(output))
        return output
    finally:
        filesystem_path(partial).unlink(missing_ok=True)


def join_preview_windows(clips: list[Path], output: Path, runner=None, *, timeline_map=None) -> Path:
    """Place decoded windows on one frame clock and concatenate exact audio spans."""
    clips = [Path(clip).resolve() for clip in clips]
    output = Path(output).resolve()
    if not clips or filesystem_path(output).exists() or any(not filesystem_path(clip).is_file() or clip == output for clip in clips):
        raise RuntimeError("PACKAGING_PREVIEW_MONTAGE_INPUT: 短片输入或输出路径无效")
    from .workbench_actions import _default_process_runner, _run_checked
    process_runner = runner or _default_process_runner
    if timeline_map is None:
        # Compatibility callers have no receipt; use the encoded audio duration,
        # not video frame rounding or AAC decoder padding, as their clock.
        probe = find_ffprobe(os.environ, search_roots=default_ffprobe_search_roots())
        spans = []
        cursor = 0.0
        for clip in clips:
            result = _run_checked(process_runner, [str(probe), "-v", "error",
                "-select_streams", "a:0", "-show_entries", "stream=duration",
                "-of", "json", str(clip)], cwd=output.parent,
                timeout_seconds=30, max_output_bytes=65536)
            try:
                duration = float(json.loads(result["stdout"])["streams"][0]["duration"])
            except (ValueError, TypeError, KeyError, IndexError) as error:
                raise RuntimeError("PACKAGING_PREVIEW_MONTAGE_TIMING: 无法核对声音区间") from error
            spans.append({"previewStart": cursor, "previewEnd": cursor + duration})
            cursor += duration
    else:
        spans = list(timeline_map)
    if len(spans) != len(clips):
        raise RuntimeError("PACKAGING_PREVIEW_MONTAGE_TIMING: 片段数与时间映射不一致")
    cursor = 0.0
    for span in spans:
        try:
            start, end = float(span["previewStart"]), float(span["previewEnd"])
        except (ValueError, TypeError, KeyError) as error:
            raise RuntimeError("PACKAGING_PREVIEW_MONTAGE_TIMING: 时间映射无效") from error
        if (not math.isfinite(start) or not math.isfinite(end) or end <= start
                or abs(start - cursor) > .000001):
            raise RuntimeError("PACKAGING_PREVIEW_MONTAGE_TIMING: 时间映射不连续")
        cursor = end
    total = cursor
    ffmpeg = find_ffmpeg(os.environ, search_roots=default_ffprobe_search_roots())
    partial = output.with_name(f"{output.stem}.part-{uuid.uuid4().hex}{output.suffix}")
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-n"]
    for clip in clips:
        command.extend(("-i", str(clip)))
    # AV concat waits for the longest decoded stream in every segment, so an
    # extra video frame or AAC padding used to shift every later window.
    # A held first frame supplies a single output frame clock. Each actual
    # window is placed at its receipt offset, while audio joins sample spans.
    filters = [f"[0:v]trim=end_frame=1,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={total},trim=duration={total}[clock]"]
    last = "clock"
    for index, span in enumerate(spans):
        start, end = float(span["previewStart"]), float(span["previewEnd"])
        duration = end - start
        filters.extend((
            f"[{index}:v]trim=duration={duration},setpts=PTS-STARTPTS+{start}/TB[window{index}]",
            f"[{last}][window{index}]overlay=0:0:eof_action=repeat:enable='gte(t,{start})*lt(t,{end})'[video{index}]",
            f"[{index}:a]atrim=duration={duration},asetpts=PTS-STARTPTS,apad=whole_dur={duration},atrim=duration={duration}[audio{index}]",
        ))
        last = f"video{index}"
    filters.append("".join(f"[audio{i}]" for i in range(len(clips))) +
                   f"concat=n={len(clips)}:v=0:a=1[a]")
    command.extend((
        "-filter_complex", ";".join(filters),
        "-map", f"[{last}]", "-map", "[a]", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k", "-movflags", "+faststart",
        "-t", str(total), str(partial),
    ))
    try:
        _run_checked(process_runner, command, cwd=output.parent,
                     timeout_seconds=1800, max_output_bytes=1024 * 1024)
        if not filesystem_path(partial).is_file() or filesystem_path(partial).stat().st_size <= 0:
            raise RuntimeError("PACKAGING_PREVIEW_MONTAGE_FAILED: 无法合成短预览")
        filesystem_path(partial).replace(filesystem_path(output))
        return output
    finally:
        filesystem_path(partial).unlink(missing_ok=True)


def annotate_preview_coverage(
    coverage: list[dict], instances: list[dict], timeline_map: tuple[dict, ...],
) -> list[dict]:
    """Bind compiler coverage to visible/audible positions in the short file."""
    indexed = {item["instance_id"]: item for item in instances}
    result = []
    covered = set()
    for entry in coverage:
        item = indexed.get(entry.get("instanceId"))
        if item is None or entry.get("status") != "rendered":
            result.append(dict(entry))
            continue
        spans = []
        for window in timeline_map:
            start = max(float(item["start_seconds"]), window["fullStart"])
            end = min(float(item["end_seconds"]), window["fullEnd"])
            if end > start:
                spans.append({
                    "fullStart": round(start, 3), "fullEnd": round(end, 3),
                    "previewStart": round(window["previewStart"] + start - window["fullStart"], 3),
                    "previewEnd": round(window["previewStart"] + end - window["fullStart"], 3),
                })
        if spans:
            covered.add(item["instance_id"])
        result.append({**entry, "previewSpans": spans})
    if covered != set(indexed):
        raise RuntimeError("PACKAGING_PREVIEW_COVERAGE_MISSING: 短预览漏掉包装实例")
    return result


def preview_canonical_key(identity: PreviewIdentity) -> str:
    return canonical_digest(asdict(identity))


def _task_preview_context(db, task_id: str) -> tuple[str, float, PreviewOutputProfile]:
    row = db.connection.execute(
        """
        SELECT t.task_type, t.brief_json, MAX(sc.narrative_end) AS timeline_duration
        FROM tasks t
        LEFT JOIN scene_cards sc ON sc.task_id=t.task_id
        WHERE t.task_id=?
        GROUP BY t.task_id
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")
    task_type = row["task_type"]
    if task_type not in {"digital_human", "ad_mix"}:
        raise RuntimeError(f"UNSUPPORTED_ROUTE: 当前任务类型不支持包装预览：{task_type}")
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("BRIEF_INVALID: 任务文案不是有效 JSON") from error
    aspect_ratio = brief.get("aspect_ratio") if isinstance(brief, dict) else None
    profile = packaging_preview_output(task_type, aspect_ratio)
    return task_type, float(row["timeline_duration"] or 0.0), profile


def _signature_or_missing(path: Path) -> dict:
    resolved = Path(path).resolve()
    if not filesystem_path(resolved).is_file():
        return {"path": str(resolved), "missing": True}
    return file_content_signature(resolved)


def _renderer_source_signatures(project_root: Path, task_type: str) -> list[dict]:
    """Invalidate cached previews when code that produces their pixels changes."""
    common = {
        "packaging_preview": Path(__file__),
        "packaging_plan": Path(__file__).with_name("packaging_plan.py"),
        "packaging_layers": Path(__file__).with_name("packaging_layers.py"),
        "packaging_layout": Path(__file__).with_name("packaging_layout.py"),
        "packaging_music_review": Path(__file__).with_name("packaging_music_review.py"),
        "render_recipe_py": Path(__file__).with_name("render_recipe.py"),
        "render_output_profile": Path(__file__).with_name("render_output_profile.py"),
        "timeline_contract": Path(__file__).with_name("timeline_contract.py"),
        "subtitle_appearance": project_root / "src/subtitle-appearance.ts",
        "subtitle_appearance_compiled": project_root / "src/subtitle-appearance.js",
        "ass_text": project_root / "src/subtitle-ass.ts",
        "ass_text_alignment": project_root / "src/subtitle-alignment.ts",
        "mix_sfx": project_root / "src/mix-sfx.ts",
        "audio_mix_policy": project_root / "src/audio-mix-policy.ts",
        "audio_mix_policy_json": project_root / "src/audio-mix-policy-data.json",
        "audio_mix_policy_py": Path(__file__).with_name("audio_mix_policy.py"),
        "render_full_audio": project_root / "scripts/render-full-audio.ts",
        "types": project_root / "src/types.ts",
        "fancy_generator": project_root / "scripts/gen-fancy-text.py",
        "fancy_manifest": project_root / "assets/manifests/fancy-text-presets.json",
        "fancy_python": project_root / ".venv/Scripts/python.exe",
        "geometry": project_root / "scripts/media-display-geometry.ts",
        "tsconfig": project_root / "tsconfig.json",
    }
    route = (
        {
            "ad_actions": Path(__file__).with_name("ad_actions.py"),
            "rough_cut": Path(__file__).with_name("rough_cut.py"),
            "render_ad": project_root / "scripts/render-ad.ts",
            "ad_preview_window": project_root / "scripts/ad-preview-window.ts",
            "render_recipe_ts": project_root / "scripts/render-recipe.ts",
            "ad_style": project_root / "scripts/ad-style.ts",
            "load_env": project_root / "scripts/load-env.ts",
        }
        if task_type == "ad_mix" else {
            "speech_alignment": Path(__file__).with_name("speech_alignment.py"),
            "subtitle_compiler": Path(__file__).with_name("subtitles.py"),
            "subtitle_alignment_ts": project_root / "src/subtitle-alignment.ts",
            "speech_simplifier": project_root / "scripts/simplify-speech.cjs",

            "render_mix_adapter": Path(__file__).with_name("render_mix_adapter.py"),
            "digital_render_profile": Path(__file__).with_name("digital_render_profile.py"),
            "digital_render_recipe_py": Path(__file__).with_name("digital_render_recipe.py"),
            "render_mix": project_root / "scripts/render-mix.ts",
            "digital_render_recipe_ts": project_root / "scripts/digital-render-recipe.ts",
            "render_mix_options": project_root / "scripts/render-mix-options.ts",
        }
    )
    common.update({"refined_renderer": project_root / "scripts/refined_layers.py",
        "refined_bridge": project_root / "scripts/refined-layers.ts",
        "refined_domain": Path(__file__).with_name("packaging_refined.py"),
        "refined_font": project_root / "assets/fonts/NotoSansSC-VF.ttf",
        "visual_card_manifest": project_root / "assets/manifests/visual-cards.json",
        "subtitle_manifest": project_root / "assets/manifests/subtitle-presets.json"})
    # Installed releases execute compiled files. Track those actual bytes too.
    for kind, path in list({**common, **route}.items()):
        if path.suffix == ".ts": common[kind+"_js"] = path.with_suffix(".js")
        elif path.suffix == ".py":
            common[kind+"_pyc"] = path.with_suffix(".pyc")
            for cached in path.parent.glob("__pycache__/"+path.stem+".*.pyc"):
                common[kind+"_cache_"+cached.name] = cached
    return [
        {"kind": kind, **_signature_or_missing(path)}
        for kind, path in sorted({**common, **route}.items())
    ]


def _preview_input_digest(
    db,
    task_id: str,
    plan_id: str,
    task_type: str,
    duration: float,
    timeline_map: tuple[dict, ...] = (),
) -> str:
    row = db.connection.execute(
        "SELECT title,brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - guarded by _task_preview_context
        raise KeyError(f"任务不存在：{task_id}")
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("BRIEF_INVALID: 任务文案不是有效 JSON") from error
    keywords = brief.get("keywords") if isinstance(brief, dict) else None
    keyword = (
        keywords[0]
        if isinstance(keywords, list) and keywords and isinstance(keywords[0], str)
        else brief.get("keyword") if isinstance(brief, dict) else None
    ) or row["title"]
    project_root = packaging_plan.MANIFEST_DIR.resolve().parents[1]
    voice_path = (
        project_root / "runtime/workbench/tasks" / task_id / "artifacts/ad/voice.mp3"
    ).resolve()
    managed_voice=None
    if task_type=='ad_mix':
        from .tasks import get_task
        from .ad_voice_preview import current_ad_voice_source
        current_voice=current_ad_voice_source(db,project_root,get_task(db,task_id))
        if current_voice.source_kind=='managed':
            if current_voice.path is None:raise RuntimeError('VOICE_BUNDLE_UNAVAILABLE: 当前声音包不可读取')
            voice_path=current_voice.path
            head=db.connection.execute('SELECT revision,context_digest,audio_json FROM voice_edit_heads WHERE task_id=?',(task_id,)).fetchone()
            managed_voice=dict(head)
    compiled = compile_packaging_layers(
        db,
        plan_id,
        PackagingTimingContext(
            duration_seconds=duration,
            title=str(row["title"]),
            keyword=str(keyword),
            voice_mode=(
                f"cached:{voice_path}" if task_type == "ad_mix" else "original"
            ),
        ),
    )
    auxiliary: list[dict] = []
    if task_type == "digital_human":
        sources = _digital_subtitle_sources(db, task_id, project_root)
        auxiliary.extend(
            {"kind": key, **_signature_or_missing(Path(path))}
            for key, path in sorted(sources.items())
        )
    else:
        trusted_tasks_root = (project_root / "runtime/workbench/tasks").resolve()
        task_root = (trusted_tasks_root / task_id).resolve()
        if task_root != trusted_tasks_root and trusted_tasks_root not in task_root.parents:
            raise RuntimeError("INVALID_TASK_ID: 任务目录越界")
        script = (brief.get("final_script") or {}) if isinstance(brief, dict) else {}
        script_relative_path = (
            script.get("relative_path") if isinstance(script, dict) else None
        )
        if not isinstance(script_relative_path, str) or not script_relative_path.strip():
            raise RuntimeError("BRIEF_INVALID: 广告任务缺少锁定文案路径")
        script_path = (task_root / script_relative_path).resolve()
        if script_path != task_root and task_root not in script_path.parents:
            raise RuntimeError("LOCKED_SCRIPT_PATH_INVALID: 锁定广告文案路径越界")
        paths = {
            "script": script_path,
            "adplan": task_root / "artifacts/ad/adplan.json",
            "adtiming": (voice_path.parent/'timeline.json' if managed_voice else task_root / "artifacts/ad/adtiming.json"),
            "voice": voice_path,
            **({'voice_manifest':voice_path.parent/'receipt.json'} if managed_voice else {}),
        }
        auxiliary.extend(
            {"kind": kind, **_signature_or_missing(path)}
            for kind, path in sorted(paths.items())
        )
    return canonical_digest({
        "brief": brief,
        "compiled_packaging": compiled.snapshot,
        "plan_fields": compiled.plan_fields,
        **({'voice_version':managed_voice} if managed_voice else {}),
        "auxiliary_files": auxiliary,
        "renderer_sources": _renderer_source_signatures(project_root, task_type),
        **({"timeline_map": timeline_map} if timeline_map else {}),
    })


def build_preview_input(db, task_id: str, plan_id: str) -> PackagingPreviewInput:
    plan = validate_plan_for_render(db, plan_id)
    if plan["task_id"] != task_id:
        raise PermissionError("PACKAGING_PLAN_NOT_OWNED: 方案不属于当前任务")
    picture = latest_gate(db, task_id, "picture_lock")
    if picture is None:
        raise RuntimeError("PICTURE_LOCK_MISSING: 必须先完成画面锁定")
    task_type, timeline_duration, profile = _task_preview_context(db, task_id)
    instance_plan = int(plan.get("schema_version", 1)) >= 2
    # Preview the complete locked timeline on both routes. Sampling is retained
    # only as a reader for historical jobs, never for newly generated previews.
    duration = timeline_duration
    if duration <= 0:
        raise RuntimeError("PACKAGING_PREVIEW_TIMELINE_EMPTY: 当前任务没有可预览时长")
    enabled_instances = active_instances(plan) if instance_plan else []
    enabled_items = active_items(plan)
    timeline_map = ({"fullStart": 0.0, "fullEnd": duration,
                     "previewStart": 0.0, "previewEnd": duration},)
    renderer_version = f"{'ad' if task_type == 'ad_mix' else 'digital'}-packaging-preview-v6-recipe-continuous-ass-layout"
    identity = PreviewIdentity(
        task_id=task_id,
        plan_id=plan_id,
        picture_lock_digest=picture["gate_digest"],
        plan_digest=canonical_digest(canonical_packaging_gate_payload(plan)),
        renderer_version=renderer_version,
        preview_profile=profile.profile_id,
        input_digest=_preview_input_digest(
            db, task_id, plan_id, task_type, duration, timeline_map
        ),
    )
    slots = tuple(
        sorted(
            ({
                item["slot"]
                for item in enabled_items
                if not item["is_backup"]
            } | {instance["instance_id"] for instance in enabled_instances}
               if instance_plan else {
                item["slot"] for item in enabled_items if not item["is_backup"]
            })
        )
    )
    return PackagingPreviewInput(identity, duration, task_type, plan, slots, timeline_map)


def current_preview_matches(db, preview: dict) -> bool:
    try:
        current = build_preview_input(db, preview["task_id"], preview["plan_id"])
    except (KeyError, PermissionError, RuntimeError, ValueError):
        return False
    return preview_canonical_key(current.identity) == preview["canonical_key"]


def _validate_preview_receipt(
    receipt: dict, duration_seconds: float,
    timeline_map: list[dict] | tuple[dict, ...],
) -> None:
    if not isinstance(receipt, dict) or not re.fullmatch(
        r"[0-9a-f]{64}", str(receipt.get("recipe_digest", ""))
    ):
        raise RuntimeError("PACKAGING_PREVIEW_RECEIPT_INVALID: 配方摘要无效")
    intervals = receipt.get("covered_output_intervals")
    expected = (
        [{"start_us": seconds_to_us(item["fullStart"]),
          "end_us": seconds_to_us(item["fullEnd"])} for item in timeline_map]
        if timeline_map else [{"start_us": 0,
                               "end_us": seconds_to_us(duration_seconds)}]
    )
    if intervals != expected:
        raise RuntimeError("PACKAGING_PREVIEW_RECEIPT_INVALID: 成片覆盖区间不匹配")
    versions = receipt.get("asset_versions")
    if not isinstance(versions, list) or not versions:
        raise RuntimeError("PACKAGING_PREVIEW_RECEIPT_INVALID: 素材版本缺失")
    for item in versions:
        if (not isinstance(item, dict) or
                not isinstance(item.get("kind"), str) or not item["kind"] or
                not isinstance(item.get("path"), str) or
                not Path(item["path"]).is_absolute() or
                not isinstance(item.get("size_bytes"), int) or
                item["size_bytes"] < 0 or
                not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))):
            raise RuntimeError("PACKAGING_PREVIEW_RECEIPT_INVALID: 素材版本无效")


def _recipe_receipt_required(preview: dict) -> bool:
    version = preview.get("renderer_version", "")
    return (version.startswith(("ad-packaging-preview-v4-recipe",
                                "ad-packaging-preview-v5-recipe", "ad-packaging-preview-v6-recipe")) or
            version.startswith(("digital-packaging-preview-v4-recipe",
                                "digital-packaging-preview-v5-recipe", "digital-packaging-preview-v6-recipe")))


def _digital_content_receipt_valid(preview: dict) -> bool:
    if not preview.get("renderer_version", "").startswith((
        "digital-packaging-preview-v4-recipe",
        "digital-packaging-preview-v5-recipe",
        "digital-packaging-preview-v6-recipe",
    )):
        return True
    receipt = preview.get("preview_receipt") or {}
    return bool(re.fullmatch(
        r"[0-9a-f]{64}", str(receipt.get("recipe_content_digest", ""))))


def _receipt_assets_current(preview: dict) -> bool:
    if not _recipe_receipt_required(preview):
        return True
    receipt = preview.get("preview_receipt")
    if receipt is None or not _digital_content_receipt_valid(preview):
        return False
    try:
        _validate_preview_receipt(
            receipt, float(preview["duration_seconds"]), preview["timeline_map"])
        return all(
            filesystem_path(Path(item["path"])).is_file() and
            file_content_signature(item["path"]) == {
                "size_bytes": item["size_bytes"], "sha256": item["sha256"]
            }
            for item in receipt["asset_versions"]
        )
    except (KeyError, TypeError, ValueError, RuntimeError, OSError):
        return False


def assert_preview_confirmable(
    db,
    task_id: str,
    plan_id: str,
    preview_id: str | None,
    *,
    project_root: Path | None = None,
) -> dict:
    if not preview_id:
        raise RuntimeError(
            "PACKAGING_PREVIEW_REQUIRED: 确认包装方案前必须生成并观看当前预览"
        )
    preview = get_preview_artifact(db, preview_id)
    if (
        preview is None
        or preview["task_id"] != task_id
        or preview["plan_id"] != plan_id
    ):
        raise RuntimeError("PACKAGING_PREVIEW_REQUIRED: 当前方案没有对应预览")
    if preview["status"] == "missing":
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
    if preview["status"] == "stale" or not current_preview_matches(db, preview):
        raise RuntimeError("PACKAGING_PREVIEW_STALE: 画面或包装方案已变化")
    if preview["status"] != "ready":
        raise RuntimeError("PACKAGING_PREVIEW_REQUIRED: 当前预览尚未生成完成")
    from .packaging_watch import has_completed_preview_watch
    if not preview.get("viewed_at") or not has_completed_preview_watch(db, preview):
        raise RuntimeError("PACKAGING_PREVIEW_NOT_VIEWED: 请先完整观看包装预览")
    plan = packaging_plan.get_plan(db, plan_id)
    if plan is not None and int(plan.get("schema_version", 1)) >= 2:
        covered = {
            item.get("instanceId") for item in preview.get("coverage", [])
            if item.get("status") == "rendered" and item.get("instanceId")
        }
        missing = {item["instance_id"] for item in active_instances(plan)} - covered
        if missing:
            raise RuntimeError("PACKAGING_PREVIEW_COVERAGE_MISSING: 预览未覆盖所有包装元素")
        if preview.get("renderer_version", "").endswith("montage"):
            expected_map = build_preview_input(db, task_id, plan_id).timeline_map
            if not preview.get("timeline_map"):
                raise RuntimeError("PACKAGING_PREVIEW_MAP_MISSING: 短预览没有全片时间映射")
            if (preview["timeline_map"] != list(expected_map)
                    or abs(float(preview["duration_seconds"])
                           - expected_map[-1]["previewEnd"]) > .12):
                raise RuntimeError("PACKAGING_PREVIEW_MAP_CHANGED: 短预览时间映射已变化")
            if any(item.get("instanceId") in covered and not item.get("previewSpans")
                   for item in preview.get("coverage", [])):
                raise RuntimeError("PACKAGING_PREVIEW_COVERAGE_MISSING: 包装元素缺少短预览位置")
    if not preview.get("rel_path") or not preview.get("output_signature"):
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件证据不完整")
    if _recipe_receipt_required(preview):
        receipt = preview.get("preview_receipt")
        if receipt is None:
            raise RuntimeError("PACKAGING_PREVIEW_RECEIPT_MISSING: 预览缺少配方凭证")
        if not _digital_content_receipt_valid(preview):
            raise RuntimeError("PACKAGING_PREVIEW_RECEIPT_INVALID: 数字人内容摘要无效")
        _validate_preview_receipt(
            receipt, float(preview["duration_seconds"]), preview["timeline_map"])
        for asset in receipt["asset_versions"]:
            path = Path(asset["path"])
            if (not filesystem_path(path).is_file() or
                    file_content_signature(path) != {
                        "size_bytes": asset["size_bytes"], "sha256": asset["sha256"]
                    }):
                raise RuntimeError("PACKAGING_PREVIEW_ASSET_CHANGED: 预览素材版本已变化")
    if project_root is None:
        from .render_mix_adapter import PROJECT_ROOT

        project_root = PROJECT_ROOT
    media_path = _resolve_cache_path(Path(project_root), preview["rel_path"])
    if not filesystem_path(media_path).is_file():
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
    if file_content_signature(media_path) != preview["output_signature"]:
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_CHANGED: 预览文件内容已变化")
    return preview



def packaging_confirmation_digest(db, task_id: str, plan_id: str) -> str:
    """Bind the displayed packaging choice to its picture lock, without media work."""
    plan = packaging_plan.get_plan(db, plan_id)
    if plan is None or plan["task_id"] != task_id:
        raise PermissionError("PACKAGING_PLAN_NOT_OWNED: 方案不属于当前任务")
    return packaging_plan_confirmation_digest(db, plan)


def packaging_plan_confirmation_digest(db, plan: dict) -> str:
    """Project a token for the exact displayed plan, without rereading its settings.

    Submission still calls packaging_confirmation_digest under its write lock.
    A concurrent edit therefore invalidates the displayed token instead of
    silently authorizing settings the user has not seen.
    """
    picture = latest_gate(db, plan["task_id"], "picture_lock")
    return canonical_digest({
        "plan": canonical_packaging_gate_payload(plan),
        "picture_lock": picture["gate_digest"] if picture else None,
    })


def confirm_plan_for_generation(db, task_id: str, plan_id: str, *, expected_digest: str) -> dict:
    """Confirm production settings; preview viewing is optional, final review is not."""
    with db.transaction(immediate=True):
        digest = packaging_confirmation_digest(db, task_id, plan_id)
        if not expected_digest or digest != expected_digest:
            raise RuntimeError("PACKAGING_CONFIRMATION_STALE: 包装或画面已变化，请刷新后确认")
        validate_plan_for_render(db, plan_id)
        return confirm_plan(db, plan_id, evidence={
            "confirmation_mode": "final_review", "settings_digest": digest,
        })


def confirm_plan_after_preview(
    db,
    task_id: str,
    plan_id: str,
    preview_id: str | None,
    *,
    project_root: Path | None = None,
) -> dict:
    """Atomically bind viewed preview evidence to the packaging gate."""
    with db.transaction(immediate=True):
        preview = assert_preview_confirmable(
            db,
            task_id,
            plan_id,
            preview_id,
            project_root=project_root,
        )
        result = confirm_plan(
            db,
            plan_id,
            evidence={
                "preview_id": preview["preview_id"],
                "viewed_at": preview["viewed_at"],
                "canonical_key": preview["canonical_key"],
                **({"recipe_digest": preview["preview_receipt"]["recipe_digest"]}
                   if preview.get("preview_receipt") else {}),
                **({"recipe_content_digest": preview["preview_receipt"]["recipe_content_digest"]}
                   if preview.get("preview_receipt") and
                      "recipe_content_digest" in preview["preview_receipt"] else {}),
            },
        )
    return {**result, "preview_id": preview["preview_id"]}


def _preview_output_path(
    project_root: Path, task_id: str, preview_id: str
) -> Path:
    if any(part in task_id for part in ("/", "\\")) or task_id in {".", ".."}:
        raise ValueError("INVALID_TASK_ID: 任务编号包含非法路径字符")
    if any(part in preview_id for part in ("/", "\\")) or preview_id in {".", ".."}:
        raise ValueError("INVALID_PREVIEW_ID: 预览编号包含非法路径字符")
    return (
        Path(project_root).resolve()
        / PREVIEW_CACHE_RELATIVE_ROOT
        / task_id
        / f"{preview_id}.mp4"
    ).resolve()


def _clear_unbound_preview_outputs(project_root: Path, task_id: str, preview_id: str) -> None:
    """A queued/failed preview owns these two files; ready media is never retried."""
    output = _preview_output_path(project_root, task_id, preview_id)
    filesystem_path(output).unlink(missing_ok=True)
    work_dir = output.parent / f"{preview_id}-work"
    filesystem_path(work_dir / "full.mp4").unlink(missing_ok=True)
    if filesystem_path(work_dir).is_dir():
        for clip in filesystem_path(work_dir).glob("window-*.mp4"):
            filesystem_path(clip).unlink(missing_ok=True)


def _digital_subtitle_sources(db, task_id: str, project_root: Path) -> dict:
    row = db.connection.execute(
        "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("ASS_SOURCE_MISMATCH: 任务字幕来源无效") from error
    if not isinstance(brief, dict):
        raise RuntimeError("ASS_SOURCE_MISMATCH: 任务字幕来源无效")

    required = {"script_path", "words_path", "semantics_path"}
    if "subtitle_sources" in brief:
        sources = brief["subtitle_sources"]
        if not isinstance(sources, dict) or set(sources) != required:
            raise RuntimeError("ASS_SOURCE_MISMATCH: 包装预览锁定字幕来源不完整")
        try:
            return {key: str(Path(sources[key]).resolve()) for key in required}
        except (TypeError, ValueError) as error:
            raise RuntimeError("ASS_SOURCE_MISMATCH: 包装预览锁定字幕来源路径无效") from error

    trusted_tasks = (Path(project_root).resolve() / "runtime/workbench/tasks").resolve()
    task_root = (trusted_tasks / task_id).resolve()
    if task_root == trusted_tasks or trusted_tasks not in task_root.parents:
        raise RuntimeError("INVALID_TASK_ID: 任务目录越界")
    script_record = brief.get("final_script")
    relative_script = (
        script_record.get("relative_path")
        if isinstance(script_record, dict) else "inputs/final-script.txt"
    )
    if not isinstance(relative_script, str) or not relative_script.strip():
        raise RuntimeError("ASS_SOURCE_MISMATCH: 锁定文案路径无效")
    script_path = (task_root / relative_script).resolve()
    if script_path == task_root or task_root not in script_path.parents:
        raise RuntimeError("LOCKED_SCRIPT_PATH_INVALID: 锁定文案路径越界")
    storyboard_dir = task_root / "artifacts/storyboard"
    storyboard_brief = storyboard_dir / "brief.json"
    if storyboard_brief.is_file():
        from .workbench_actions import _subtitle_semantics, _validate_storyboard_brief
        _subtitle_semantics(
            storyboard_dir,
            _validate_storyboard_brief(storyboard_brief, script_path),
        )
    return {
        "script_path": str(script_path),
        "words_path": str((task_root / "artifacts/transcript.words.json").resolve()),
        "semantics_path": str((storyboard_dir / "semantics.json").resolve()),
    }


def render_digital_packaging_preview(
    db,
    current: PackagingPreviewInput,
    project_root: Path,
    preview_id: str,
    runner,
) -> dict:
    """Render a bounded digital-human preview through the production mix path."""
    from .render_mix_adapter import (
        build_packaging_preview_mix_job,
        run_render_mix,
    )
    from .render_recipe import preview_receipt
    from .digital_render_recipe import digital_recipe_content_digest
    from .workbench_actions import _as_subprocess_runner

    root = Path(project_root).resolve()
    output = _preview_output_path(
        root, current.identity.task_id, preview_id
    )
    work_dir = output.parent / f"{preview_id}-work"
    montage = bool(current.timeline_map and
                   current.timeline_map[-1]["previewEnd"] < current.duration_seconds - .01)
    render_output = work_dir / "window-0.mp4" if montage else output
    job = build_packaging_preview_mix_job(
        db,
        current.identity.task_id,
        current.identity.plan_id,
        work_dir,
        render_output,
        current.duration_seconds,
        subtitle_sources=_digital_subtitle_sources(
            db, current.identity.task_id, root
        ),
        subtitle_runner=_as_subprocess_runner(runner) if runner else None,
    )
    from .packaging_music_review import measure_digital_music
    stem_path = work_dir / f"audio-review-{uuid.uuid4().hex}.wav"
    job["audio_review_path"] = str(stem_path)
    rendered_clips: list[Path] = []
    try:
        if montage:
            for index, window in enumerate(current.timeline_map):
                window_job = {**job,
                    "output_path": str(work_dir / f"window-{index}.mp4"),
                    "preview_window": window}
                if index > 0:
                    window_job.pop("audio_review_path", None)
                rendered_clips.append(run_render_mix(
                    window_job, runner=_as_subprocess_runner(runner) if runner else None))
            rendered = join_preview_windows(rendered_clips, output, runner=runner,
                                            timeline_map=current.timeline_map)
        else:
            rendered = run_render_mix(
                job, runner=_as_subprocess_runner(runner) if runner else None)
        audio_advice = measure_digital_music(job, current.timeline_map)
    finally:
        filesystem_path(stem_path).unlink(missing_ok=True)
        for clip in rendered_clips:
            filesystem_path(clip).unlink(missing_ok=True)
    receipt = preview_receipt(job["render_recipe"], current.timeline_map)
    receipt["recipe_content_digest"] = digital_recipe_content_digest(
        job["render_recipe"])
    return {
        "output_path": rendered,
        "duration_seconds": (current.timeline_map[-1]["previewEnd"]
                             if current.timeline_map else current.duration_seconds),
        "coverage": list(job["packaging"].get("coverage") or []),
        "timeline_map": current.timeline_map,
        "preview_receipt": receipt,
        "audio_advice": audio_advice,
    }


def render_packaging_preview(
    db, preview_id: str, project_root: Path, runner=None
) -> dict:
    """Render one immutable preview identity without changing workflow gates."""
    preview = get_preview_artifact(db, preview_id)
    if preview is None:
        raise KeyError(f"找不到包装预览：{preview_id}")
    current = build_preview_input(db, preview["task_id"], preview["plan_id"])
    if preview_canonical_key(current.identity) != preview["canonical_key"]:
        return set_preview_status(
            db,
            preview_id,
            "stale",
            error_code="PACKAGING_PREVIEW_STALE",
        )
    # Generic job resume requeues the job, not its preview artifact. Re-enter
    # through queued after validating the same input identity, just as a new
    # preview request does. This never records viewing or confirms a gate.
    if preview["status"] in {"failed", "missing", "stale"}:
        set_preview_status(db, preview_id, "queued")
    set_preview_status(db, preview_id, "rendering")
    try:
        _clear_unbound_preview_outputs(
            Path(project_root), preview["task_id"], preview_id)
        if current.task_type == "digital_human":
            result = render_digital_packaging_preview(
                db, current, Path(project_root), preview_id, runner
            )
        elif current.task_type == "ad_mix":
            from .ad_actions import render_ad_packaging_preview
            from .rough_cut import validate_ad_source_windows

            try:
                validate_ad_source_windows(db.connection, preview["task_id"])
            except ValueError as error:
                raise RuntimeError(f"AD_SOURCE_WINDOW_INVALID: {error}") from error
            result = render_ad_packaging_preview(
                db, current, Path(project_root), preview_id, runner
            )
        else:  # pragma: no cover - build_preview_input already rejects it
            raise RuntimeError(
                f"UNSUPPORTED_ROUTE: 当前任务类型不支持包装预览：{current.task_type}"
            )
        if _recipe_receipt_required(preview) and not result.get("preview_receipt"):
            raise RuntimeError("PACKAGING_PREVIEW_RECEIPT_MISSING: 预览缺少配方凭证")
        if not _digital_content_receipt_valid({
            **preview, "preview_receipt": result.get("preview_receipt")
        }):
            raise RuntimeError("PACKAGING_PREVIEW_RECEIPT_INVALID: 数字人内容摘要无效")
        refreshed = build_preview_input(
            db, preview["task_id"], preview["plan_id"]
        )
        if preview_canonical_key(refreshed.identity) != preview["canonical_key"]:
            return set_preview_status(
                db,
                preview_id,
                "stale",
                error_code="PACKAGING_PREVIEW_STALE",
            )
        output = Path(result["output_path"]).resolve()
        relative = output.relative_to(Path(project_root).resolve()).as_posix()
        timeline_map = list(result.get("timeline_map") or [])
        if list(current.timeline_map) != timeline_map:
            raise RuntimeError("PACKAGING_PREVIEW_MAP_CHANGED: 渲染时间映射与计划不一致")
        coverage = list(result.get("coverage") or [])
        if current.timeline_map:
            coverage = annotate_preview_coverage(
                coverage, active_instances(current.plan), current.timeline_map)
        with db.transaction() as connection:
            connection.execute("UPDATE packaging_preview_artifacts SET audio_advice_json=? WHERE preview_id=?",
                (json.dumps(result.get("audio_advice") or {"status": "unknown", "reason": "route_not_measured"}, ensure_ascii=False, allow_nan=False), preview_id))
        ready = finalize_preview_artifact(
            db,
            preview_id,
            project_root=Path(project_root),
            rel_path=relative,
            duration_seconds=float(result["duration_seconds"]),
            coverage=coverage,
            timeline_map=timeline_map,
            preview_receipt=result.get("preview_receipt"),
        )
        return {
            **ready,
            "route": current.task_type,
            "profile": current.identity.preview_profile,
        }
    except Exception as error:
        latest = get_preview_artifact(db, preview_id)
        if latest is not None and latest["status"] == "rendering":
            code = str(error).partition(":")[0] or "PACKAGING_PREVIEW_FAILED"
            set_preview_status(
                db,
                preview_id,
                "failed",
                error_code=code,
                error_detail=str(error).partition(":")[0],
            )
        raise


def _decode_artifact(row) -> dict | None:
    if row is None:
        return None
    value = dict(row)
    advice = value.pop("audio_advice_json", None)
    value["audio_advice"] = json.loads(advice) if advice else None
    value["coverage"] = json.loads(value.pop("coverage_json") or "[]")
    value["timeline_map"] = json.loads(value.pop("timeline_map_json") or "[]")
    receipt = value.pop("preview_receipt_json")
    value["preview_receipt"] = json.loads(receipt) if receipt else None
    signature = value.pop("output_signature_json")
    value["output_signature"] = json.loads(signature) if signature else None
    return value


def get_preview_artifact(db, preview_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM packaging_preview_artifacts WHERE preview_id=?",
        (preview_id,),
    ).fetchone()
    return _decode_artifact(row)


def find_reusable_preview(db, identity: PreviewIdentity) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM packaging_preview_artifacts WHERE canonical_key=?",
        (preview_canonical_key(identity),),
    ).fetchone()
    return _decode_artifact(row)


def current_preview_for_plan(db, task_id: str, plan_id: str) -> dict | None:
    """Return the latest preview, projecting superseded input as stale."""
    current = build_preview_input(db, task_id, plan_id)
    row = db.connection.execute(
        "SELECT * FROM packaging_preview_artifacts "
        "WHERE task_id=? AND plan_id=? ORDER BY rowid DESC LIMIT 1",
        (task_id, plan_id),
    ).fetchone()
    preview = _decode_artifact(row)
    if preview is None:
        return None
    if (preview["canonical_key"] != preview_canonical_key(current.identity)
            or (preview["status"] == "ready" and not _receipt_assets_current(preview))):
        return {**preview, "status": "stale", "viewed_at": None}
    return preview


def create_preview_artifact(db, identity: PreviewIdentity) -> dict:
    key = preview_canonical_key(identity)
    with db.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM packaging_preview_artifacts WHERE canonical_key=?",
            (key,),
        ).fetchone()
        if existing is None:
            preview_id = f"pv-{uuid.uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO packaging_preview_artifacts(
                  preview_id,task_id,plan_id,canonical_key,picture_lock_digest,
                  plan_digest,renderer_version,preview_profile,status
                ) VALUES(?,?,?,?,?,?,?,?, 'queued')
                """,
                (
                    preview_id,
                    identity.task_id,
                    identity.plan_id,
                    key,
                    identity.picture_lock_digest,
                    identity.plan_digest,
                    identity.renderer_version,
                    identity.preview_profile,
                ),
            )
    result = find_reusable_preview(db, identity)
    if result is None:  # pragma: no cover - guarded by the insert/select above
        raise RuntimeError("PACKAGING_PREVIEW_CREATE_FAILED: 无法创建包装预览记录")
    return result


def _active_job_for_preview(db, preview_id: str) -> dict | None:
    from .workbench_worker import get_job

    rows = db.connection.execute(
        """
        SELECT j.job_id,p.payload_json
        FROM jobs j
        JOIN job_payloads p ON p.job_id=j.job_id
        WHERE j.job_type='packaging_preview'
          AND j.status IN ('queued','running')
        ORDER BY j.created_at,j.job_id
        """
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("preview_id") == preview_id:
            return get_job(db, row["job_id"])
    return None


def _next_preview_job_id(db, preview_id: str) -> str:
    base = f"packaging-preview-{preview_id}"
    existing = {
        row["job_id"]
        for row in db.connection.execute(
            "SELECT job_id FROM jobs WHERE job_id=? OR job_id LIKE ?",
            (base, f"{base}-r%"),
        ).fetchall()
    }
    if base not in existing:
        return base
    for retry in range(1, 100):
        candidate = f"{base}-r{retry}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("PACKAGING_PREVIEW_RETRY_LIMIT: 预览重试次数过多")


def _preview_request_projection(artifact: dict, job: dict | None) -> dict:
    return {
        "previewId": artifact["preview_id"],
        "planId": artifact["plan_id"],
        "status": artifact["status"],
        "profile": artifact["preview_profile"],
        "jobId": None if job is None else job["job_id"],
        "job": job,
    }


def request_preview(db, task_id: str, plan_id: str) -> dict:
    """Idempotently bind one canonical preview artifact to one active job."""
    from .workbench_worker import enqueue_job, get_job

    value = build_preview_input(db, task_id, plan_id)
    artifact = create_preview_artifact(db, value.identity)
    if (artifact["status"] == "ready" and current_preview_matches(db, artifact)
            and _receipt_assets_current(artifact)):
        return _preview_request_projection(artifact, None)
    if artifact["status"] == "ready":
        artifact = set_preview_status(
            db, artifact["preview_id"], "stale",
            error_code="PACKAGING_PREVIEW_STALE",
        )
    active = _active_job_for_preview(db, artifact["preview_id"])
    if active is not None:
        return _preview_request_projection(artifact, active)
    if artifact["status"] == "rendering":
        artifact = set_preview_status(
            db,
            artifact["preview_id"],
            "failed",
            error_code="PACKAGING_PREVIEW_JOB_ORPHANED",
        )
    if artifact["status"] in {"failed", "stale", "missing"}:
        artifact = set_preview_status(db, artifact["preview_id"], "queued")
    job_id = _next_preview_job_id(db, artifact["preview_id"])
    try:
        enqueue_job(
            db,
            job_id=job_id,
            job_type="packaging_preview",
            task_id=task_id,
            payload={"preview_id": artifact["preview_id"]},
            retry_policy="explicit",
        )
    except ValueError:
        active = _active_job_for_preview(db, artifact["preview_id"])
        if active is None:
            raise
        return _preview_request_projection(artifact, active)
    return _preview_request_projection(artifact, get_job(db, job_id))


_ALLOWED_TRANSITIONS = {
    "queued": {"rendering", "ready", "failed", "stale", "missing"},
    "rendering": {"ready", "failed", "stale", "missing"},
    "ready": {"stale", "missing"},
    "failed": {"queued", "stale"},
    "stale": {"queued"},
    "missing": {"queued"},
}


def set_preview_status(
    db,
    preview_id: str,
    status: str,
    *,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> dict:
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT status FROM packaging_preview_artifacts WHERE preview_id=?",
            (preview_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"找不到包装预览：{preview_id}")
        current = row["status"]
        if status != current and status not in _ALLOWED_TRANSITIONS[current]:
            raise RuntimeError(
                f"PACKAGING_PREVIEW_STATE_INVALID: {current} 不能变为 {status}"
            )
        connection.execute(
            """
            UPDATE packaging_preview_artifacts
            SET status=?, error_code=?, error_detail=?,
                viewed_at=CASE WHEN ?='ready' THEN viewed_at ELSE NULL END,
                updated_at=datetime('now')
            WHERE preview_id=?
            """,
            (status, error_code, error_detail, status, preview_id),
        )
    result = get_preview_artifact(db, preview_id)
    if result is None:  # pragma: no cover
        raise KeyError(f"找不到包装预览：{preview_id}")
    return result


def _resolve_cache_path(project_root: Path, rel_path: str) -> Path:
    relative = Path(rel_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("PACKAGING_PREVIEW_PATH_INVALID: 预览路径无效")
    root = (Path(project_root).resolve() / PREVIEW_CACHE_RELATIVE_ROOT).resolve()
    resolved = (Path(project_root).resolve() / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RuntimeError(
            "PACKAGING_PREVIEW_PATH_INVALID: 预览不在受控缓存目录"
        ) from error
    if resolved.suffix.lower() != ".mp4":
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_INVALID: 预览必须是 MP4")
    return resolved


def finalize_preview_artifact(
    db,
    preview_id: str,
    *,
    project_root: Path,
    rel_path: str,
    duration_seconds: float,
    coverage: list[dict],
    timeline_map: list[dict] | tuple[dict, ...] = (),
    preview_receipt: dict | None = None,
) -> dict:
    if preview_receipt is not None:
        _validate_preview_receipt(preview_receipt, duration_seconds, timeline_map)
    media_path = _resolve_cache_path(Path(project_root), rel_path)
    if not filesystem_path(media_path).is_file():
        set_preview_status(
            db,
            preview_id,
            "missing",
            error_code="PACKAGING_PREVIEW_MEDIA_MISSING",
        )
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
    signature = file_content_signature(media_path)
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT status FROM packaging_preview_artifacts WHERE preview_id=?",
            (preview_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"找不到包装预览：{preview_id}")
        if "ready" not in _ALLOWED_TRANSITIONS[row["status"]]:
            raise RuntimeError(
                f"PACKAGING_PREVIEW_STATE_INVALID: {row['status']} 不能变为 ready"
            )
        connection.execute(
            """
            UPDATE packaging_preview_artifacts
            SET status='ready', rel_path=?, output_signature_json=?,
                duration_seconds=?, size_bytes=?, coverage_json=?, timeline_map_json=?,
                preview_receipt_json=?,
                error_code=NULL, error_detail=NULL, viewed_at=NULL,
                updated_at=datetime('now')
            WHERE preview_id=?
            """,
            (
                Path(rel_path).as_posix(),
                json.dumps(signature, sort_keys=True),
                float(duration_seconds),
                signature["size_bytes"],
                json.dumps(coverage, ensure_ascii=False, sort_keys=True),
                json.dumps(timeline_map, ensure_ascii=False, sort_keys=True),
                (json.dumps(preview_receipt, ensure_ascii=False, sort_keys=True)
                 if preview_receipt is not None else None),
                preview_id,
            ),
        )
    result = get_preview_artifact(db, preview_id)
    if result is None:  # pragma: no cover
        raise KeyError(f"找不到包装预览：{preview_id}")
    return result


def mark_preview_viewed(db, task_id: str, preview_id: str, *, watch_session_id=None, project_root=None) -> dict:
    from .packaging_watch import complete_preview_watch
    if not watch_session_id:
        raise RuntimeError("FULL_WATCH_REQUIRED: 请先连续观看当前包装预览")
    if project_root is None:
        from .render_mix_adapter import PROJECT_ROOT
        project_root = PROJECT_ROOT
    complete_preview_watch(db, task_id, preview_id, watch_session_id, project_root)
    with db.transaction(immediate=True) as connection:
        changed = connection.execute(
            """
            UPDATE packaging_preview_artifacts
            SET viewed_at=datetime('now'), updated_at=datetime('now')
            WHERE preview_id=? AND task_id=? AND status='ready'
            """,
            (preview_id, task_id),
        ).rowcount
    if changed != 1:
        raise RuntimeError("PACKAGING_PREVIEW_NOT_READY: 预览尚未完成")
    result = get_preview_artifact(db, preview_id)
    if result is None:  # pragma: no cover
        raise KeyError(f"找不到包装预览：{preview_id}")
    return result


def resolve_packaging_preview_media(
    db, preview_id: str, project_root: Path
) -> Path:
    preview = get_preview_artifact(db, preview_id)
    if preview is None:
        raise KeyError(f"找不到包装预览：{preview_id}")
    if preview["status"] != "ready" or not preview.get("rel_path"):
        if preview["status"] == "missing":
            raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
        raise RuntimeError("PACKAGING_PREVIEW_NOT_READY: 预览尚未完成")
    media_path = _resolve_cache_path(Path(project_root), preview["rel_path"])
    if not filesystem_path(media_path).is_file():
        set_preview_status(
            db,
            preview_id,
            "missing",
            error_code="PACKAGING_PREVIEW_MEDIA_MISSING",
        )
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
    actual = file_content_signature(media_path)
    if actual != preview["output_signature"]:
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_CHANGED: 预览文件内容已变化")
    return media_path
