"""Serve isolated ad and digital script-stage tasks for frozen-prototype comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.candidate_versions import record_candidate_qa
from media_catalog.digital_workflow import (
    _default_file_snapshot, _register_source_plan_connection, _source_catalog_plan,
)
from media_catalog.libraries import register_library
from media_catalog.provenance import canonical_digest, record_gate
from media_catalog.rework import reject_with_rework
from media_catalog.snapshots import record_qa_review_snapshot, record_render_dependency_snapshot
from media_catalog.workflow import seed_stage_states
from workbench.server import create_server


SCRIPTS = {
    "visual-script-ad": ("ad_mix", "示例园区招商广告", "为企业寻找一处合适的发展空间。\n这里有生产车间、仓储空间和员工生活配套。\n欢迎预约实地考察。"),
    "visual-script-digital": ("digital_human", "示例园区数字人口播", "为企业寻找一处合适的发展空间。\n园区提供生产车间、仓储空间和员工生活配套，满足企业生产经营需要。\n欢迎预约实地考察，了解更多园区信息。"),
}


def seed(root: Path, db_path: Path) -> None:
    media = db_path.parent / "source.mp4"
    if not media.exists() or media.stat().st_size < 100:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", "color=c=#dfe4da:s=180x320:r=1", "-t", "4", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(media),
        ], check=True)
    with CatalogDatabase.open(db_path) as db:
        db.migrate()
        for task_id, (route, title, script) in SCRIPTS.items():
            script_path = root / "runtime" / "workbench" / "tasks" / task_id / "inputs" / "final-script.txt"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(script, encoding="utf-8", newline="\n")
            brief = {"final_script": {
                "relative_path": "inputs/final-script.txt",
                "sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
            }}
            if route == "digital_human":
                brief["digital_human_source"] = {"path": str(media)}
            with db.transaction() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO tasks(task_id,title,task_type,status,brief_json) VALUES(?,?,?,?,?)",
                    (task_id, title, route, "created", json.dumps(brief, ensure_ascii=False)),
                )
            seed_stage_states(db, task_id)
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE task_stage_states SET status='completed' "
                    "WHERE task_id=? AND stage_key='create_preflight'", (task_id,),
                )
                connection.execute(
                    "UPDATE task_stage_states SET status='ready' "
                    "WHERE task_id=? AND stage_key='brief_lock'", (task_id,),
                )
        seed_ad_rework(db, root, media)
        seed_ad_pre_candidate_rework(db, root)
        seed_digital_rework(db, root)


def prepare_tool_links(root: Path, source_root: Path) -> None:
    """Expose already installed tools to the isolated fixture's real preflight."""
    targets = {
        ".venv": source_root / ".venv",
        "node_modules": source_root / "node_modules",
        "scripts": source_root / "scripts",
    }
    for name, target in targets.items():
        if not target.is_dir():
            raise RuntimeError(f"Fixture tool directory unavailable: {target}")
        alias = root / name
        if alias.exists():
            if alias.resolve() != target.resolve():
                raise RuntimeError(f"Fixture tool alias points elsewhere: {alias}")
            continue
        if os.name == "nt":
            env = os.environ.copy()
            env["CUTROOM_FIXTURE_ALIAS"] = str(alias)
            env["CUTROOM_FIXTURE_TARGET"] = str(target)
            subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; "
                "New-Item -ItemType Junction -Path $env:CUTROOM_FIXTURE_ALIAS "
                "-Target $env:CUTROOM_FIXTURE_TARGET | Out-Null",
            ], check=True, env=env)
        else:
            alias.symlink_to(target, target_is_directory=True)


