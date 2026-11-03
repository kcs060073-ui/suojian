"""Read-only packaging geometry checks in the final output coordinate system.

Collision reports are conservative layout hints, not face detection or approval.
No draft content, timing, or manually selected placement is rewritten here.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache
import math
import re
from pathlib import Path
import subprocess


# Only a task-detail read opts in. Resolve a repeated registered resource once
# within that response; no path or resource cache survives to another request.
_layout_resources = ContextVar("packaging_layout_resources", default=None)


@contextmanager
def layout_read_snapshot():
    token = _layout_resources.set({})
    try:
        yield
    finally:
        _layout_resources.reset(token)


def _layout_resource(db, category, asset_ref):
    from . import packaging_plan
    snapshot = _layout_resources.get()
    key = (id(db), str(packaging_plan.MANIFEST_DIR), category, asset_ref)
    if snapshot is not None and key in snapshot:
        return deepcopy(snapshot[key])
    value = (packaging_plan.find_item(category, asset_ref, db) or {},
             packaging_plan.resolve_manifest_asset_path(category, asset_ref, db))
    if snapshot is not None:
        snapshot[key] = deepcopy(value)
    return value


@lru_cache(maxsize=48)
def _font(path: str, size: int, mtime_ns: int, file_size: int):
    from PIL import ImageFont
    from io import BytesIO
    # Windows FreeType keeps path handles open; load bytes so removable fonts
    # can be replaced or restored while the service is running.
    return ImageFont.truetype(BytesIO(Path(path).read_bytes()), size)


def _intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _text_extent(path, text, size, padding):
    stat = path.stat()
    # libass Fontsize denotes the em cell after ascender/descender scaling,
    # while FreeType/Pillow's requested size is the raw em. Measure at 1000px
    # to avoid the rounding error of small ascender/descender values.
    font = _font(str(path), 1000, stat.st_mtime_ns, stat.st_size)
    ascent, descent = font.getmetrics()
    factor = size / (ascent + descent)
    lines = text.split("\n")
    width = max(font.getlength(line) * factor + max(0, len(line) - 1) for line in lines)
    # Include ASS box borders and a two-pixel raster rounding allowance.
    return width + padding * 2 + 2, size * len(lines) + padding * 2 + 2



def inspect_packaging_layout(db, plan: dict) -> dict:
    from . import packaging_plan
    from .packaging_instances import ROLES
    from .tools import ToolNotFound
    import json
    task = db.connection.execute("SELECT task_type,brief_json FROM tasks WHERE task_id=?", (plan["task_id"],)).fetchone()
    brief = json.loads(task["brief_json"] or "{}")
    digital = task["task_type"] == "digital_human"
    ratio = "9:16" if digital else brief.get("aspect_ratio", "9:16")
    width, height = (1920, 1080) if ratio == "16:9" else (1080, 1920)
    report = {"version": 2, "frameSize": [width, height], "safeArea": [.02, .02, .98, .98],
              "bounds": [], "issues": [], "personDetection": "not_performed"}
    if int(plan.get("schema_version", 1)) < 2:
        return report  # Historical fixed-slot outputs are never reinterpreted.
    root = packaging_plan.MANIFEST_DIR.resolve().parents[1]
    issues, boxes = report["issues"], report["bounds"]
    subtitle = next((i for i in packaging_plan.active_items(plan) if i["slot"] == "subtitle" and not i["is_backup"]), None)
    subtitle_box = None
    if subtitle:
        preset = packaging_plan.find_item("subtitle", subtitle["asset_ref"], db) or {}
        size = float(max(preset.get("size", 93), preset.get("punch_size", 0)) if digital else preset.get("size", 93))
        margin = float(preset.get("margin_v", 144))
        alignment = int(preset.get("alignment", 2))
        scale_y = 1.0
        # render-ad uses the preset's raw ASS values at its actual canvas size.
        # An explicit ass_style overrides the structured preset in that renderer.
        if not digital and preset.get("ass_style"):
            fields = preset["ass_style"].split(",")
            size, scale_y = float(fields[2]), float(fields[12]) / 100
            alignment, margin = int(fields[18]), float(fields[21])
        band_height = size * scale_y * 1.6 * int(preset.get("max_lines", 2))
        if alignment >= 7:
            top, bottom = margin, margin + band_height
        elif alignment >= 4:
            top, bottom = (height - band_height) / 2, (height + band_height) / 2
        else:
            bottom = height - margin
            top = bottom - band_height
        subtitle_box = (.02, top / height, .98, bottom / height)
        from .packaging_appearance import render_appearance
        appearance = render_appearance(plan, db)
        if appearance:
            size = appearance.get("fontSize", size)
            center = appearance.get("x", .5)
            bottom = appearance.get("y", bottom / height)
            span = appearance.get("width", .96)
            subtitle_box = (center-span/2, bottom-size*1.6*int(preset.get("max_lines",2))/height, center+span/2, bottom)
            if min(subtitle_box[:2]) < .02-1e-6 or max(subtitle_box[2:]) > .98+1e-6:
                issues.append({"code":"PACKAGING_SAFE_AREA","severity":"blocking","instanceIds":[],"message":"字幕区域超出画面，请缩小字号或移回画面内"})

    report["subtitleBox"] = list(subtitle_box) if subtitle_box else None
    scenes = [dict(row) for row in db.connection.execute(
        "SELECT narrative_start,narrative_end,mix_form FROM scene_cards WHERE task_id=?", (plan["task_id"],))]
    for item in packaging_plan.active_instances(plan):
        role, identity = item["role"], item["instance_id"]
        label = {"title_card":"标题", "info_card":"信息卡", "brand":"Logo", "sticker":"贴纸", "fancy_text":"花字"}.get(role, role)
        if item.get("content_text"): label += "「" + item["content_text"][:16] + "」"
        if role == "sfx" or role not in ROLES: continue
        values = [item.get(k) for k in ("x", "y", "scale")]
        if not all(isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in values):
            continue  # Existing domain validation explains pending positions.
        x, y, scale = values
        category = "title_card" if role in {"info_card", "title_card", "brand"} else role
        manifest, path = _layout_resource(db, category, item["asset_ref"])
        if not path or not path.is_file(): continue  # Existing resource validation owns missing files.
        font_size = None
        try:
            if role in {"title_card", "info_card"} or manifest.get("design_version") == "refined-v1":
                if not item.get("content_text"): continue
                if manifest.get("design_version") == "refined-v1":
                    from .packaging_refined import renderer
                    _, pixels = renderer().draw_element({"text": item["content_text"], "role": role,
                        "x": x, "y": y, "scale": scale,
                        "contentLayout": json.loads(item.get("content_layout_json") or "{}")}, width, height)
                    box = (pixels[0]/width, pixels[1]/height, pixels[2]/width, pixels[3]/height)
                    font_size = (120 if role == "title_card" else 56) * scale / (.60 if role == "title_card" else .40)
                else:
                    font_size = max(24, round((78 if digital else width * .072) * scale / .84))
                    w, h = _text_extent(path, item["content_text"], font_size, 2 if digital else 8)
                    # ASS opaque box border extends slightly before the chosen anchor.
                    box = (x - (2 if digital else 8) / width, y - (2 if digital else 8) / height,
                           x + (w - (2 if digital else 8)) / width, y + (h - (2 if digital else 8)) / height)
            elif role == "fancy_text":
                if not item.get("content_text"): continue
                # Reuse the production graphic generator; don't invent a second
                # formula for background, shadow, glow, or multiline dimensions.
                import importlib.util
                spec = importlib.util.spec_from_file_location("cutroom_fancy_layout", Path(__file__).resolve().parents[1] / "scripts/gen-fancy-text.py")
                generator = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(generator)
                generator.FONT_DIR = str(root / "assets/fonts")
                w, h = generator.render_text(item["content_text"], manifest, measure_only=True)
                font_size = 200 * scale * width / w
                box = (x,y,x+scale,y+scale * width * h / w / height)
            else:
                if path.suffix.casefold() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
                    from .tools import find_ffprobe, default_ffprobe_search_roots
                    import os
                    tool = find_ffprobe(os.environ, search_roots=default_ffprobe_search_roots(os.environ))
                    result = subprocess.run([str(tool), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation", "-of", "json", str(path)],
                        capture_output=True, check=True, timeout=10)
                    stream = json.loads(result.stdout)["streams"][0]
                    w, h = stream["width"], stream["height"]
                    from .qa import _parse_rotation
                    if _parse_rotation(stream) in {90, 270}:
                        w, h = h, w
                else:
                    from PIL import Image
                    with Image.open(path) as image:
                        image.load()
                        w, h = image.size
                box = (x,y,x+scale,y+scale * width * h / w / height)
        except (OSError, ValueError, KeyError, IndexError, ToolNotFound, subprocess.SubprocessError) as error:
            issues.append({"code": "PACKAGING_LAYOUT_RESOURCE_INVALID", "severity": "blocking", "instanceIds": [identity],
                           "message": f"{label}素材无法解码，请恢复或替换资源：{error}"})
            continue
        bound = {"instanceId": identity, "role": role, "box": list(box),
                 "start": item["start_seconds"], "end": item["end_seconds"]}
        if font_size is not None: bound["fontSize"] = font_size
        if font_size is not None:
            minimum = 44 if width < height else 40
            if font_size < minimum:
                issues.append({"code": "PACKAGING_TEXT_TOO_SMALL", "severity": "warning", "instanceIds": [identity],
                               "message": f"{label}文字偏小（约{font_size:.0f}号），建议至少{minimum}号或分行显示"})
            characters = len(re.sub(r"[\s，。！？；、,.!?;]", "", item.get("content_text") or ""))
            needed = max(1.0, characters / 8)
            if bound["end"] - bound["start"] + .05 < needed:
                issues.append({"code": "PACKAGING_READING_TIME", "severity": "warning", "instanceIds": [identity],
                               "message": f"{label}显示时间偏短，建议至少{needed:.1f}秒；请缩短文字或调整出现时段"})
        boxes.append(bound)
        if box[0] < .02 - 1e-6 or box[1] < .02 - 1e-6 or box[2] > .98 + 1e-6 or box[3] > .98 + 1e-6:
            issues.append({"code": "PACKAGING_SAFE_AREA", "severity": "blocking", "instanceIds": [identity],
                           "message": f"{label}元素超出画面安全区，请缩短文字、缩小或调整位置（保留当前人工设置）"})
        if subtitle_box and _intersects(box, subtitle_box):
            issues.append({"code": "PACKAGING_SUBTITLE_OVERLAP", "severity": "warning", "instanceIds": [identity],
                           "message": f"{label}可能遮挡字幕预留区域，请在真实预览中核对并按需调整"})
        if digital and _intersects(box, (.18,.10,.82,.72)) and any(
            s["mix_form"] in {"数字人全屏", "画中画"} and s["narrative_start"] < bound["end"] and bound["start"] < s["narrative_end"] for s in scenes):
            issues.append({"code": "PACKAGING_PERSON_REGION", "severity": "warning", "instanceIds": [identity],
                           "message": f"{label}进入人物中央构图区域，请核对是否遮挡人物或人脸（构图提示，未做人脸检测）"})
    for index, left in enumerate(boxes):
        for right in boxes[index+1:]:
            if left["start"] < right["end"] and right["start"] < left["end"] and _intersects(left["box"],right["box"]):
                issues.append({"code": "PACKAGING_OVERLAP", "severity": "warning", "instanceIds": [left["instanceId"],right["instanceId"]],
                               "message": "两个包装元素在同一时间重叠，请核对层级或调整位置/时间"})
    return report


def assert_packaging_layout(report: dict) -> None:
    blocking = [i for i in report["issues"] if i["severity"] == "blocking"]
    if blocking:
        raise ValueError(blocking[0]["code"] + ": " + "；".join(i["message"] for i in blocking[:4]))
