"""Serve synthetic visual content search through the real workbench UI."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.visual_observations import record_visual_observation
from scripts.visual_storyboard_fixture import seed
from workbench.server import create_server


def seed_content_search(db_path: Path) -> None:
    seed(db_path)
    media_root = db_path.parent / "media"
    random_file = media_root / "A1847.mp4"
    if not random_file.exists():
        shutil.copyfile(media_root / "园区道路.mp4", random_file)
    db = CatalogDatabase.open(db_path)
    try:
        db.migrate()
        exists = db.connection.execute(
            "SELECT 1 FROM assets WHERE asset_id='search-random'"
        ).fetchone()
        if not exists:
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                    "VALUES('search-random','video',?,'search-fingerprint',datetime('now'))",
                    (random_file.stat().st_size,),
                )
                connection.execute(
                    "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,file_identity,state) "
                    "VALUES('search-random','visual-library','A1847.mp4',1,'search-random','available')"
                )
        if db.connection.execute(
            "SELECT 1 FROM visual_observations WHERE asset_id='search-random'"
        ).fetchone():
            return
        for start, end, title in ((0, 5_000_000, "园区道路"),
                                  (5_000_000, 10_000_000, "厂房内部")):
            record_visual_observation(
                db, "search-random", start, end, origin="model",
                method="multimodal-frames", method_version="v1",
                expected_asset_fingerprint="search-fingerprint",
                evidence_us=[start + 1_000_000, end - 1_000_000],
                title=title, description=title + "实拍画面",
                camera_motion="未知", uncertainty_reason="样本未核验运动",
            )
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    validation = root / "out" / "validation" / "content-search"
    validation.mkdir(parents=True, exist_ok=True)
    db_path = validation / "catalog.sqlite"
    seed_content_search(db_path)
    server = create_server(db_path=db_path, port=args.port, project_root=root,
                           startup_key="content-search-fixture-only", worker_handlers={})
    print(f"READY {server.runtime.origin}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
