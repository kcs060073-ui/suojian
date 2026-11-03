"""Validated task-local subtitle presentation, shared by previews and exports."""
import json
import math
import re


def validate_appearance(value, db=None):
    if not isinstance(value, dict):
        raise ValueError("SUBTITLE_APPEARANCE_INVALID: 字幕设置必须是对象")
    limits = {"fontSize": (24, 180), "outlineWidth": (0, 12), "backgroundOpacity": (0, 1),
              "x": (.02, .98), "y": (.02, .98), "width": (.2, .96)}
    colors = {"primaryHex", "highlightHex", "outlineHex", "backgroundHex"}
    if set(value) - (set(limits) | colors | {"fontPresetId"}):
        raise ValueError("SUBTITLE_APPEARANCE_INVALID: 不支持的字幕设置")
    result = {}
    for key, item in value.items():
        if key in limits:
            low, high = limits[key]
            if isinstance(item, bool) or not isinstance(item, (float, int)) or not math.isfinite(item) or not low <= item <= high:
                raise ValueError("SUBTITLE_APPEARANCE_INVALID: 字幕大小或位置超出允许范围")
            result[key] = round(item, 4)
        elif key in colors:
            if not isinstance(item, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", item):
                raise ValueError("SUBTITLE_APPEARANCE_INVALID: 请选择有效颜色")
            result[key] = item.upper()
        else:
            from .packaging_plan import find_item, resolve_manifest_asset_path
            preset = find_item("subtitle", item, db) if isinstance(item, str) else None
            path = resolve_manifest_asset_path("subtitle", item, db) if preset else None
            if not preset or preset.get("license") != "commercial-approved" or not path or not path.is_file():
                raise ValueError("SUBTITLE_FONT_INVALID: 请选择可用的字幕字体")
            result[key] = item
    if "x" in result and "width" in result and (result["x"] - result["width"] / 2 < .02 - 1e-6 or result["x"] + result["width"] / 2 > .98 + 1e-6):
        raise ValueError("SUBTITLE_APPEARANCE_INVALID: 字幕区域超出画面，请移回画面内")
    return result


def read_appearance(plan):
    raw = plan.get("subtitle_appearance_json", "{}")
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def render_appearance(plan, db=None):
    value = validate_appearance(read_appearance(plan), db)
    if value.get("fontPresetId"):
        from .packaging_plan import find_item
        ref = value.pop("fontPresetId")
        from .packaging_plan import resolve_manifest_asset_path
        value["fontFamily"] = find_item("subtitle", ref, db)["fontname_guess"]
        value["fontFile"] = str(resolve_manifest_asset_path("subtitle", ref, db))
    return value


def appearance_defaults(plan, aspect_ratio="9:16", db=None):
    from .packaging_plan import find_item
    selected = next((i for i in plan.get("items", []) if i["slot"] == "subtitle" and not i["is_backup"]), None)
    preset = find_item("subtitle", selected["asset_ref"], db) if selected else {}
    preset = preset or {}
    value = {"fontSize": preset.get("size", 93), "primaryHex": preset.get("primary_hex", "#FFFFFF"),
        "highlightHex": preset.get("highlight_hex", "#FD9437"), "outlineHex": preset.get("outline_hex", "#000000"),
        "outlineWidth": preset.get("outline", 2), "backgroundHex": preset.get("back_hex", "#000000"),
        "backgroundOpacity": 0, "x": .5, "y": .9, "width": .9}
    height = 1080 if aspect_ratio == "16:9" else 1920
    margin = preset.get("margin_v", 144)
    if preset.get("ass_style"):
        fields = preset["ass_style"].split(",")
        value["fontSize"] = float(fields[2]); margin = float(fields[21])
        def hex_color(c):
            c = c.strip("&").removeprefix("H")[-6:]
            return "#" + c[4:6] + c[2:4] + c[:2]
        value["primaryHex"] = hex_color(fields[3]); value["outlineHex"] = hex_color(fields[5])
        value["outlineWidth"] = float(fields[16])
        value["backgroundHex"] = hex_color(fields[5] if fields[15] == "3" else fields[6])
        if fields[15] == "3":
            ass_color = fields[5].strip("&").removeprefix("H").zfill(8)
            value["backgroundOpacity"] = round(1 - int(ass_color[:2], 16) / 255, 4)
    value["y"] = round(1 - margin / height, 4)
    if selected: value["fontPresetId"] = selected["asset_ref"]
    return {**value, **read_appearance(plan)}
