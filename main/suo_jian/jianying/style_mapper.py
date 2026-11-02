from __future__ import annotations


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    cleaned = (value or "#FFFFFF").lstrip("#")
    if len(cleaned) != 6:
        cleaned = "FFFFFF"
    return tuple(int(cleaned[index:index + 2], 16) / 255 for index in (0, 2, 4))


def resolve_style(styles: dict, style_id: str) -> dict:
    style = styles.get(style_id) or styles.get("subtitle_style_01") or {}
    return {
        "font_size": float(style.get("font_size", 52)),
        "bold": int(style.get("font_weight", 900)) >= 700,
        "fill": hex_to_rgb(style.get("fill", "#FFD800")),
        "stroke": hex_to_rgb(style.get("stroke", {}).get("color", "#111111")),
        "stroke_width": float(style.get("stroke", {}).get("width", 5)),
        "shadow": bool(style.get("shadow", {}).get("enabled", True)),
        "x": float(style.get("position", {}).get("x", 0.5)),
        "y": float(style.get("position", {}).get("y", 0.66)),
    }
