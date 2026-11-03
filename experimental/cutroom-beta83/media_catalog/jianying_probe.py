"""Synthetic probe inputs, deliberately not a native Jianying draft."""
from pathlib import Path

from .render_recipe import compile_ad_recipe


def build_probe_recipe(media_dir):
    root = Path(media_dir).resolve()
    recipe = compile_ad_recipe(
        task_id="jianying-compatibility-probe", ratio="9:16",
        segments=[{
            "i": index, "cardId": f"shot-{index}", "assetId": f"probe-{index}",
            "mediaKind": "video", "text": text, "start": index * 2,
            "end": (index + 1) * 2, "dur": 2,
            "src": str(root / name), "sourceStart": 1, "sourceEnd": 3,
        } for index, (name, text) in enumerate([
            ("shot-a.mp4", "第一镜头：独立裁切测试"),
            ("shot-b.mp4", "第二镜头：修改这行字幕"),
        ])], voice_path=root / "main.wav",
        subtitle_preset={"id": "probe-plain", "fontSize": 48},
        packaging_snapshot={"plan_id": "synthetic-probe"},
        packaging_fields={"bgm": {"src": str(root / "bgm.wav"), "gain": 0.15}},
        picture_lock_digest=None,
    )
    # Hard cut probe: transition compatibility is a separate, later gate.
    recipe["transitions"] = []
    return recipe
