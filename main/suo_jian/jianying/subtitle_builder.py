from __future__ import annotations

from .style_mapper import resolve_style
from .text_geometry import native_font_size, native_border_width


def add_subtitles(script, draft, draft_blueprint: dict) -> int:
    text_track = script.append_track(draft.TrackSpec(draft.TrackType.text, "溯剪-可编辑字幕"))
    count = 0
    for item in draft_blueprint.get("timeline", []):
        time = item["time"]
        subtitle = item["subtitle"]
        if not subtitle.get("text") or float(time["duration"]) <= 0:
            continue
        style = resolve_style(draft_blueprint.get("styles", {}), subtitle.get("style_id", "subtitle_style_01"))
        transform_y = max(-1.0, min(1.0, (0.5 - style["y"]) * 2))
        segment = draft.TextSegment(
            subtitle["text"],
            draft.trange(f"{float(time['start']):.3f}s", f"{float(time['duration']):.3f}s"),
            style=draft.TextStyle(size=native_font_size(style["font_size"], script.width, script.height), bold=style["bold"], color=style["fill"], align=1),
            border=draft.TextBorder(color=style["stroke"], width=native_border_width(style["stroke_width"], style["font_size"])),
            shadow=draft.TextShadow(color=(0.0, 0.0, 0.0), alpha=0.65, diffuse=8.0, distance=3.0) if style["shadow"] else None,
            clip_settings=draft.ClipSettings(transform_x=(style["x"] - 0.5) * 2, transform_y=transform_y),
        )
        script.add_segment(segment, text_track)
        count += 1
    return count
