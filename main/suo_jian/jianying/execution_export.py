"""Export real original clips, text and audio, never import the baked preview."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from suo_jian.blueprint.templates import digest, write_json
from .text_geometry import native_font_size, native_border_width, prepare_export_plan
from .layered_emphasis import add_layer_animation, apply_native_binding


def _sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def register_native_draft(output: Path) -> dict:
    """Register our generated folder without replacing other local projects."""
    output = Path(output).resolve()
    index_path = output.parent / "root_meta_info.json"
    original = index_path.read_bytes() if index_path.exists() else None
    index = json.loads(original.decode("utf-8-sig")) if original else {"all_draft_store": [], "draft_ids": 0}
    if not isinstance(index.get("all_draft_store"), list):
        raise ValueError("剪映草稿列表格式不受支持，未修改现有列表。")
    content = json.loads((output / "draft_content.json").read_text(encoding="utf-8-sig"))
    meta_path = output / "draft_meta_info.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    draft_id = content["id"]
    known = next((row for row in index["all_draft_store"] if row.get("draft_id") == draft_id), None)
    if known and Path(known.get("draft_fold_path") or ".").resolve() != output:
        raise ValueError("该草稿 ID 已被另一个工程使用，未覆盖原工程。请重新导出为独立草稿。")
    if known and meta.get("draft_name") and meta.get("draft_fold_path"):
        return {"registered": True, "draft_id": draft_id}
    # The editor owns this index while running. Never compete with its autosave.
    if os.name == "nt":
        processes = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                   "$sjEditors = @(Get-Process -Name JianyingPro -ErrorAction SilentlyContinue); if ($sjEditors.Count -gt 0) { Write-Output 'RUNNING' }"],
                                   capture_output=True, text=True, errors="replace", timeout=15)
        if processes.returncode != 0:
            raise ValueError("无法确认剪映运行状态，未修改草稿列表。请检查本机进程读取权限。")
        if "RUNNING" in processes.stdout:
            raise ValueError("请先关闭剪映再保存草稿，以免与剪映自动保存同时修改草稿列表。")
    stamp = time.time_ns() // 1000
    meta.update(draft_id=draft_id, draft_name=meta.get("draft_name") or output.name,
                draft_fold_path=str(output), draft_root_path=str(output.parent),
                draft_json_file=str(output / "draft_content.json"), tm_duration=content["duration"],
                tm_draft_create=meta.get("tm_draft_create") or stamp, tm_draft_modified=stamp,
                draft_is_invisible=False, tm_draft_removed=0)
    entry_keys = ("draft_id", "draft_name", "draft_fold_path", "draft_root_path", "draft_json_file",
                  "draft_cover", "tm_duration", "tm_draft_create", "tm_draft_modified", "tm_draft_removed",
                  "draft_is_invisible", "draft_type", "draft_new_version")
    entry = {key: meta.get(key, "") for key in entry_keys}
    entry.update(cloud_draft_sync=False, cloud_draft_cover=False, streaming_edit_draft_ready=True)
    if known is None:
        index["all_draft_store"].insert(0, entry)
        index["draft_ids"] = max(int(index.get("draft_ids", 0)) + 1, len(index["all_draft_store"]))
    else:
        known.update(entry)
    index["root_path"] = str(output.parent)
    if original is not None:
        backup = index_path.with_name("root_meta_info.suojian-backup-" + hashlib.sha256(original).hexdigest()[:12] + ".json")
        if not backup.exists():
            shutil.copy2(index_path, backup)
        if index_path.read_bytes() != original:
            raise ValueError("剪映草稿列表刚被其他程序更新，请稍后重试。")
    write_json(meta_path, meta)
    write_json(index_path, index)
    return {"registered": True, "draft_id": draft_id}


def editable_emphasis_plan(plan):
    """Opt-in native text alternative; keep the original preview plan immutable."""
    value = json.loads(json.dumps(plan))
    reports = value.get("emphasis_report") or []
    for row in value.get("overlays", []):
        if row["kind"] != "重点花字素材":
            continue
        match = next((item for item in reports if abs(item["start"]-row["start"]) < .01 and abs(item["end"]-row["start"]-row["duration"]) < .02), None)
        if not match:
            raise ValueError("重点花字缺少文字映射，请先重新读取预览。")
        base = ((value.get("reference_match") or {}).get("policy") or {}).get("subtitle", {})
        value["texts"].append({"kind":"可编辑重点文字", "text":match["text"], "start":row["start"], "duration":row["duration"],
            "style":{**base,"size":value["canvas"]["width"]*(.057 if match["template"] == "business-label-A" else .07),
                     "y":match["center_y"], "max_lines":1}})
    value["overlays"] = [row for row in value.get("overlays", []) if row["kind"] != "重点花字素材"]
    value["export_mode"] = "editable-emphasis"
    value["limitations"] = [item for item in value.get("limitations", []) if not item.startswith("重点花字在预览")]
    value["limitations"].append("重点花字已改为剪映原生文字，可直接改字；沿用颜色、位置与时间，标签底板、数字渐变及入场动画采用简化效果。")
    return value


def build_execution_draft(plan: dict, draft_root: Path, name: str) -> tuple[Path, dict]:
    import pyJianYingDraft as d
    plan = prepare_export_plan(plan)
    if plan.get("schema_version") != "suojian-execution-plan-1":
        raise ValueError("没有可用的统一执行方案。")
    output = Path(draft_root) / name
    if output.exists():
        manifest = output / "suojian_execution_manifest.json"
        if manifest.exists():
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            if saved.get("execution_digest") == digest(plan) and (output / "draft_content.json").is_file() and (output / "draft_meta_info.json").is_file():
                saved["draft_registration"] = register_native_draft(output)
                write_json(manifest, saved)
                return output, saved
        raise FileExistsError("同名草稿已存在；不会覆盖您可能在剪映中编辑过的工程。")
    # Validate all sources before creating any draft.
    sources = {row["source"] for key in ("clips", "audio", "overlays") for row in plan[key]}
    missing = [path for path in sources if not Path(path).is_file()]
    if missing:
        raise FileNotFoundError("草稿所需的原始素材已移动：" + Path(missing[0]).name)
    for value in sorted(sources):
        expected = plan.get("source_signatures", {}).get(value)
        if expected and (_sha(value) != expected["sha256"] or Path(value).stat().st_size != expected["size_bytes"]):
            raise IOError("原始素材在预览后发生变化，请重新生成预览，不能悄悄替换草稿内容。")
    canvas = plan["canvas"]
    Path(draft_root).mkdir(parents=True, exist_ok=True)
    script = d.DraftFolder(str(draft_root)).create_draft(name, canvas["width"], canvas["height"], canvas["fps"])
    # The upstream template contains a fixed UUID; each new work must be distinct.
    script.content["id"] = str(uuid.uuid4()).upper()
    assets = output / "素材"
    assets.mkdir()
    copied = {}
    for value in sorted(sources):
        source = Path(value)
        target = assets / (hashlib.sha256(value.encode()).hexdigest()[:12] + source.suffix)
        shutil.copy2(source, target)
        if _sha(source) != _sha(target):
            raise IOError("复制素材校验失败，请检查安全软件拦截。")
        copied[value] = target
    def span(start, duration):
        return d.trange(f"{float(start):.6f}s", f"{float(duration):.6f}s")
    def track(kind, label):
        return script.append_track(d.TrackSpec(kind, label))
    def color(hex_value):
        value = str(hex_value).lstrip("#")
        return tuple(int(value[i:i+2], 16) / 255 for i in (0, 2, 4))

    video_track = track(d.TrackType.video, "园区原始镜头")
    video_count = 0
    for index, row in enumerate(plan["clips"]):
        material = d.VideoMaterial(str(copied[row["source"]]))
        left, cursor, offset = row["duration"], row["start"], row["source_start"]
        last = None
        while left > .00001:
            available = material.duration / 1e6 - offset
            if available <= .00001:
                raise ValueError("镜头源起点超出素材范围。")
            length = min(left, available)
            # Center-crop to canvas just like 剪辑工作台's scale+crop renderer.
            fit = max(canvas["width"] / material.width, canvas["height"] / material.height)
            scale = fit / min(canvas["width"] / material.width, canvas["height"] / material.height)
            last = d.VideoSegment(material, span(cursor, length), source_timerange=span(offset, length),
                                  volume=0, clip_settings=d.ClipSettings(scale_x=scale, scale_y=scale))
            script.add_segment(last, video_track)
            video_count += 1
            left, cursor, offset = left - length, cursor + length, 0
        if last and plan.get("transition",{}).get("kind") != "切镜" and index < len(plan["clips"]) - 1 and last.target_timerange.duration > 400000:
            last.add_transition(getattr(d.TransitionType, "叠化"), duration="0.4s")

    text_tracks = {}
    text_count = 0
    for row in plan["texts"]:
        if row["duration"] <= 0 or not row["text"]:
            continue
        if row["kind"] not in text_tracks:
            text_tracks[row["kind"]] = track(d.TrackType.text, row["kind"])
        style = row["style"]
        font_name = str(style.get("fontname_guess", ""))
        font = (d.FontType.得意黑 if "Smiley" in font_name else d.FontType.SourceHanSerifCN_Bold if "Serif" in font_name else d.FontType.SourceHanSansCN_Bold)
        size = float(style.get("size", 46))
        # ASS anchors bottom-center; approximate its baseline in Jianying's center transform.
        y = style.get("y", 1 - (float(style.get("margin_v", 60)) + size * .6) / canvas["height"])
        segment = d.TextSegment(row["text"], span(row["start"], row["duration"]), font=font,
                    style=d.TextStyle(size=native_font_size(size, canvas["width"], canvas["height"]), bold=style.get("bold", True), italic=style.get("italic",False),
                                      color=color(style.get("primary_hex", "#FFFFFF")), align=1,
                                      auto_wrapping=style.get("max_lines",2) != 1, max_line_width=.88),
                    border=d.TextBorder(color=color(style.get("outline_hex", "#000000")),
                                        width=native_border_width(style.get("outline", 3), size)),
                    clip_settings=d.ClipSettings(transform_x=(float(style.get("x",.5))-.5)*2,transform_y=(.5 - y) * 2))
        add_layer_animation(segment,row,d,text=True)
        apply_native_binding(segment,row.get("native_binding"))
        script.add_segment(segment, text_tracks[row["kind"]])
        text_count += 1
    audio_tracks, sfx_index = [], 0
    for row in plan["audio"]:
        if row["start"] >= plan["duration"]:
            continue
        sfx_index += row["kind"] == "音效"
        label = row["kind"] + (str(sfx_index) if row["kind"] == "音效" else "")
        audio_track = track(d.TrackType.audio, label)
        material = d.AudioMaterial(str(copied[row["source"]]))
        duration = min(row.get("duration") or material.duration / 1e6, plan["duration"] - row["start"])
        if not row["loop"]:
            duration = min(duration, material.duration / 1e6)
        cursor, remaining = row["start"], duration
        offset = row.get("source_start",0)
        if offset < 0 or offset >= material.duration/1e6:
            raise ValueError("音乐源起点超出素材范围。")
        while remaining > .00001:
            length = min(remaining, material.duration / 1e6 - offset)
            if length <= 0:
                raise ValueError("音频素材时长为零。")
            audio_segment = d.AudioSegment(material, span(cursor, length), source_timerange=span(offset, length), volume=float(row["gain"]))
            fade_in = min(length,row.get("fade_in",0)) if cursor == row["start"] else 0
            fade_out = min(length,row.get("fade_out",0)) if remaining <= length+.00001 else 0
            if fade_in or fade_out: audio_segment.add_fade(int(fade_in*1e6),int(fade_out*1e6))
            script.add_segment(audio_segment,audio_track)
            cursor, remaining, offset = cursor + length, remaining - length, 0
        audio_tracks.append(label)
    for index, row in enumerate(plan["overlays"]):
        material = d.VideoMaterial(str(copied[row["source"]]))
        desired_w = row["scale"] * canvas["width"]
        desired_h = desired_w * material.height / material.width
        fit = min(canvas["width"] / material.width, canvas["height"] / material.height)
        scale = desired_w / material.width / fit
        segment = d.VideoSegment(material, span(row["start"], row["duration"]), volume=0,
            clip_settings=d.ClipSettings(alpha=row["opacity"], scale_x=scale, scale_y=scale,
                transform_x=(row["x"] + desired_w / canvas["width"] / 2 - .5) * 2,
                transform_y=(.5 - row["y"] - desired_h / canvas["height"] / 2) * 2))
        add_layer_animation(segment,row,d)
        overlay_track = (script.insert_track(d.TrackSpec(d.TrackType.video,row["kind"]),over_track=video_track) if row["kind"].startswith("花字装饰") else track(d.TrackType.video,row["kind"]+str(index+1)))
        script.add_segment(segment, overlay_track)
    script.save()
    meta_path = output / "draft_meta_info.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    meta.update(draft_id=script.content["id"], draft_name=name)
    write_json(meta_path, meta)
    report = {"execution_digest": digest(plan), "render_input_digest": plan["render_input_digest"],
              "task_id": plan["task_id"], "editable_video_segments": video_count,
              "editable_text_segments": text_count, "audio_tracks": audio_tracks,
              "independent_overlays": len(plan["overlays"]), "baked_preview_imported": False,
              "export_mode":plan.get("export_mode","appearance"), "emphasis_components":plan.get("emphasis_components",[]),
              "animated_text_segments":sum(bool(x.get("animation")) for x in plan["texts"]),
              "animated_decoration_segments":sum(bool(x.get("animation")) for x in plan["overlays"]),
              "human_reviewed": False, "limitations": plan["limitations"],
              "text_layout_version": plan["text_layout_version"], "text_geometry_editor_verified": False,
              "jianying_open_verified": False}
    write_json(output / "suojian_execution_manifest.json", report)
    report["draft_registration"] = register_native_draft(output)
    write_json(output / "suojian_execution_manifest.json", report)
    return output, report
