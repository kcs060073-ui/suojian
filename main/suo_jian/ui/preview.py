from __future__ import annotations

import json
import shutil
from pathlib import Path

from suo_jian.blueprint import enrich_blueprint


def write_preview(blueprint: dict, destination: Path, video: Path) -> Path:
    """Write a portable workbench with shared CSS/JS assets beside the blueprint."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    blueprint = enrich_blueprint(blueprint)
    asset_source = Path(__file__).with_name("static")
    asset_destination = destination.parent / "preview_assets"
    asset_destination.mkdir(exist_ok=True)
    for name in ("workbench.css", "workbench.js", "workspace.js", "suojian-icon.png"):
        shutil.copy2(asset_source / name, asset_destination / name)
    bootstrap = {
        "blueprint": blueprint,
        "videoUrl": video.resolve().as_uri(),
        "jsonUrl": "edit_blueprint.json",
        "stemUrls": {
            track.get("kind", "unknown"): Path(track["path"]).resolve().as_uri()
            for track in blueprint.get("audio_stems", {}).get("tracks", [])
            if track.get("path") and Path(track["path"]).exists()
        },
        "sourceAudioUrl": (destination.parent / "audio_stems" / "source.wav").resolve().as_uri()
        if (destination.parent / "audio_stems" / "source.wav").exists() else "",
    }
    data = json.dumps(bootstrap, ensure_ascii=False).replace("</", "<\\/")
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>溯剪 · 相似剪映草稿工作台</title><link rel="icon" href="preview_assets/suojian-icon.png"><link rel="stylesheet" href="preview_assets/workbench.css"></head>
<body><div id="workbench"></div><script>window.SUOJIAN_BOOTSTRAP={data};</script><script src="preview_assets/workspace.js"></script><script src="preview_assets/workbench.js"></script></body></html>'''
    destination.write_text(html, encoding="utf-8")
    return destination
