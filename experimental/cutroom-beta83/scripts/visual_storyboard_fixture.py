"""Serve a reproducible ad storyboard for comparison with the frozen prototype.

Use ``--validation-dir out/validation/<new-name>`` for each new comparison.
The database and synthetic media stay under ``out/validation``. This script
never touches the user's production catalog.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.image_probe import probe_image
from media_catalog.libraries import register_library
from media_catalog.probe import probe_media
from media_catalog.scene_cards import add_scene_card
from media_catalog.script_segments import create_script_segment
from media_catalog.tasks import set_status
from media_catalog.workflow import complete_stage, project_gates, seed_stage_states
from workbench.server import create_server


GROUPS = (
    ("为企业寻找一处合适的发展空间。", 0, 3),
    ("园区提供生产车间、仓储空间和员工生活配套，满足企业生产经营需要。", 3, 12),
    ("欢迎预约实地考察，了解更多园区信息。", 12, 15),
)
SHOTS = (
    (0, 0, 3, "园区道路.mp4", "video", 0, 3),
    (1, 3, 6, "厂房内部.mp4", "video", 19, 22),
    (1, 6, 9, "仓库装卸区.mp4", "video", 10, 13),
    (1, 9, 12, "员工餐厅.jpg", "image", None, None),
    (2, 12, 15, "园区入口.mp4", "video", 3, 6),
)


def seed_asset_locations(db, db_path: Path) -> None:
    media_root = db_path.parent / "media"
    media_root.mkdir(exist_ok=True)
    sample_video = media_root / "园区道路.mp4"
    if not sample_video.exists() or sample_video.stat().st_size < 100:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=#dfe4da:s=96x60:r=1", "-t", "32", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", str(sample_video)],
            check=True,
        )
    sample_image = media_root / "员工餐厅.jpg"
    if not sample_image.exists() or sample_image.stat().st_size < 100:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=#dfe4da:s=96x60:r=1", "-frames:v", "1", str(sample_image)],
            check=True,
        )
    register_library(db, "visual-library", "示例园区实拍", "local", str(media_root))
    with db.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO task_source_scopes"
            "(scope_id,task_id,library_id,role,relative_path,source_mode,active,priority) "
            "VALUES('visual-task-scope','visual-ad','visual-library','primary','','primary',1,0)"
        )
        for index, (_, _, _, filename, _, _, _) in enumerate(SHOTS):
            if filename.endswith(".mp4") and filename != sample_video.name:
                shutil.copyfile(sample_video, media_root / filename)
            connection.execute(
                "INSERT OR IGNORE INTO asset_locations"
                "(asset_id,library_id,relative_path,mtime_ns,file_identity,state) "
                "VALUES(?,?,?,?,?,'available')",
                (f"visual-asset-{index}", "visual-library", filename, 1,
                 f"visual-file-{index}"),
            )
            source = media_root / filename
            probe = (probe_media(source, Path(shutil.which("ffprobe") or "ffprobe"))
                     if filename.endswith(".mp4") else probe_image(source))
            connection.execute(
                "INSERT OR REPLACE INTO analysis_runs"
                "(asset_id,stage,status,result_json,updated_at) "
                "VALUES(?,'technical_probe','completed',?,datetime('now'))",
                (f"visual-asset-{index}", json.dumps(probe)),
            )


def seed(db_path: Path) -> None:
    with CatalogDatabase.open(db_path) as db:
        db.migrate()
        existing = db.connection.execute(
            "SELECT brief_json FROM tasks WHERE task_id='visual-ad'"
        ).fetchone()
        if existing:
            brief = json.loads(existing["brief_json"] or "{}")
            card_count = db.connection.execute(
                "SELECT COUNT(*) FROM scene_cards WHERE task_id='visual-ad'"
            ).fetchone()[0]
            if brief.get("aspect_ratio") != "16:9" or card_count != len(SHOTS):
                raise ValueError(
                    "Existing storyboard fixture has a different state; "
                    "pass --validation-dir with a new isolated directory"
                )
            seed_asset_locations(db, db_path)
            if not project_gates(db, "visual-ad")["brief_locked"]:
                set_status(db, "visual-ad", "brief_locked")
            seed_stage_states(db, "visual-ad")
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE task_stage_states SET status='completed' "
                    "WHERE task_id='visual-ad' AND stage_key IN "
                    "('create_preflight','brief_lock','material_sync')"
                )
            complete_stage(db, "visual-ad", "ai_storyboard",
                           input_digest="visual-planning-input", details={"scene_card_count": 5})
            return
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,title,task_type,status,brief_json) VALUES(?,?,?,?,?)",
                ("visual-ad", "示例园区 · 招商短视频", "ad_mix", "brief_locked",
                 json.dumps({"aspect_ratio": "16:9",
                             "final_script": {"sha256": "visual-script-v1"}},
                            ensure_ascii=False)),
            )
            for index, (_, _, _, filename, media_kind, _, _) in enumerate(SHOTS):
                connection.execute(
                    "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                    "VALUES(?,?,?,?,datetime('now'))",
                    (f"visual-asset-{index}", media_kind, 10, f"visual-fingerprint-{index}"),
                )
                connection.execute(
                    "INSERT INTO task_assets(task_id,asset_id,role) VALUES(?,?,?)",
                    ("visual-ad", f"visual-asset-{index}", "broll"),
                )
        seed_asset_locations(db, db_path)
        segments = [
            create_script_segment(db, "visual-ad", "visual-script-v1", index, text,
                                  start * 1_000_000, end * 1_000_000)
            for index, (text, start, end) in enumerate(GROUPS)
        ]
        for index, (group, start, end, _, _, source_start, source_end) in enumerate(SHOTS):
            add_scene_card(
                db, "visual-ad", index, shot_id=f"visual-shot-{index}",
                script_segment_id=segments[group]["segment_id"],
                narrative_text=GROUPS[group][0], narrative_start=start,
                narrative_end=end, asset_id=f"visual-asset-{index}",
                mix_form="broll全屏", enter_tc=source_start, exit_tc=source_end,
                source_type="系统推荐",
            )
        set_status(db, "visual-ad", "brief_locked")
        seed_stage_states(db, "visual-ad")
        with db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' "
                "WHERE task_id='visual-ad' AND stage_key IN "
                "('create_preflight','brief_lock','material_sync')"
            )
        complete_stage(db, "visual-ad", "ai_storyboard",
                       input_digest="visual-planning-input", details={"scene_card_count": 5})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--validation-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    validation = (args.validation_dir or root / "out" / "validation" / "storyboard").resolve()
    if not validation.is_relative_to((root / "out" / "validation").resolve()):
        raise ValueError("Fixture directory must remain under project out/validation")
    validation.mkdir(parents=True, exist_ok=True)
    db_path = validation / "catalog.sqlite"
    seed(db_path)
    server = create_server(db_path=db_path, port=args.port, project_root=root,
                           startup_key="visual-fixture-only", worker_handlers={})
    print(f"READY {server.runtime.origin}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
