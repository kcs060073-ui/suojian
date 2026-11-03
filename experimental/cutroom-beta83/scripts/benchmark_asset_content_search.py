"""Deterministic local benchmark for current-window material search.

Uses a fresh synthetic catalog; never reads or writes production media.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from media_catalog.asset_index import list_asset_index
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999))]


def benchmark(count: int, repetitions: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="cutroom-content-search-") as temporary:
        root = Path(temporary)
        db = CatalogDatabase.open(root / "catalog.sqlite")
        try:
            db.migrate()
            media_root = root / "media"
            media_root.mkdir()
            register_library(db, "benchmark", "测试素材库", "local", str(media_root))
            with db.transaction() as connection:
                for index in range(count):
                    asset_id = f"benchmark-{index:05d}"
                    fingerprint = f"fingerprint-{index:05d}"
                    connection.execute(
                        "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                        "VALUES(?,'video',1000,?,datetime('now'))",
                        (asset_id, fingerprint),
                    )
                    connection.execute(
                        "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,file_identity,state) "
                        "VALUES(?,'benchmark',?,1,?,'available')",
                        (asset_id, f"{index:05d}.mp4", asset_id),
                    )
                    for window in range(2):
                        title = "厂房内部" if index % 8 == 0 and window == 1 else "园区道路"
                        connection.execute(
                            """INSERT INTO visual_observations(
                               asset_id,source_start_us,source_end_us,asset_fingerprint,
                               origin,method,method_version,evidence_json,title,description,
                               camera_motion)
                               VALUES(?,?,?,?, 'model','fixture','v1','[]',?,?, '未知')""",
                            (asset_id, window * 5_000_000, (window + 1) * 5_000_000,
                             fingerprint, title, title),
                        )
            samples = []
            result = None
            for _ in range(repetitions):
                start = time.perf_counter()
                result = list_asset_index(db, query="厂房", limit=24)
                samples.append((time.perf_counter() - start) * 1000)
            assert result is not None and result["total"] == (count + 7) // 8
            return {
                "assets": count, "observations": count * 2,
                "matches": result["total"], "samples": repetitions,
                "query": "厂房", "page_size": 24,
                "p50_ms": round(statistics.median(samples), 2),
                "p95_ms": round(percentile(samples, 0.95), 2),
                "max_ms": round(max(samples), 2),
                "failures": 0,
                "measurement": "local process; warm repeated query; no heavy job",
            }
        finally:
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=int, default=1280)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.assets, args.samples), ensure_ascii=False))
