"""Developer diagnostic: local media preflight, never a native export action."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_catalog.jianying_media import inspect_jianying_media


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--trusted-root", type=Path, action="append", required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("report already exists; choose a new evidence file")
    recipe = json.loads(args.recipe.read_text(encoding="utf-8-sig"))
    report = inspect_jianying_media(recipe, recipe["asset_manifest"], args.trusted_root,
                                   ffprobe_path=args.ffprobe, ffmpeg_path=args.ffmpeg)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    print(json.dumps({"report": str(args.report.resolve()), "status": report["status"],
                      "metadata_verified": report["metadata_verified"],
                      "decode_samples_verified": report["decode_samples_verified"],
                      "native_project_verified": False}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