def seed_ad_rework(db: CatalogDatabase, root: Path, video: Path) -> None:
    task_id = "visual-script-ad-rework"
    if db.connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        return
    script = "企业选址，先看空间，也要看配套。\n这次带你了解园区的生产空间与周边环境。\n欢迎预约实地查看。"
    task_root = root / "runtime" / "workbench" / "tasks" / task_id
    (task_root / "inputs").mkdir(parents=True, exist_ok=True)
    (task_root / "inputs" / "final-script.txt").write_text(
        script, encoding="utf-8", newline="\n",
    )
    ad_dir = task_root / "artifacts" / "ad"
    ad_dir.mkdir(parents=True, exist_ok=True)
    voice = ad_dir / "voice.mp3"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=2", "-codec:a", "libmp3lame", str(voice),
    ], check=True)
    script_sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
    (ad_dir / "recommendation.json").write_text(
        json.dumps({"script_sha256": script_sha}, ensure_ascii=False), encoding="utf-8",
    )
    brief = {"route": "ad_mix", "aspect_ratio": "9:16", "final_script": {
        "relative_path": "inputs/final-script.txt", "sha256": script_sha,
    }}
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO tasks(task_id,title,task_type,status,brief_json) VALUES(?,?,'ad_mix','brief_locked',?)",
            (task_id, "示例园区 · 招商短视频", json.dumps(brief, ensure_ascii=False)),
        )
        record_gate(connection, task_id, "brief", canonical_digest(brief), brief)
        connection.execute(
            "INSERT INTO scene_cards(card_id,task_id,idx,narrative_text) VALUES('ad-rework-card',?,0,'旧画面')",
            (task_id,),
        )
    render = record_render_dependency_snapshot(db, task_id, {
        "output_path": str(video), "dependency_digest": "visual-ad-rework",
        "dependency_snapshot": {"files": []}, "plan_path": str(root / "plan.json"),
        "ass_path": None, "packaging": {"plan_id": "visual"},
    })
    qa = {"report_id": "visual-ad-qa", "task_id": task_id,
          "video_path": str(video.resolve()), "status": "passed",
          "issue_count": 0, "blocking_count": 0, "issues": [],
          "video_spec": {"duration": 4.0}}
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO qa_reports(report_id,task_id,video_path,status,issue_count,blocking_count,report_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (qa["report_id"], task_id, qa["video_path"], "passed", 0, 0, json.dumps(qa)),
        )
    review = record_qa_review_snapshot(db, task_id, render, qa)
    candidate = record_candidate_qa(db, task_id, render, qa,
                                    qa_snapshot_id=review["qa_snapshot_id"])
    seed_stage_states(db, task_id)
    with db.transaction() as connection:
        connection.execute("UPDATE task_stage_states SET status='completed' WHERE task_id=?", (task_id,))
    reject_with_rework(db, task_id, candidate["candidate_id"], reviewed_by="Peter",
                       category="script", notes="文案需调整", idempotency_key="visual-ad-rework")


def seed_ad_pre_candidate_rework(db: CatalogDatabase, root: Path) -> None:
    """A confirmed script and rough cut with no rendered candidate or QA."""
    task_id = "visual-script-ad-pre-candidate"
    if db.connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        return
    script = "企业选址，先看空间，也要看配套。\n这次带你了解园区的生产空间与周边环境。\n欢迎预约实地查看。"
    task_root = root / "runtime" / "workbench" / "tasks" / task_id
    (task_root / "inputs").mkdir(parents=True, exist_ok=True)
    (task_root / "inputs" / "final-script.txt").write_text(
        script, encoding="utf-8", newline="\n",
    )
    ad_dir = task_root / "artifacts" / "ad"
    ad_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=2", "-codec:a", "libmp3lame",
        str(ad_dir / "voice.mp3"),
    ], check=True)
    script_sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
    (ad_dir / "recommendation.json").write_text(
        json.dumps({"script_sha256": script_sha}, ensure_ascii=False), encoding="utf-8",
    )
    brief = {"route": "ad_mix", "aspect_ratio": "9:16", "final_script": {
        "relative_path": "inputs/final-script.txt", "sha256": script_sha,
    }}
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO tasks(task_id,title,task_type,status,brief_json) VALUES(?,?,'ad_mix','brief_locked',?)",
            (task_id, "示例园区 · 招商短视频", json.dumps(brief, ensure_ascii=False)),
        )
        record_gate(connection, task_id, "brief", canonical_digest(brief), brief)
        connection.execute(
            "INSERT INTO scene_cards(card_id,task_id,idx,narrative_text) "
            "VALUES('ad-pre-candidate-card',?,0,'当前粗剪画面')", (task_id,),
        )
    seed_stage_states(db, task_id)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE task_stage_states SET status='completed' WHERE task_id=? "
            "AND stage_key IN ('create_preflight','brief_lock','material_sync','ai_storyboard')",
            (task_id,),
        )
        connection.execute(
            "UPDATE task_stage_states SET status='ready' WHERE task_id=? AND stage_key='rough_cut'",
            (task_id,),
        )


