"""Use the same display-only layout engine for render input and QA evidence."""
import json
from pathlib import Path
import shutil
import subprocess


def layout_ad_subtitles(segments: list[dict], preset: dict, ratio: str) -> list[dict]:
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node") or str(root / "node.exe")
    result = subprocess.run(
        [node, str(root / "src" / "ad-subtitle-layout.js")],
        input=json.dumps({"segments": segments, "preset": preset, "ratio": ratio}, ensure_ascii=False),
        capture_output=True, encoding="utf-8", check=False, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"AD_SUBTITLE_LAYOUT_FAILED: {result.stderr.strip()}")
    return json.loads(result.stdout)
