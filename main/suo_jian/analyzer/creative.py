from __future__ import annotations

import subprocess
from pathlib import Path


def _bpm_range(beats: dict) -> dict:
    bpm = beats.get("bpm") if beats.get("available") else None
    if isinstance(bpm, (int, float)) and bpm > 0:
        rounded = int(round(bpm))
        return {"min": max(40, rounded - 2), "max": rounded + 2}
    return {"min": 100, "max": 130}


def _subtitle_production_plan(width: int, height: int) -> dict:
    """A production-ready caption recipe; values scale with the actual canvas."""
    landscape = width >= height
    font_size = round(height * (0.064 if landscape else 0.052))
    stroke_width = max(2, round(font_size * 0.09))
    anchor_y = 0.82 if landscape else 0.68
    max_chars = 15 if landscape else 11
    return {
        "canvas": {"width": width, "height": height, "ratio": "16:9" if landscape else "9:16"},
        "safe_area": {
            "anchor": "bottom_center",
            "x": 0.5,
            "y": anchor_y,
            "left": 0.08,
            "right": 0.92,
            "top": 0.64 if landscape else 0.57,
            "bottom": 0.91 if landscape else 0.86,
            "note": "避开平台底部按钮、画面人物脸部与重要产品信息。",
        },
        "base_style": {
            "font_family_candidates": ["思源黑体 Heavy", "阿里巴巴普惠体 Heavy", "剪映默认粗体"],
            "font_weight": 900,
            "font_size_px": font_size,
            "line_height": 1.16,
            "letter_spacing_px": 0,
            "max_lines": 2,
            "recommended_chars_per_line": max_chars,
            "fill": "#FFFFFF",
            "stroke": {"color": "#111111", "width_px": stroke_width},
            "shadow": {"enabled": True, "color": "#000000", "opacity": 0.52, "offset_x": 0, "offset_y": 3, "blur_px": 6},
        },
        "emphasis": {
            "enabled": True,
            "target": "金额、数字、地名、产品名、行动词",
            "color": "#FFD800",
            "secondary_color": "#F02B21",
            "rule": "每条只突出 1–2 个信息单元，避免整句同时上色。",
        },
        "animation": {
            "in": "0.16 秒轻微放大弹入（95% → 100%）",
            "hold": "保持稳定，不做持续抖动",
            "out": "切镜时直接替换；行动号召可在结尾 0.18 秒淡出",
        },
        "timing": {
            "minimum_duration_seconds": 1.0,
            "recommended_duration_seconds": "1.5–2.5",
            "reading_speed": "中文 4–6 字/秒；数字和单位额外留出 0.2 秒",
            "line_break_rule": "在语义短语处换行，不拆分价格、面积、时间和单位。",
        },
        "flower_text_candidates": ["白字黑边·资讯字幕", "黄字黑边·重点词", "醒目标题·轻弹入"],
        "confidence": 0.68,
        "caveat": "这是按画布比例生成的可编辑制作规格；字体与剪映花字素材名仍需在当前剪映版本中试听、试排后确认。",
    }


def analyze_creative(video: Path, metadata: dict, beats: dict, output: Path) -> tuple[dict, list[str]]:
    """Build conservative creative hints and extract real cover-frame candidates."""
    output.mkdir(parents=True, exist_ok=True)
    duration = max(float(metadata.get("duration", 0)), 0.1)
    height = int(metadata.get("height") or 1080)
    candidates = []
    notes: list[str] = []
    timestamps = [round(min(duration - 0.05, max(0.0, duration * ratio)), 2) for ratio in (0.08, 0.5, 0.88)]
    reasons = ["前段建立画面，适合承载主标题", "中段主体信息候选", "后段行动号召候选"]

    for rank, (timestamp, reason) in enumerate(zip(timestamps, reasons), start=1):
        filename = f"cover_frame_{rank}.jpg"
        destination = output / filename
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(timestamp), "-i", str(video), "-frames:v", "1",
            "-q:v", "2", str(destination),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=30)
            candidates.append({"rank": rank, "timestamp": timestamp, "file": filename, "reason": reason})
        except (OSError, subprocess.SubprocessError):
            notes.append(f"封面候选帧 {timestamp}s 提取失败。")

    size_min = max(28, round(height * 0.038))
    size_max = max(size_min + 4, round(height * 0.05))
    bpm_range = _bpm_range(beats)
    recommended = candidates[0] if candidates else {"timestamp": timestamps[0], "reason": "待重新提取视频帧"}
    result = {
        "subtitle_style": {
            "template_label": "底部居中·粗体高对比（待人工确认）",
            "font_size_px_estimate": {"min": size_min, "max": size_max},
            "font_family_candidates": ["思源黑体 Heavy", "阿里巴巴普惠体 Heavy", "剪映默认黑体"],
            "jianying_flower_text_candidates": ["黄字黑边", "醒目标题", "综艺花字-黄色粗体"],
            "style_description": "底部居中、粗体、高对比描边；颜色与重点词需结合画面人工确认。",
            "caveat": "当前版本根据视频尺寸与常见短视频排版给出候选，不声称精确识别字体或剪映模板 ID。",
        },
        "subtitle_production": _subtitle_production_plan(int(metadata.get("width") or 1920), height),
        "background_music": {
            "bpm_estimate": bpm_range,
            "mood": ["轻快", "商务", "科技"],
            "search_keywords": ["商务科技", "产业宣传", "轻快", f"{bpm_range['min']}-{bpm_range['max']} BPM"],
            "caveat": "这是节奏与场景的检索建议，不是原曲指纹识别结果。",
        },
        "cover": {
            "selection_mode": "video_frame",
            "recommended_timestamp": recommended["timestamp"],
            "reason": recommended["reason"],
            "candidate_frames": candidates,
            "headline": "从视频文案提炼 8–14 字主标题",
            "subheadline": "将价格、地点或核心卖点作为副标题",
        },
    }
    return result, notes