def seed_digital_rework(db: CatalogDatabase, root: Path) -> None:
    task_id = "visual-script-digital-rework"
    if db.connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        return
    old_source = root / "digital-old.mp4"
    new_source = root / "digital-new.mp4"
    for path, color, frequency in (
        (old_source, "#dfe4da", 330), (new_source, "#e1d9ce", 660),
    ):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s=180x320:r=25", "-f", "lavfi",
            "-i", f"sine=frequency={frequency}:duration=5", "-t", "5", "-shortest",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ], check=True)
    materials = root / "materials"
    materials.mkdir(exist_ok=True)
    register_library(db, "visual-primary", "示例主素材", "local", str(materials),
                     purpose="task_specific")
    script = "园区提供生产车间、仓储空间和员工生活配套。\n欢迎预约实地考察，了解更多园区信息。"
    task_root = root / "runtime" / "workbench" / "tasks" / task_id
    (task_root / "inputs").mkdir(parents=True, exist_ok=True)
    (task_root / "artifacts").mkdir(exist_ok=True)
    (task_root / "inputs" / "final-script.txt").write_text(
        script, encoding="utf-8", newline="\n",
    )
    (task_root / "artifacts" / "transcript.words.json").write_text(
        "{}", encoding="utf-8",
    )
    snapshot = _default_file_snapshot(old_source)
    source_plan = _source_catalog_plan(db, snapshot)
    brief = {
        "route": "digital_human", "subtype": "zhaoshang",
        "final_script": {
            "relative_path": "inputs/final-script.txt",
            "sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        },
        "digital_human_source": {"read_only": True, **snapshot,
                                 "asset_id": source_plan["asset_id"],
                                 "library_id": source_plan["library_id"]},
        "materials": {"primary_library_id": "visual-primary",
                      "selected_generic_ids": [], "include_default_generic": False},
    }
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO tasks(task_id,title,task_type,status,brief_json) "
            "VALUES(?,?,'digital_human','brief_locked',?)",
            (task_id, "示例园区 · 数字人口播", json.dumps(brief, ensure_ascii=False)),
        )
        _register_source_plan_connection(connection, source_plan, snapshot)
        connection.execute(
            "INSERT INTO task_assets(task_id,asset_id,role) VALUES(?,?,'digital_human')",
            (task_id, source_plan["asset_id"]),
        )
        record_gate(connection, task_id, "brief", canonical_digest(brief), brief)
        connection.execute(
            "INSERT INTO scene_cards(card_id,task_id,idx,narrative_text) "
            "VALUES('digital-rework-card',?,0,'旧画面')", (task_id,),
        )
    render = record_render_dependency_snapshot(db, task_id, {
        "output_path": str(old_source), "dependency_digest": "visual-digital-rework",
        "dependency_snapshot": {"files": []}, "plan_path": str(root / "plan.json"),
        "ass_path": None, "packaging": {"plan_id": "visual"},
    })
    qa = {"report_id": "visual-digital-qa", "task_id": task_id,
          "video_path": str(old_source.resolve()), "status": "passed",
          "issue_count": 0, "blocking_count": 0, "issues": [],
          "video_spec": {"duration": 5.0}}
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO qa_reports(report_id,task_id,video_path,status,issue_count,blocking_count,report_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (qa["report_id"], task_id, qa["video_path"], "passed", 0, 0, json.dumps(qa)),
        )
    review = record_qa_review_snapshot(db, task_id, render, qa)
    candidate = record_candidate_qa(db, task_id, render, qa,
                                    qa_snapshot_id=review["qa_snapshot_id"])
    seed_stage_states(db, task_id)
    with db.transaction() as connection:
        connection.execute("UPDATE task_stage_states SET status='completed' WHERE task_id=?", (task_id,))
    reject_with_rework(db, task_id, candidate["candidate_id"], reviewed_by="Peter",
                       category="script", notes="更换数字人口播底片",
                       idempotency_key="visual-digital-rework")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    source_root = Path(__file__).resolve().parents[1]
    validation = source_root / "out" / "validation" / "script-stage"
    validation.mkdir(parents=True, exist_ok=True)
    prepare_tool_links(validation, source_root)
    db_path = validation / "catalog.sqlite"
    seed(validation, db_path)
    server = create_server(
        db_path=db_path, port=args.port, project_root=validation,
        static_root=source_root / "web-ui" / "public", startup_key="script-fixture-only",
        worker_handlers={},
    )
    print(f"READY {server.runtime.origin}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
