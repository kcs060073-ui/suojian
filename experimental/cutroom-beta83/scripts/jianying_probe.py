"""Create only synthetic probe media/recipe, never a claimed native project."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_catalog.jianying_probe import build_probe_recipe
from media_catalog.jianying_export import verify_jianying_assets
from media_catalog.jianying_compatibility import inspect_jianying_compatibility


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-version", required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    commands = []

    def run(command):
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        commands.append({"command": command, "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        (root / "commands.json").write_text(json.dumps(commands, ensure_ascii=False, indent=2), encoding="utf-8")
        result.check_returncode()
        return result.stdout

    for name, source in (("shot-a.mp4", "testsrc2=size=360x640:rate=30"),
                         ("shot-b.mp4", "smptebars=size=360x640:rate=30")):
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-f", "lavfi",
             "-i", source, "-t", "4", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / name)])
    for name, frequency in (("main.wav", 440), ("bgm.wav", 880)):
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-f", "lavfi",
             "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=4",
             "-c:a", "pcm_s16le", str(root / name)])
    probes = {}
    for name in ("shot-a.mp4", "shot-b.mp4", "main.wav", "bgm.wav"):
        probes[name] = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(root / name)]))
    recipe = build_probe_recipe(root)
    report = verify_jianying_assets(recipe, recipe["asset_manifest"], [root])
    compatibility = inspect_jianying_compatibility(recipe, args.target_version)
    for name, value in (("recipe.json", recipe), ("asset-verification.json", report),
                        ("compatibility.json", compatibility), ("ffprobe.json", probes)):
        (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n第一镜头：独立裁切测试\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n第二镜头：修改这行字幕\n", encoding="utf-8")
    print(json.dumps({"probe_inputs": str(root), "native_project_verified": False,
                      "compatibility": compatibility["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
