"""Freeze Cutroom's actual render input for both preview lineage and draft export."""
from __future__ import annotations

import json
import math
import hashlib
from pathlib import Path
from suo_jian.blueprint.templates import digest


def number(value, *, minimum=0.0):
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ValueError("执行时间和尺寸必须是有效的正数。")
    return value


def file_signature(path: Path) -> dict:
    with Path(path).open("rb") as handle:
        sha = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"size_bytes": Path(path).stat().st_size, "sha256": sha}


def candidate_render_path(root: Path, task_id: str, candidates: list[dict]) -> Path | None:
    """Bind to the selected candidate, never to a different file's modification time."""
    if not candidates:
        return None
    candidate = max(candidates, key=lambda row: int(row["version_no"]))
    label = str(candidate.get("version_label") or "")
    if not label or label in {".", ".."} or any(char in label for char in "/\\:"):
        raise ValueError("Cutroom 候选版本目录无效。")
    render = (Path(root) / "runtime/workbench/tasks" / task_id / "render").resolve()
    path = (render / label / "adrender.json").resolve()
    path.relative_to(render)
    return path if path.is_file() else None


def normalize_render_plan(raw: dict, *, root: Path, task_id: str, preview_path: str,
                          template_id: str | None = None) -> dict:
    root = Path(root).resolve()
    def source(value):
        if not value:
            raise ValueError("执行方案存在缺失素材；不能导出占位草稿。")
        path = Path(value)
        return str((path if path.is_absolute() else root / path).resolve())

    width, height = (1920, 1080) if raw.get("ratio") == "16:9" else (1080, 1920)
    profile = raw.get("output") or {}
    width, height = int(profile.get("width", width)), int(profile.get("height", height))
    if min(width, height) <= 0:
        raise ValueError("执行方案画布无效。")
    clips, texts = [], []
    total_duration = sum(number(row["dur"], minimum=.001) for row in raw.get("segments", []))
    previous_end = 0.0
    preset = raw.get("subtitlePreset") or {}
    for index, row in enumerate(raw.get("segments", [])):
        start, duration = number(row["start"]), number(row["dur"], minimum=.001)
        if abs(start - previous_end) > .03:
            raise ValueError("执行方案镜头时间不连续。")
        previous_end = start + duration
        clip = {"id": f"镜头{index + 1:02d}", "source": source(row.get("src")),
                "start": start, "duration": duration,
                "source_start": number(row.get("sourceStart", 0)), "source_end": row.get("sourceEnd"),
                "audio_muted": True}
        if clip["source_end"] is not None:
            clip["source_end"] = number(clip["source_end"])
            if clip["source_end"] - clip["source_start"] < duration - .05:
                raise ValueError("源镜头窗口不足，不能无提示改变视频速度。")
        clips.append(clip)
    if not clips:
        raise ValueError("执行方案没有镜头。")
    for row in raw.get("subtitleEvents", raw.get("segments", [])):
        start, duration = number(row["start"]), number(row.get("dur", float(row["end"])-float(row["start"])) if "end" in row else row["dur"], minimum=.001)
        subtitle_text = row.get("displayText", row.get("text"))
        if subtitle_text:
            subtitle_start = number(row.get("subtitleStart", start))
            subtitle_end = number(row.get("subtitleEnd", start + duration), minimum=.001)
            if subtitle_end <= subtitle_start or subtitle_end > total_duration + .03:
                raise ValueError("执行方案字幕时间必须位于整条配音时长范围内。")
            texts.append({"text": subtitle_text, "start": subtitle_start,
                          "duration": subtitle_end - subtitle_start,
                          "kind": "字幕", "style": {**preset, "size":row.get("displaySize",preset.get("size",46)), "numberPreset":row.get("numberPreset")}})
    audio = []
    voice = raw.get("voice")
    voice = voice if isinstance(voice, str) else (voice or {}).get("src")
    audio.append({"kind": "人声", "source": source(voice), "start": 0, "duration": previous_end,
                  "gain": 1, "loop": False})
    bgm = raw.get("bgm") or {}
    if bgm.get("src"):
        audio.append({"kind": "背景音乐", "source": source(bgm["src"]), "start": 0,
                      "duration": previous_end, "gain": number(bgm.get("gain", .13)),
                      "source_start":number(bgm.get("start",0)), "fade_in":number(bgm.get("fadeIn",.35)), "fade_out":number(bgm.get("fadeOut",1.5)),
                      "loop": True, "duck": bool(bgm.get("duck"))})
    for row in raw.get("sfx") or []:
        audio.append({"kind": "音效", "source": source(row["src"]), "start": number(row.get("start", 0)),
                      "duration": None, "gain": number(row.get("gain", .35)), "loop": False})
    for key, kind in (("titles", "标题模板"), ("fancyTexts", "花字")):
        for row in raw.get(key) or []:
            texts.append({"text": row["text"], "start": number(row["start"]),
                          "duration": number(row["end"]) - number(row["start"]), "kind": kind,
                          "style": {"size": width * (.072 if key == "titles" else .06),
                                    "primary_hex": "#FFFFFF", "y": .12 if key == "titles" else .22}})
    overlays = []
    watermark = raw.get("watermark") or {}
    if watermark.get("src"):
        position = watermark.get("position") or {}
        overlays.append({"kind": "品牌图层", "source": source(watermark["src"]),
                         "start": 0, "duration": previous_end, "x": position.get("x", .04),
                         "y": position.get("y", .02), "scale": position.get("width", .28),
                         "opacity": watermark.get("opacity", 1)})
    for row in raw.get("stickers") or []:
        overlays.append({"kind": "贴纸", "source": source(row["src"]), "start": row.get("start", 0),
                         "duration": row.get("end", previous_end) - row.get("start", 0),
                         "x": row.get("x", .72), "y": row.get("y", .12),
                         "scale": row.get("scale", .2), "opacity": row.get("opacity", 1)})
    for row in raw.get("emphasisOverlays",[]):
        overlays.append({"kind":"重点花字素材","source":source(row["src"]),"start":number(row["start"]),
                         "duration":number(row["end"])-number(row["start"]),"x":row.get("x",0),"y":row.get("y",0),"scale":row.get("scale",1),"opacity":row.get("opacity",1)})
    return {"schema_version": "suojian-execution-plan-1", "task_id": task_id,
            "template_id": template_id, "render_input_digest": digest(raw),
            "canvas": {"width": width, "height": height, "fps": 30}, "duration": previous_end,
            "clips": clips, "texts": texts, "audio": audio, "overlays": overlays,
            "reference_match": raw.get("referenceMatch"), "music_match":raw.get("musicMatch"), "emphasis_report":raw.get("emphasisReport"),
            "transition": {"kind": "切镜" if raw.get("transitionMode") == "cut" else "叠化", "duration": 0 if raw.get("transitionMode") == "cut" else .4}, "preview_path": preview_path,
            "limitations": ["草稿字幕字体使用剪映内近似字体；不会伪称识别到精确花字模板编号。",
                            "参考模式使用直接切镜；其他模式的转场使用剪映原生近似效果。",
                            "重点花字在预览中使用独立透明素材和入场动画；草稿保留独立素材层，复杂花字不是原生可编辑文字模板。",
                            "独立人声、音乐和音效保留；预览中的侧链压低及整体响度处理未逐样本复刻。",
                            "参考剪法用于选镜提示与目录字幕近似映射；本版执行以 Cutroom 实际包装方案为准。",
                            "参考视频的逐事件音效重建、精确花字和贴纸模板匹配尚未接入；当前使用实际包装素材。"]}


def load_bound_plan(path: Path, root: Path, task_id: str, candidate: dict, template_id=None):
    path, expected = path.resolve(), (root / "runtime" / "workbench" / "tasks" / task_id / "render").resolve()
    path.relative_to(expected)
    if path.parent.name != candidate.get("version_label"):
        raise ValueError("预览候选与渲染方案版本不一致。")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return normalize_render_plan(raw, root=root, task_id=task_id,
                                 preview_path=candidate["output_path"], template_id=template_id)
