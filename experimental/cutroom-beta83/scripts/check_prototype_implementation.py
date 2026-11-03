"""Guard the frozen design and implementation ledger against omissions."""
from pathlib import Path
import csv
import hashlib
import json
import sys

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import check_requirement_progress as progress_guard

ROOT = Path(__file__).resolve().parents[1]

def check(root=ROOT):
    errors = []
    manifest = json.loads((root / "docs/implementation/2026-09-24-design-baseline.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        path = root / entry["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            errors.append(f"Frozen design changed/missing: {entry['path']}")
    try:
        counts = progress_guard.check(root=root)
        requirements = sum(counts.values())
    except (OSError, ValueError) as error:
        errors.append(str(error))
        requirements = 0
    return errors, requirements, len(manifest["files"])

if __name__ == "__main__":
    errors, requirements, files = check()
    for error in errors:
        print(error)
    print(f"{requirements} requirements; {files} design files; {len(errors)} errors")
    raise SystemExit(bool(errors))
