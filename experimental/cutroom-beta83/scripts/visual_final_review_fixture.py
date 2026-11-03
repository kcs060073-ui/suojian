"""Serve isolated, decodable ad and digital candidates at frozen step 06."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.candidate_versions import record_candidate_qa
from media_catalog.snapshots import record_render_dependency_snapshot, record_render_review_candidate
from media_catalog.workbench_worker import enqueue_job
from media_catalog.workflow import WORKFLOW_STAGES, seed_stage_states
from workbench.server import create_server


TASKS = (
    ("visual-review-digital", "digital_human", "数字人口播 · 成片审查", "#dfe4da", 440),
    ("visual-review-ad", "ad_mix", "信息流广告 · 成片审查", "#e5ddd3", 520),
)


def seed(
    validation: Path,
    processing_stage: str | None = None,
    processing_status: str = "queued",
    blocked_ad: bool = False,
    history_ad: bool = False,
) -> Path:
    validation.mkdir(parents=True, exist_ok=True)
    db_path = validation / "catalog.sqlite"
    if db_path.exists():
        raise FileExistsError(f"隔离审片夹具目录已有数据库：{db_path}")
    with CatalogDatabase.open(db_path) as db:
        db.migrate()
        for task_id, route, title, color, frequency in TASKS:
            blocked = blocked_ad and route == "ad_mix"
            video = validation / f"{task_id}.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-n",
                "-f", "lavfi", "-i", f"color=c={color}:s=540x960:r=25",
                "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=8",
                "-t", "8", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(video),
            ], check=True)
            with db.transaction() as connection:
                connection.execute(
                    """INSERT INTO tasks(task_id,title,task_type,status,brief_json)
                    VALUES(?,?,?,'created',?)""",
                    (task_id, title, route, json.dumps({"route": route})),
                )
            issues = [
                {"check": "video_stream", "severity": "info", "ok": True,
                 "detail": "视频轨道正常"},
                {"check": "audio_stream", "severity": "info", "ok": True,
                 "detail": "音频轨道正常"},
                {"check": "subtitle_safe_area", "severity": "blocking" if blocked else "info",
                 "ok": not blocked,
                 "detail": "00:07.2—00:08.0 · 当前字幕可能被平台界面遮挡。"
                 if blocked else "字幕样式位于手机安全区",
                 **({"start_seconds": 7.2, "end_seconds": 8.0} if blocked else {})},
                {"check": "material_fingerprint", "severity": "info", "ok": True,
                 "detail": "素材内容一致"},
            ]
            report = {
                "report_id": f"qa-{task_id}", "task_id": task_id,
                "video_path": str(video.resolve()), "status": "blocked" if blocked else "passed",
                "issue_count": len(issues), "blocking_count": int(blocked), "issues": issues,
                "video_spec": {"duration": 8.0, "width": 540, "height": 960},
            }
            with db.transaction() as connection:
                connection.execute(
                    """INSERT INTO qa_reports(
                        report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (report["report_id"], task_id, report["video_path"],
                     report["status"], report["issue_count"], report["blocking_count"],
                     json.dumps(report, ensure_ascii=False)),
                )
            render_job = {
                "output_path": str(video.resolve()),
                "dependency_digest": f"visual-{task_id}",
                "dependency_snapshot": {"files": []},
                "plan_path": str(validation / f"{task_id}.plan.json"),
                "ass_path": None, "packaging": {"plan_id": f"visual-{task_id}"},
            }
            if blocked:
                rendered = record_render_dependency_snapshot(db, task_id, render_job)
                record_candidate_qa(db, task_id, rendered, report, eligible_for_review=False)
            else:
                record_render_review_candidate(db, task_id, render_job, report)
            if history_ad and route == "ad_mix":
                next_video = validation / f"{task_id}-v2.mp4"
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-n",
                    "-f", "lavfi", "-i", "color=c=#d2dacd:s=540x960:r=25",
                    "-f", "lavfi", "-i", "sine=frequency=650:duration=8",
                    "-t", "8", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(next_video),
                ], check=True)
                next_report = {
                    **report, "report_id": f"qa-{task_id}-v2",
                    "video_path": str(next_video.resolve()),
                }
                with db.transaction() as connection:
                    connection.execute(
                        """INSERT INTO qa_reports(
                            report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                        ) VALUES(?,?,?,?,?,?,?)""",
                        (next_report["report_id"], task_id, next_report["video_path"],
                         next_report["status"], next_report["issue_count"],
                         next_report["blocking_count"], json.dumps(next_report, ensure_ascii=False)),
                    )
                next_job = {**render_job, "output_path": str(next_video.resolve()),
                            "dependency_digest": f"visual-{task_id}-v2"}
                if blocked:
                    rendered = record_render_dependency_snapshot(db, task_id, next_job)
                    record_candidate_qa(db, task_id, rendered, next_report,
                                        eligible_for_review=False)
                else:
                    record_render_review_candidate(db, task_id, next_job, next_report)
            seed_stage_states(db, task_id)
            with db.transaction() as connection:
                connection.execute(
                    """UPDATE task_stage_states SET status='completed'
                    WHERE task_id=? AND stage_key IN (%s)"""
                    % ",".join("?" for _ in WORKFLOW_STAGES[:10]),
                    (task_id, *WORKFLOW_STAGES[:10]),
                )
                connection.execute(
                    """UPDATE task_stage_states SET status='ready'
                    WHERE task_id=? AND stage_key=?""",
                    (task_id, "auto_qa" if blocked else "full_review"),
                )
        if processing_stage:
            task_id = "visual-review-processing"
            with db.transaction() as connection:
                connection.execute(
                    """INSERT INTO tasks(task_id,title,task_type,status,brief_json)
                    VALUES(?,?,'ad_mix','created',?)""",
                    (task_id, "信息流广告 · 成片处理中", json.dumps({"route": "ad_mix"})),
                )
            seed_stage_states(db, task_id)
            completed = WORKFLOW_STAGES[:8 if processing_stage == "rendering" else 9]
            with db.transaction() as connection:
                connection.execute(
                    """UPDATE task_stage_states SET status='completed'
                    WHERE task_id=? AND stage_key IN (%s)"""
                    % ",".join("?" for _ in completed),
                    (task_id, *completed),
                )
            enqueue_job(
                db, job_id=f"visual-{processing_stage}", job_type="render_and_qa",
                task_id=task_id, payload={"fixture": processing_stage},
            )
            with db.transaction() as connection:
                connection.execute(
                    """UPDATE jobs SET status=?, phase_label=?, progress_current=2,
                    progress_total=5, error_summary=? WHERE job_id=?""",
                    (
                        processing_status,
                        "自动质检" if processing_stage == "checking" else "合成成片",
                        "RuntimeError: RENDER_OUTPUT_CHANGED_DURING_QA: local private path"
                        if processing_status == "failed" and processing_stage == "checking"
                        else "RuntimeError: PROCESS_TIMEOUT: local private path"
                        if processing_status == "failed" else None,
                        f"visual-{processing_stage}",
                    ),
                )
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--validation-dir", type=Path, required=True)
    parser.add_argument("--processing-stage", choices=("rendering", "checking"))
    parser.add_argument("--processing-status", choices=("queued", "failed"), default="queued")
    parser.add_argument("--blocked-ad", action="store_true")
    parser.add_argument("--history-ad", action="store_true")
    args = parser.parse_args()
    validation = args.validation_dir.resolve()
    root = Path(__file__).resolve().parents[1]
    if root not in validation.parents:
        raise ValueError("审片夹具必须保存在当前项目下")
    if args.processing_status != "queued" and not args.processing_stage:
        parser.error("--processing-status requires --processing-stage")
    db_path = seed(validation, args.processing_stage, args.processing_status,
                   args.blocked_ad, args.history_ad)
    server = create_server(
        db_path=db_path, port=args.port, project_root=root,
        startup_key="visual-review-fixture", worker_handlers={},
    )
    print(f"READY {server.runtime.origin} {validation}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
