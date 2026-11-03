"""Measure read-only material pagination against an isolated synthetic catalog."""

import argparse
import cProfile
import hashlib
import json
import math
from pathlib import Path
import pstats
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_catalog.asset_index import list_asset_index
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--assets", type=int, default=23000)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()
    if args.assets < 48 or args.samples < 1:
        parser.error('assets must be at least 48 and samples must be positive')
    if not args.label or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in args.label):
        parser.error('label must contain only ASCII letters, digits, hyphens or underscores')
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    database = root / "catalog.sqlite"
    new = not database.exists()
    db = CatalogDatabase.open(database)
    if new:
        db.migrate()
        media = root / "media"
        media.mkdir()
        register_library(db, "bench", "Synthetic benchmark", "local", str(media))
        for index in range(args.assets):
            (media / f"clip-{index:05d}.mp4").write_bytes(b"presence fixture only")
        with db.transaction() as connection:
            connection.executemany(
                "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                "VALUES(?,'video',21,?,'benchmark')",
                [(f"asset-{i:05d}", f"fp-{i}") for i in range(args.assets)],
            )
            connection.executemany(
                "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,state) "
                "VALUES(?,'bench',?,1,'available')",
                [(f"asset-{i:05d}", f"clip-{i:05d}.mp4") for i in range(args.assets)],
            )
            connection.executemany(
                "INSERT INTO visual_observations(asset_id,source_start_us,source_end_us,"
                "asset_fingerprint,origin,method,method_version,evidence_json,title,"
                "description,camera_motion) VALUES(?,0,1000000,?,'human','benchmark',"
                "'1','{}','Synthetic observation','Synthetic description','unknown')",
                [(f"asset-{i:05d}", f"fp-{i}") for i in range(0, args.assets, 4)],
            )
    before = hashlib.sha256("\n".join(db.connection.iterdump()).encode()).hexdigest()
    db.connection.execute("PRAGMA query_only=ON")
    profiler = cProfile.Profile()
    profiler.runcall(list_asset_index, db, limit=24, offset=24)
    with (root / f"{args.label}-profile.txt").open("w", encoding="utf-8") as output:
        pstats.Stats(profiler, stream=output).sort_stats("cumulative").print_stats(25)
    samples = []
    for label, offset in [("first", 0), ("second", 24)]:
        for _ in range(args.samples):
            started = time.perf_counter()
            page = list_asset_index(db, limit=24, offset=offset)
            seconds = time.perf_counter() - started
            assert page["total"] == args.assets
            assert [row["assetId"] for row in page["items"]] == [
                f"asset-{i:05d}" for i in range(offset, offset + 24)
            ]
            samples.append({"page": label, "seconds": seconds})
    after = hashlib.sha256("\n".join(db.connection.iterdump()).encode()).hexdigest()
    assert before == after
    result = {
        "assets": args.assets, "samples": samples, "databaseUnchanged": before == after,
        "moduleSha256": hashlib.sha256(
            (Path(__file__).resolve().parents[1] / "media_catalog/asset_index.py")
            .read_text(encoding="utf-8").encode()
        ).hexdigest(),
        "stats": {},
        "limitation": "Synthetic presence files; no playable media or real device validation.",
    }
    for label in ["first", "second"]:
        values = sorted(x["seconds"] for x in samples if x["page"] == label)
        result["stats"][label] = {"p95Seconds": values[math.ceil(.95 * len(values)) - 1]}
    (root / f"{args.label}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["stats"]))
    db.close()


if __name__ == "__main__":
    main()
