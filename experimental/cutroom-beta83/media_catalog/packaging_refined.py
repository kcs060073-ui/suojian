"""Shared versioned content metadata; legacy instances have an empty value."""
import importlib.util
from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=1)
def renderer():
    path = Path(__file__).resolve().parents[1] / "scripts/refined_layers.py"
    spec = importlib.util.spec_from_file_location("cutroom_refined_layers", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

def validate_content_layout(layout, text):
    if not isinstance(layout, dict): raise ValueError("PACKAGING_LAYOUT_INVALID: 分层必须是对象")
    for key in ("version", "text", "label", "kicker", "body"):
        if key in layout and (not isinstance(layout[key], str) or len(layout[key]) > 300):
            raise ValueError("PACKAGING_LAYOUT_INVALID: 分层文字无效")
    source = layout.get("source")
    if source is not None:
        if (not isinstance(source, dict) or set(source) != {"source_ref", "script_start", "script_end"}
            or not isinstance(source["source_ref"], str) or len(source["source_ref"]) > 100
            or any(type(source[k]) is not int for k in ("script_start", "script_end"))
            or not 0 <= source["script_start"] < source["script_end"] <= 20000):
            raise ValueError("PACKAGING_LAYOUT_INVALID: 分层来源范围无效")
    renderer().valid_layout(layout, text)
    allowed = {"version", "text", "label", "kicker", "body", "source"}
    if set(layout) - allowed: raise ValueError("PACKAGING_LAYOUT_INVALID: 不支持的分层字段")
    return layout
