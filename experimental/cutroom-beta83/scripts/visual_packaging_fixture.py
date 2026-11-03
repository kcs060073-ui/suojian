"""Serve an isolated five-shot ad task at the packaging step for UI review."""

from __future__ import annotations

import argparse
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.packaging_plan import propose
from media_catalog.scene_cards import set_picture_lock
from media_catalog.script_segments import storyboard_digest
from media_catalog.storyboard_review import accept_storyboard
from media_catalog.workflow import project_gates
from scripts.visual_storyboard_fixture import seed
from workbench.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--validation-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    validation = args.validation_dir or root / "out" / "validation" / "packaging"
    validation.mkdir(parents=True, exist_ok=True)
    db_path = validation / "catalog.sqlite"
    seed(db_path)
    with CatalogDatabase.open(db_path) as db:
        db.migrate()
        accept_storyboard(db, "visual-ad", storyboard_digest(db, "visual-ad"))
        if not project_gates(db, "visual-ad")["picture_lock"]:
            set_picture_lock(db, "visual-ad", confirm_all=True)
        if not db.connection.execute(
            "SELECT 1 FROM packaging_plans WHERE task_id='visual-ad' LIMIT 1"
        ).fetchone():
            propose(db, "visual-ad", "信息流广告")
    server = create_server(db_path=db_path, port=args.port, project_root=root,
                           startup_key="visual-packaging-fixture", worker_handlers={})
    print(f"READY {server.runtime.origin}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
