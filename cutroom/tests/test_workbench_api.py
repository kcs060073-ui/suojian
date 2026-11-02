import http.client
import inspect
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog.digital_workflow import DigitalTaskPreflightError
from media_catalog.libraries import register_library
from media_catalog.scene_cards import add_scene_card, get_scene_card, set_picture_lock
from media_catalog.task_sources import set_task_sources
from media_catalog.tasks import create_task, set_status
from media_catalog.workbench_worker import enqueue_job

from tests.test_workbench_server import FakePicker


class WorkbenchApiTests(unittest.TestCase):
    def setUp(self):
        from workbench.server import create_server

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.static = self.root / "public"
        self.static.mkdir()
        (self.static / "index.html").write_text("workbench", encoding="utf-8")
        self.db_path = self.root / "catalog.sqlite"
        self.server = create_server(
            db_path=self.db_path,
            port=0,
            static_root=self.static,
            project_root=self.root,
            picker=FakePicker(),
            startup_key="api-key",
            worker_handlers={},
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, headers, _ = self.request("GET", "/auth/bootstrap?key=api-key")
        self.assertEqual(status, 303)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method, path, payload=None):
        host, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {}
        body = None
        if hasattr(self, "cookie"):
            headers["Cookie"] = self.cookie
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers.update({"Origin": self.origin, "Content-Type": "application/json"})
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = (
            response.status,
            dict(response.getheaders()),
            json.loads(raw) if raw else None,
        )
        connection.close()
        return result

    def seed_task(self, task_id="task-1"):
        with CatalogDatabase.open(self.db_path) as db:
            create_task(
                db,
                task_id,
                "新疆数字人 v2",
                "digital_human",
                brief={"final_script": {"sha256": "abc"}},
            )

    def test_asset_index_endpoint_returns_task_scoped_page(self):
        self.seed_task()
        media_root = self.root / "task-media"
        media_root.mkdir()
        with CatalogDatabase.open(self.db_path) as db:
            register_library(
                db,
                "task-media",
                "本次项目",
                "local",
                str(media_root),
                purpose="task_specific",
            )
            with db.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO assets(
                      asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                    ) VALUES('asset-1','video',10,'quick-1',datetime('now'))
                    """
                )
                connection.execute(
                    """
                    INSERT INTO asset_locations(
                      asset_id,library_id,relative_path,mtime_ns,file_identity,state
                    ) VALUES('asset-1','task-media','door.mp4',1,'file-1','available')
                    """
                )
            set_task_sources(
                db,
                "task-1",
                primary_library_id="task-media",
                selected_generic_ids=[],
                include_default_generic=False,
            )

        status, _, payload = self.request(
            "GET",
            "/api/assets?taskId=task-1&mediaKind=video&limit=1&offset=0",
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["assetId"], "asset-1")
        self.assertEqual(payload["items"][0]["authorizationStatus"], "unknown")

    def test_asset_index_endpoint_rejects_malformed_query(self):
        for query in ("limit=many", "authorization=maybe"):
            with self.subTest(query=query):
                status, _, payload = self.request("GET", f"/api/assets?{query}")

                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "ASSET_INDEX_QUERY_INVALID")

    def test_locked_scene_material_authorization_can_be_explicitly_attested(self):
        self.seed_task()
        with CatalogDatabase.open(self.db_path) as db:
            set_status(db, "task-1", "brief_locked")
            with db.transaction() as connection:
                for asset_id in ("shot-a", "shot-b"):
                    connection.execute(
                        "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                        "VALUES(?,'video',10,?,datetime('now'))",
                        (asset_id, "quick-" + asset_id),
                    )
            add_scene_card(
                db, "task-1", 0, narrative_text="一", narrative_start=0,
                narrative_end=1, asset_id="shot-a", mix_form="broll全屏",
                enter_tc=0, exit_tc=1,
            )
            add_scene_card(
                db, "task-1", 1, narrative_text="二", narrative_start=1,
                narrative_end=2, asset_id="shot-b", mix_form="broll全屏",
                enter_tc=0, exit_tc=1,
            )
            set_picture_lock(db, "task-1", confirm_all=True)

        status, _, attestation = self.request(
            "POST",
            "/api/tasks/task-1/material-authorization",
            {"confirmedBy": "本机同事"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(attestation["approved"])
        self.assertEqual(attestation["assetIds"], ["shot-a", "shot-b"])
        self.assertEqual(attestation["confirmedBy"], "本机同事")

        status, _, detail = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["materialAuthorization"], attestation)

        status, _, reusable = self.request(
            "POST",
            "/api/tasks/task-1/material-authorization",
            {"confirmedBy": "本机同事", "basis": "self_produced"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(reusable["approved"])
        with CatalogDatabase.open(self.db_path) as db:
            rows = db.connection.execute(
                "SELECT asset_id FROM self_produced_asset_authorizations ORDER BY asset_id"
            ).fetchall()
        self.assertEqual([row["asset_id"] for row in rows], ["shot-a", "shot-b"])

        status, _, _ = self.request(
            "POST",
            "/api/tasks/task-1/material-authorization",
            {"confirmedBy": "本机同事", "basis": "unrecognized"},
        )
        self.assertEqual(status, 400)

    def test_storyboard_feedback_rerank_and_apply_are_separate_actions(self):
        self.seed_task()
        media_root = self.root / "storyboard-media"
        media_root.mkdir()
        with CatalogDatabase.open(self.db_path) as db:
            register_library(
                db,
                "storyboard-media",
                "本次项目",
                "local",
                str(media_root),
                purpose="task_specific",
            )
            with db.transaction() as connection:
                for asset_id, relative_path in (
                    ("night", "夜间园区.mp4"),
                    ("day", "白天厂房.mp4"),
                ):
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                        ) VALUES(?, 'video', 10, ?, datetime('now'))
                        """,
                        (asset_id, "quick-" + asset_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO asset_locations(
                          asset_id,library_id,relative_path,mtime_ns,file_identity,state
                        ) VALUES(?, 'storyboard-media', ?, 1, ?, 'available')
                        """,
                        (asset_id, relative_path, asset_id),
                    )
            set_task_sources(
                db,
                "task-1",
                primary_library_id="storyboard-media",
                selected_generic_ids=[],
                include_default_generic=False,
            )
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO task_assets(task_id,asset_id,role) VALUES('task-1','night','broll')"
                )
            add_scene_card(
                db,
                "task-1",
                1,
                narrative_text="展示厂房空间",
                narrative_start=0,
                narrative_end=4,
                asset_id="night",
                mix_form="broll全屏",
                enter_tc=0,
                exit_tc=4,
                source_type="系统推荐",
                system_suggestion="夜景",
            )

        status, _, rejected = self.request(
            "POST",
            "/api/tasks/task-1/storyboard/feedback",
            {"cardId": "task-1#1", "decision": "rejected", "instruction": "不要夜景"},
        )
        self.assertEqual((status, rejected["decision"]), (200, "rejected"))
        with CatalogDatabase.open(self.db_path) as db:
            self.assertEqual(get_scene_card(db, "task-1#1")["asset_id"], "night")

        status, _, reranked = self.request(
            "POST",
            "/api/tasks/task-1/storyboard/rerank",
            {"cardId": "task-1#1", "instruction": "改用白天厂房"},
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["assetId"] for item in reranked["candidates"]], ["day"])

        status, _, applied = self.request(
            "POST",
            "/api/tasks/task-1/storyboard/apply-candidate",
            {
                "cardId": "task-1#1",
                "feedbackId": reranked["feedbackId"],
                "assetId": "day",
            },
        )
        self.assertEqual((status, applied["feedback"]["decision"]), (200, "applied"))
        with CatalogDatabase.open(self.db_path) as db:
            self.assertEqual(get_scene_card(db, "task-1#1")["asset_id"], "day")

    def test_job_pause_endpoint_projects_precise_controls_and_timing_fields(self):
        self.seed_task()
        with CatalogDatabase.open(self.db_path) as db:
            enqueue_job(
                db,
                job_id="pause-me",
                job_type="render",
                task_id="task-1",
                payload={"task_id": "task-1"},
            )

        status, _, paused = self.request("POST", "/api/jobs/pause-me/pause", {})

        self.assertEqual((status, paused["status"]), (200, "paused"))
        self.assertFalse(paused["canPause"])
        self.assertTrue(paused["canResume"])
        self.assertFalse(paused["pauseRequested"])
        self.assertEqual(paused["estimateStatus"], "unknown")
        self.assertIsNone(paused["estimatedStartAt"])
        self.assertIn("observedAt", paused)

        status, _, resumed = self.request("POST", "/api/jobs/pause-me/resume", {})
        self.assertEqual((status, resumed["status"]), (200, "queued"))
        self.assertTrue(resumed["canPause"])

    def seed_review_candidate(self, task_id="task-1", suffix="v1"):
        self.seed_task(task_id)
        return self.record_review_candidate(task_id=task_id, suffix=suffix)

    def record_review_candidate(self, task_id="task-1", suffix="v1"):
        from media_catalog.snapshots import record_render_review_candidate
        from media_catalog.workflow import seed_stage_states

        video = self.root / f"{task_id}-{suffix}.mp4"
        video.write_bytes(f"video-{suffix}".encode("utf-8"))
        report = {
            "report_id": f"qa-{task_id}-{suffix}",
            "task_id": task_id,
            "video_path": str(video.resolve()),
            "status": "passed",
            "issue_count": 1,
            "blocking_count": 0,
            "issues": [
                {"severity": "warning", "check": "caption", "detail": "请复核字幕"}
            ],
            "video_spec": {"duration": 12.0},
        }
        with CatalogDatabase.open(self.db_path) as db:
            with db.transaction() as connection:
                connection.execute(
                    """INSERT INTO qa_reports(
                      report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        report["report_id"], task_id, report["video_path"], "passed",
                        1, 0, json.dumps(report, ensure_ascii=False),
                    ),
                )
            candidate = record_render_review_candidate(
                db,
                task_id,
                {
                    "output_path": str(video.resolve()),
                    "dependency_digest": f"digest-{suffix}",
                    "dependency_snapshot": {"files": []},
                    "plan_path": str(self.root / f"{suffix}.plan.json"),
                    "ass_path": None,
                    "packaging": {"plan_id": f"plan-{suffix}"},
                },
                report,
            )
            seed_stage_states(db, task_id)
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE task_stage_states SET status='completed' WHERE task_id=?",
                    (task_id,),
                )
        return candidate

    def test_review_rework_publish_closure_keeps_candidate_evidence_isolated(self):
        from media_catalog.full_review import (
            approve_after_full_watch,
            latest_watch_session,
        )
        from media_catalog.publish import build_publish_package, list_publish
        from tests.review_helpers import complete_watch_session

        first = self.seed_review_candidate()
        with CatalogDatabase.open(self.db_path) as db:
            first_watch = complete_watch_session(
                db,
                "task-1",
                first["qa_snapshot_id"],
                first["qa_report_id"],
            )
            approve_after_full_watch(
                db,
                "task-1",
                first["qa_snapshot_id"],
                first["qa_report_id"],
                first_watch,
                "自动化测试同事",
            )

            def fake_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"jpeg")
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                patch("media_catalog.publish.DELIVER_ROOT", self.root / "deliverables"),
                patch("media_catalog.publish.find_ffmpeg", return_value=Path("ffmpeg")),
                patch("media_catalog.publish.subprocess.run", side_effect=fake_ffmpeg),
            ):
                first_package = build_publish_package(
                    db,
                    "task-1",
                    "视频号",
                    video_path=first["output_path"],
                    qa_report={"report_id": first["qa_report_id"]},
                )

        status, _, saved = self.request(
            "POST",
            "/api/tasks/task-1/review/rework",
            {
                "candidateId": first["candidate_id"],
                "category": "subtitle",
                "notes": "字幕时间需调整",
                "reviewedBy": "自动化测试同事",
                "idempotencyKey": "closure-reject-v1",
            },
        )
        self.assertEqual(status, 200)
        rework_id = saved["rework"]["reworkId"]
        status, _, executed = self.request(
            "POST",
            f"/api/tasks/task-1/review/rework/{rework_id}/execute",
            {"idempotencyKey": "closure-execute-v1"},
        )
        self.assertEqual((status, executed["status"]), (200, "applied"))

        second = self.record_review_candidate(suffix="v2")
        with CatalogDatabase.open(self.db_path) as db:
            self.assertEqual(second["source_candidate_id"], first["candidate_id"])
            self.assertEqual(second["rework_request_id"], rework_id)
            self.assertIsNone(
                latest_watch_session(
                    db,
                    "task-1",
                    second["qa_snapshot_id"],
                    second["qa_report_id"],
                )
            )
            packages = {item["record_id"]: item for item in list_publish(db, "task-1")}
            self.assertEqual(
                packages[first_package["record_id"]]["package_status"], "expired"
            )
            with self.assertRaisesRegex(RuntimeError, "FULL_WATCH_REQUIRED"):
                approve_after_full_watch(
                    db,
                    "task-1",
                    second["qa_snapshot_id"],
                    second["qa_report_id"],
                    first_watch,
                    "自动化测试同事",
                )

        status, _, detail = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["currentCandidate"]["candidateId"], second["candidate_id"])
        self.assertEqual(detail["currentCandidate"]["sourceCandidateId"], first["candidate_id"])
        self.assertEqual(detail["reworkRequests"][0]["resultCandidateId"], second["candidate_id"])

    def test_watch_start_requires_the_submitted_current_candidate_id(self):
        candidate = self.seed_review_candidate()
        status, _, body = self.request(
            "POST",
            "/api/tasks/task-1/watch/start",
            {
                "candidateId": "candidate-stale",
                "snapshotId": candidate["qa_snapshot_id"],
                "qaReportId": candidate["qa_report_id"],
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "CANDIDATE_BINDING_MISMATCH")

    def test_structurally_invalid_historical_qa_is_projected_unavailable(self):
        candidate = self.seed_review_candidate()
        with CatalogDatabase.open(self.db_path) as db:
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE qa_reports SET report_json=? WHERE report_id=?",
                    ('{"issues":null,"blocking_count":"unknown"}', candidate["qa_report_id"]),
                )

        status, _, detail = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["candidateVersions"][0]["qa"]["status"], "unavailable")
        self.assertEqual(detail["candidateVersions"][0]["qa"]["issues"], [])

    def test_task_detail_projects_candidate_history_and_qa_without_local_paths(self):
        candidate = self.seed_review_candidate()
        status, _, detail = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["currentCandidate"]["candidateId"], candidate["candidate_id"])
        self.assertEqual(detail["currentCandidate"]["versionLabel"], "candidate-v1")
        self.assertEqual(detail["currentCandidate"]["duration"], 12.0)
        self.assertEqual(detail["candidateVersions"][0]["qa"]["blockingCount"], 0)
        self.assertEqual(detail["candidateVersions"][0]["qa"]["warningCount"], 1)
        self.assertNotIn("outputPath", detail["candidateVersions"][0])
        self.assertNotIn(str(self.root), json.dumps(detail, ensure_ascii=False))

        with CatalogDatabase.open(self.db_path) as db:
            db.connection.execute(
                "UPDATE qa_reports SET report_json='not-json' WHERE report_id=?",
                (candidate["qa_report_id"],),
            )
            db.connection.commit()
        status, _, malformed = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 200)
        self.assertEqual(malformed["currentCandidate"]["qa"]["status"], "unavailable")

    def test_rework_preview_save_and_execute_are_distinct(self):
        candidate = self.seed_review_candidate()
        status, _, preview = self.request(
            "POST", "/api/tasks/task-1/review/rework/preview", {"category": "subtitle"}
        )
        self.assertEqual((status, preview["targetStage"]), (200, "packaging_confirm"))
        status, _, saved = self.request(
            "POST",
            "/api/tasks/task-1/review/rework",
            {
                "candidateId": candidate["candidate_id"],
                "category": "subtitle",
                "notes": "字幕慢半拍",
                "positionSeconds": 12.5,
                "reviewedBy": "Peter",
                "idempotencyKey": "reject-1",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["rework"]["status"], "pending_confirmation")
        with CatalogDatabase.open(self.db_path) as db:
            self.assertEqual(
                db.connection.execute(
                    "SELECT status FROM task_stage_states WHERE task_id='task-1' AND stage_key='fine_render'"
                ).fetchone()[0],
                "completed",
            )
        rework_id = saved["rework"]["reworkId"]
        status, _, executed = self.request(
            "POST",
            f"/api/tasks/task-1/review/rework/{rework_id}/execute",
            {"idempotencyKey": "run-1"},
        )
        self.assertEqual((status, executed["status"]), (200, "applied"))

    def test_candidate_mismatch_cannot_approve_or_publish(self):
        candidate = self.seed_review_candidate()
        approve_status, _, approve = self.request(
            "POST",
            "/api/tasks/task-1/review/approve",
            {
                "candidateId": "candidate-stale",
                "snapshotId": candidate["qa_snapshot_id"],
                "qaReportId": candidate["qa_report_id"],
                "watchSessionId": "watch-stale",
                "reviewedBy": "Peter",
            },
        )
        self.assertEqual(
            (approve_status, approve["error"]["code"]),
            (409, "CANDIDATE_BINDING_MISMATCH"),
        )
        publish_status, _, publish = self.request(
            "POST",
            "/api/tasks/task-1/publish",
            {"candidateId": "candidate-stale", "platform": "视频号"},
        )
        self.assertEqual(
            (publish_status, publish["error"]["code"]),
            (409, "CANDIDATE_BINDING_MISMATCH"),
        )

    def seed_rough_media_task(self):
        self.rough_library = self.root / "rough-media"
        self.rough_library.mkdir()
        self.rough_image = self.rough_library / "image-a.jpg"
        self.rough_candidate = self.rough_library / "candidate-a.jpg"
        self.rough_image.write_bytes(b"image-a")
        self.rough_candidate.write_bytes(b"candidate-a")
        with CatalogDatabase.open(self.db_path) as db:
            register_library(
                db,
                "rough-media",
                "粗剪素材",
                "local",
                str(self.rough_library),
                purpose="task_specific",
            )
            create_task(
                db,
                "task-a",
                "真实粗剪任务",
                "digital_human",
                brief={"final_script": {"sha256": "rough-media-script"}},
            )
            with db.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO assets(
                      asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                    ) VALUES('image-a','image',?,'fp-image-a',datetime('now'))
                    """,
                    (self.rough_image.stat().st_size,),
                )
                connection.execute(
                    """
                    INSERT INTO assets(
                      asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                    ) VALUES('candidate-a','image',?,'fp-candidate-a',datetime('now'))
                    """,
                    (self.rough_candidate.stat().st_size,),
                )
                connection.execute(
                    """
                    INSERT INTO asset_locations(
                      asset_id,library_id,relative_path,mtime_ns,state
                    ) VALUES('image-a','rough-media',?,?, 'available')
                    """,
                    (self.rough_image.name, self.rough_image.stat().st_mtime_ns),
                )
                connection.execute(
                    """
                    INSERT INTO asset_locations(
                      asset_id,library_id,relative_path,mtime_ns,state
                    ) VALUES('candidate-a','rough-media',?,?, 'available')
                    """,
                    (
                        self.rough_candidate.name,
                        self.rough_candidate.stat().st_mtime_ns,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id,task_id,library_id,role,relative_path,
                      source_mode,active,priority
                    ) VALUES(
                      'task-a-primary','task-a','rough-media','primary','',
                      'primary',1,0
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO scene_cards(
                      card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                      asset_id,mix_form,enter_tc,exit_tc,audio_policy_draft,source_type
                    ) VALUES(
                      'task-a-image','task-a',0,'真实画面',0,2,
                      'image-a','信息卡',0,0,'mute','用户指定'
                    )
                    """
                )

    def test_bootstrap_list_and_detail_share_real_sqlite_task_projection(self):
        self.seed_task()
        status, _, bootstrap = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["tasks"][0]["taskId"], "task-1")
        self.assertEqual(
            bootstrap["tasks"][0]["orchestrationMode"],
            "standalone",
        )
        self.assertEqual(len(bootstrap["workflowStages"]), 12)
        self.assertEqual(bootstrap["routes"][0]["availability"], "available")
        self.assertEqual(
            {item["key"]: item["availability"] for item in bootstrap["routes"]},
            {
                "digital_human": "available",
                "park_video": "coming_later",
                "ad_mix": "available",
            },
        )

        status, _, tasks = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(tasks["tasks"], bootstrap["tasks"])

        status, _, detail = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["task"]["task_id"], "task-1")
        self.assertEqual(detail["workflow"]["task_id"], "task-1")
        self.assertIn("sources", detail)
        self.assertIn("sceneCards", detail)
        self.assertIn("eligibleAssets", detail)
        self.assertIn("packagingPlans", detail)
        self.assertIn("watchSession", detail)
        self.assertIn("jobs", detail)

    def test_task_detail_projects_media_urls_without_absolute_paths(self):
        self.seed_rough_media_task()

        status, _, detail = self.request("GET", "/api/tasks/task-a")

        self.assertEqual(status, 200)
        scene = detail["sceneCards"][0]
        self.assertEqual(scene["media_kind"], "image")
        self.assertTrue(scene["preview_version"])
        self.assertEqual(
            scene["preview_url"],
            "/api/tasks/task-a/rough-media/image-a/preview",
        )
        self.assertEqual(
            scene["thumbnail_url"],
            "/api/tasks/task-a/rough-media/image-a/thumbnail",
        )
        self.assertNotIn(str(self.rough_library), json.dumps(scene, ensure_ascii=False))
        candidate = next(
            item
            for item in detail["eligibleAssets"]
            if item["asset_id"] == "candidate-a"
        )
        self.assertEqual(
            candidate["preview_url"],
            "/api/tasks/task-a/rough-media/candidate-a/preview",
        )
        self.assertTrue(candidate["preview_available"])

    def test_task_detail_resolves_segment_and_digital_anchor_source_windows(self):
        self.seed_rough_media_task()
        segment_video = self.rough_library / "segment.mp4"
        digital_video = self.rough_library / "digital.mp4"
        segment_video.write_bytes(b"segment-video")
        digital_video.write_bytes(b"digital-video")
        with CatalogDatabase.open(self.db_path) as db:
            with db.transaction() as connection:
                for asset_id, path in (("segment-video", segment_video), ("digital-video", digital_video)):
                    connection.execute(
                        "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                        "VALUES(?, 'video', ?, ?, datetime('now'))",
                        (asset_id, path.stat().st_size, f"fp-{asset_id}"),
                    )
                    connection.execute(
                        "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,state) "
                        "VALUES(?, 'rough-media', ?, ?, 'available')",
                        (asset_id, path.name, path.stat().st_mtime_ns),
                    )
                connection.execute(
                    "INSERT INTO task_assets(task_id,asset_id,role) "
                    "VALUES('task-a','segment-video','broll')"
                )
                connection.execute(
                    "INSERT INTO task_assets(task_id,asset_id,role) "
                    "VALUES('task-a','digital-video','digital_human')"
                )
                connection.execute(
                    "INSERT INTO segments(segment_id,asset_id,start_sec,end_sec) "
                    "VALUES('segment-20-30','segment-video',20,30)"
                )
                connection.execute(
                    """
                    INSERT INTO scene_cards(
                      card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                      asset_id,segment_id,mix_form,enter_tc,exit_tc,audio_policy_draft
                    ) VALUES(
                      'task-a-segment','task-a',1,'片段',4,8,'segment-video',
                      'segment-20-30','broll全屏',0,0,'mute'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO scene_cards(
                      card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                      asset_id,mix_form,enter_tc,exit_tc,audio_policy_draft
                    ) VALUES(
                      'task-a-digital','task-a',2,'口播',12,16,'digital-video',
                      '数字人全屏',0,0,'keep'
                    )
                    """
                )

        status, _, detail = self.request("GET", "/api/tasks/task-a")

        self.assertEqual(status, 200)
        cards = {card["card_id"]: card for card in detail["sceneCards"]}
        self.assertEqual(
            (
                cards["task-a-segment"]["source_range_start"],
                cards["task-a-segment"]["source_range_end"],
                cards["task-a-segment"]["resolved_source_start"],
                cards["task-a-segment"]["resolved_source_end"],
            ),
            (20.0, 30.0, 20.0, 24.0),
        )
        self.assertEqual(
            (
                cards["task-a-digital"]["resolved_source_start"],
                cards["task-a-digital"]["resolved_source_end"],
            ),
            (12.0, 16.0),
        )

    def test_picture_lock_rejects_the_specific_offline_scene(self):
        self.seed_rough_media_task()
        self.rough_image.unlink()

        status, _, payload = self.request(
            "POST",
            "/api/tasks/task-a/picture/lock",
            {"mediaVersions": {}},
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "ROUGH_MEDIA_OFFLINE")
        self.assertIn("第 1 镜头", payload["error"]["message"])

    def test_preflight_preserves_unknown_ranges_cost_scope_queue_and_confirmations(self):
        expected = {
            "checks": [{"code": "REAL", "status": "passed"}],
            "blocking": [],
            "warnings": [],
            "estimated_space_bytes": None,
            "next_action": "create_task",
            "estimates": {
                "machine_seconds": {"status": "unavailable", "min": None, "max": None, "unit": "seconds", "basis": None, "reason": "没有测速"},
                "human_seconds": {"status": "estimated", "min": 60, "max": 120, "unit": "seconds", "basis": "现场记录", "reason": None},
                "api_cost": {"status": "estimated", "min": 0.3, "max": 0.8, "unit": "CNY", "basis": "供应商价目", "reason": None, "excluded_costs": ["缺失画面生成"]},
            },
            "cost_scope": {"included_costs": ["AI 分镜文本模型"], "excluded_costs": ["缺失画面生成"]},
            "queue": {"heavy_slot": "busy", "running_job_id": None, "waiting_count": 2, "observed_at": "2026-09-07T12:00:00Z", "estimated_wait_seconds": {"status": "unavailable", "min": None, "max": None, "unit": "seconds", "basis": None, "reason": "没有耗时基线"}},
            "confirmations": [{"key": "brief", "label": "文案锁定"}],
            "source": None,
        }
        with patch("workbench.api.preflight_digital_task", return_value=expected):
            status, _, payload = self.request(
                "POST", "/api/tasks/preflight", {"route": "digital_human"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)

    def test_create_calls_preflighted_domain_creation_and_returns_sync_success(self):
        created = {"task_id": "new-task", "workflow": {"current_stage": "brief_lock"}}
        with patch("workbench.api.create_digital_task", return_value=created) as action:
            status, _, payload = self.request(
                "POST", "/api/tasks", {"route": "digital_human", "title": "新任务"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload, created)
        self.assertEqual(action.call_args.args[1]["title"], "新任务")

    def test_blocked_creation_returns_409_with_unmodified_preflight_details(self):
        preflight = {
            "blocking": [{"code": "PRIMARY_LIBRARY_OFFLINE", "message": "主素材目录离线"}],
            "estimates": {"api_cost": {"status": "unavailable", "min": None, "max": None}},
            "queue": {"heavy_slot": "unknown", "waiting_count": None},
            "confirmations": [{"key": "brief"}],
        }
        with patch(
            "workbench.api.create_digital_task",
            side_effect=DigitalTaskPreflightError(preflight),
        ):
            status, _, payload = self.request(
                "POST", "/api/tasks", {"route": "digital_human"}
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "DIGITAL_TASK_PREFLIGHT_FAILED")
        self.assertEqual(payload["error"]["details"], preflight)

    def test_gate_conflict_is_409_and_internal_details_become_diagnostic_id(self):
        self.seed_task()
        with patch(
            "workbench.api.set_picture_lock",
            side_effect=RuntimeError("BRIEF_GATE_MISSING: 必须先锁定文案"),
        ):
            status, _, payload = self.request(
                "POST", "/api/tasks/task-1/picture/lock", {"mediaVersions": {}}
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "BRIEF_GATE_MISSING")

        with patch(
            "workbench.api.project_workflow",
            side_effect=RuntimeError("FFmpeg C:/secret/key.txt"),
        ):
            status, _, payload = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "INTERNAL_ERROR")
        self.assertRegex(payload["error"]["diagnosticId"], r"^diag-[a-f0-9]{16}$")
        self.assertNotIn("secret", json.dumps(payload, ensure_ascii=False).lower())
        diagnostic_id = payload["error"]["diagnosticId"]
        status, _, diagnostic = self.request("GET", f"/api/diagnostics/{diagnostic_id}")
        self.assertEqual(status, 200)
        self.assertEqual(diagnostic, {"diagnosticId": diagnostic_id})

    def test_missing_and_invalid_requests_use_404_and_400(self):
        status, _, payload = self.request("GET", "/api/tasks/missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

        self.seed_task()
        status, _, payload = self.request(
            "POST", "/api/tasks/task-1/rough-cut/impact", {"edit": "not-an-object"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

    def test_heavy_actions_persist_fifo_jobs_and_return_202(self):
        self.seed_task()
        cases = [
            ("/api/tasks/task-1/sources/sync", {"impactToken": "sync-token"}, "sync_sources"),
            ("/api/tasks/task-1/storyboard/recommend", {"confirmPaidCall": True, "requestId": "recommend-1"}, "recommend_storyboard"),
            ("/api/tasks/task-1/render", {}, "render_and_qa"),
            ("/api/tasks/task-1/publish", {"platform": "视频号"}, "build_publish_package"),
        ]
        job_ids = []
        positions = []
        gates = {
            "brief_locked": True,
            "picture_lock": True,
            "packaging_confirmed": True,
            "human_reviewed": True,
        }
        with (
            patch("workbench.api.project_gates", return_value=gates),
            patch(
                "workbench.api._require_candidate_binding",
                return_value={"candidate_id": "candidate-current"},
            ),
        ):
            for path, body, expected_kind in cases:
                with self.subTest(path=path):
                    status, _, payload = self.request("POST", path, body)
                    self.assertEqual(status, 202)
                    self.assertEqual(payload["status"], "queued")
                    self.assertEqual(payload["kind"], expected_kind)
                    job_ids.append(payload["jobId"])
                    positions.append(payload["queuePosition"])
        with CatalogDatabase.open(self.db_path) as db:
            rows = db.connection.execute(
                "SELECT job_id, status FROM jobs ORDER BY rowid"
            ).fetchall()
        self.assertEqual([row["job_id"] for row in rows], job_ids)
        self.assertTrue(all(row["status"] == "queued" for row in rows))
        self.assertEqual(positions, [1, 2, 3, 4])

    def test_publish_request_preserves_cover_tags_and_delivery_fields(self):
        self.seed_task()
        gates = {
            "brief_locked": True,
            "picture_lock": True,
            "packaging_confirmed": True,
            "human_reviewed": True,
        }
        with (
            patch("workbench.api.project_gates", return_value=gates),
            patch(
                "workbench.api._require_candidate_binding",
                return_value={"candidate_id": "candidate-current"},
            ),
        ):
            status, _, result = self.request(
                "POST",
                "/api/tasks/task-1/publish",
                {
                    "candidateId": "candidate-current",
                    "platform": "视频号",
                    "title": "产业更新",
                    "tags": ["招商", "产业"],
                    "coverChoice": "external",
                    "externalCoverPath": "C:/task/approved.png",
                    "deliveryType": "editable",
                },
            )
        self.assertEqual(status, 202)
        with CatalogDatabase.open(self.db_path) as db:
            row = db.connection.execute(
                "SELECT payload_json FROM job_payloads WHERE job_id=?",
                (result["jobId"],),
            ).fetchone()
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["tags"], ["招商", "产业"])
        self.assertEqual(payload["cover_choice"], "external")
        self.assertEqual(payload["external_cover_path"], "C:/task/approved.png")
        self.assertEqual(payload["delivery_type"], "editable")

    def test_publish_request_rejects_invalid_cover_or_delivery_mode(self):
        self.seed_task()
        gates = {"human_reviewed": True}
        with (
            patch("workbench.api.project_gates", return_value=gates),
            patch(
                "workbench.api._require_candidate_binding",
                return_value={"candidate_id": "candidate-current"},
            ),
        ):
            for field, value in (("coverChoice", "fourth"), ("deliveryType", "archive")):
                with self.subTest(field=field):
                    status, _, result = self.request(
                        "POST", "/api/tasks/task-1/publish",
                        {"platform": "视频号", field: value},
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(result["error"]["code"], "INVALID_REQUEST")

    def test_locking_brief_queues_transcription_before_material_work(self):
        self.seed_task()
        status, _, payload = self.request(
            "POST", "/api/tasks/task-1/brief/lock", {}
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["kind"], "transcribe")
        self.assertEqual(payload["status"], "queued")
        with CatalogDatabase.open(self.db_path) as db:
            self.assertEqual(
                db.connection.execute(
                    "SELECT status FROM tasks WHERE task_id='task-1'"
                ).fetchone()[0],
                "brief_locked",
            )
            self.assertEqual(
                db.connection.execute(
                    "SELECT job_type FROM jobs WHERE job_id=?",
                    (payload["jobId"],),
                ).fetchone()[0],
                "transcribe",
            )

    def test_storyboard_request_id_is_idempotent_within_one_task(self):
        self.seed_task()
        body = {"confirmPaidCall": True, "requestId": "same-click"}
        with patch(
            "workbench.api.project_gates", return_value={"brief_locked": True}
        ):
            first = self.request("POST", "/api/tasks/task-1/storyboard/recommend", body)
            second = self.request("POST", "/api/tasks/task-1/storyboard/recommend", body)
        self.assertEqual(first[0], 202)
        self.assertEqual(second[0], 202)
        self.assertEqual(first[2]["jobId"], second[2]["jobId"])
        with CatalogDatabase.open(self.db_path) as db:
            self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_paid_unknown_outcome_is_not_offered_unsafe_resume(self):
        self.seed_task()
        with CatalogDatabase.open(self.db_path) as db:
            enqueue_job(
                db,
                job_id="paid-unknown",
                job_type="recommend_storyboard",
                task_id="task-1",
                payload={"prompt": "园区镜头"},
                cost_status="approved",
                provider_request_id="provider-unknown",
            )
            db.connection.execute(
                "UPDATE jobs SET status='interrupted' WHERE job_id='paid-unknown'"
            )
            db.connection.execute(
                "UPDATE job_payloads SET outcome_status='unknown' WHERE job_id='paid-unknown'"
            )
            db.connection.commit()
        status, _, detail = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 200)
        job = detail["jobs"][0]
        self.assertFalse(job["canResume"])
        self.assertTrue(job["needsManualCheck"])
        self.assertEqual(job["nextAction"], "manual_check")

    def test_task_filtered_jobs_keep_global_fifo_position(self):
        self.seed_task()
        self.seed_task("task-2")
        with CatalogDatabase.open(self.db_path) as db:
            enqueue_job(db, job_id="other-first", job_type="render", task_id="task-2", payload={})
            enqueue_job(db, job_id="mine-second", job_type="render", task_id="task-1", payload={})
        status, _, detail = self.request("GET", "/api/tasks/task-1")
        self.assertEqual(status, 200)
        self.assertEqual(detail["jobs"][0]["jobId"], "mine-second")
        self.assertEqual(detail["jobs"][0]["queuePosition"], 2)

    def test_paid_storyboard_needs_explicit_confirmation_not_preflight_estimate(self):
        self.seed_task()
        with patch(
            "workbench.api.project_gates", return_value={"brief_locked": True}
        ):
            status, _, payload = self.request(
                "POST", "/api/tasks/task-1/storyboard/recommend", {"requestId": "r-1"}
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "PAID_CONFIRMATION_REQUIRED")

    def test_cancel_and_resume_use_durable_job_rules(self):
        self.seed_task()
        with CatalogDatabase.open(self.db_path) as db:
            enqueue_job(
                db,
                job_id="queued-job",
                job_type="render",
                task_id="task-1",
                payload={},
            )
        status, _, payload = self.request("POST", "/api/jobs/queued-job/cancel", {"reason": "不再需要"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "cancelled")

        with CatalogDatabase.open(self.db_path) as db:
            enqueue_job(
                db,
                job_id="failed-job",
                job_type="render",
                task_id="task-1",
                payload={},
            )
            db.connection.execute("UPDATE jobs SET status='failed' WHERE job_id='failed-job'")
            db.connection.commit()
        status, _, payload = self.request("POST", "/api/jobs/failed-job/resume")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "queued")

        status, _, one_job = self.request("GET", "/api/jobs/failed-job")
        self.assertEqual(status, 200)
        self.assertEqual(one_job["jobId"], "failed-job")
        status, _, all_jobs = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual({item["jobId"] for item in all_jobs["jobs"]}, {"queued-job", "failed-job"})

    def test_domain_actions_are_exposed_without_generic_gate_override(self):
        self.seed_task()
        action_cases = [
            ("/api/tasks/task-1/rough-cut/impact", {"edit": {"action": "delete", "card_id": "c1"}}, "preview_edit_impact", {"impact_digest": "d"}),
            ("/api/tasks/task-1/rough-cut/apply", {"edit": {"action": "delete", "card_id": "c1"}}, "apply_edit", {"edit_id": "e1"}),
            ("/api/tasks/task-1/rough-cut/undo", {}, "undo_last_edit", {"status": "reverted"}),
            ("/api/tasks/task-1/rough-cut/redo", {}, "redo_last_edit", {"status": "applied"}),
            ("/api/tasks/task-1/picture/lock", {"mediaVersions": {}}, "set_picture_lock", {"task_id": "task-1"}),
            ("/api/tasks/task-1/packaging/propose", {}, "propose_packaging", ["p1"]),
            ("/api/tasks/task-1/watch/start", {"snapshotId": "s", "qaReportId": "q"}, "start_watch", {"session_id": "w"}),
            ("/api/tasks/task-1/watch/w/tick", {"positionSeconds": 3}, "record_watch_tick", {"covered_seconds": 3}),
            ("/api/tasks/task-1/watch/w/complete", {}, "complete_watch", {"status": "completed"}),
            ("/api/tasks/task-1/review/approve", {"snapshotId": "s", "qaReportId": "q", "watchSessionId": "w", "reviewedBy": "同事"}, "approve_after_full_watch", {"decision": "approved"}),
            ("/api/tasks/task-1/review/reject", {"snapshotId": "s", "qaReportId": "q", "reviewedBy": "同事", "notes": "画面不对"}, "reject_review", {"decision": "rejected"}),
        ]
        all_gates = {"brief_locked": True, "picture_lock": True}
        for path, body, target, returned in action_cases:
            with self.subTest(path=path), ExitStack() as stack:
                stack.enter_context(patch(f"workbench.api.{target}", return_value=returned))
                stack.enter_context(patch("workbench.api.project_gates", return_value=all_gates))
                stack.enter_context(patch(
                    "workbench.api._require_candidate_binding",
                    return_value={"candidate_id": "candidate-current"},
                ))
                if "/watch/w/" in path:
                    stack.enter_context(
                        patch(
                            "workbench.api.get_watch_session",
                            return_value={"task_id": "task-1"},
                        )
                    )
                status, _, payload = self.request("POST", path, body)
                self.assertEqual(status, 200)
                self.assertIsNotNone(payload)

        status, _, payload = self.request(
            "POST", "/api/tasks/task-1/stages/complete", {"stage": "publish_package"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def test_picture_lock_click_confirms_current_rough_cut_atomically(self):
        self.seed_task()
        media_root = self.root / "picture-lock-media"
        media_root.mkdir()
        source = media_root / "digital.mp4"
        source.write_bytes(b"real-video-source")
        with CatalogDatabase.open(self.db_path) as db:
            register_library(
                db,
                "picture-lock-media",
                "画面锁定素材",
                "local",
                str(media_root),
                purpose="task_specific",
            )
            set_status(db, "task-1", "brief_locked")
            with db.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO assets(
                      asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                    ) VALUES('lock-digital','video',?,'fp-lock-digital',datetime('now'))
                    """,
                    (source.stat().st_size,),
                )
                connection.execute(
                    """
                    INSERT INTO asset_locations(
                      asset_id,library_id,relative_path,mtime_ns,state
                    ) VALUES('lock-digital','picture-lock-media',?,?, 'available')
                    """,
                    (source.name, source.stat().st_mtime_ns),
                )
                connection.execute(
                    """
                    INSERT INTO task_assets(task_id,asset_id,role)
                    VALUES('task-1','lock-digital','digital_human')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO scene_cards(
                      card_id,task_id,idx,narrative_start,narrative_end,asset_id,
                      mix_form,enter_tc,exit_tc,audio_policy_draft,confirm_status
                    ) VALUES(
                      'task-1#0','task-1',0,0,1,'lock-digital',
                      '数字人全屏',0,0,'keep',0
                    )
                    """
                )

        with patch("workbench.api.assert_picture_media_available") as validate_media:
            status, _, payload = self.request(
                "POST",
                "/api/tasks/task-1/picture/lock",
                {"mediaVersions": {"task-1#0": "version-1"}},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["changed_since_lock"], 0)
        self.assertEqual(validate_media.call_args.args[1:], (
            "task-1", {"task-1#0": "version-1"}
        ))
        with CatalogDatabase.open(self.db_path) as db:
            self.assertEqual(
                db.connection.execute(
                    "SELECT confirm_status FROM scene_cards WHERE card_id='task-1#0'"
                ).fetchone()[0],
                1,
            )

    def test_task_preview_request_enqueues_scoped_proxy_generation(self):
        self.seed_task()
        with (
            patch("workbench.api.resolve_rough_media") as resolve_media,
            patch("workbench.api.enqueue_job", return_value="job-preview") as enqueue,
            patch(
                "workbench.api.get_job",
                return_value={
                    "job_id": "job-preview",
                    "job_type": "generate_preview",
                    "task_id": "task-1",
                    "status": "queued",
                    "progress_current": 0,
                    "progress_total": 1,
                    "phase_label": "",
                    "error_code": None,
                    "error_summary": None,
                    "cost_status": "not_applicable",
                    "provider_request_id": None,
                    "created_at": "2026-09-23 00:00:00",
                    "updated_at": "2026-09-23 00:00:00",
                },
            ),
        ):
            status, _, payload = self.request(
                "POST",
                "/api/tasks/task-1/rough-media/asset-video/preview",
                {},
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload["jobId"], "job-preview")
        resolve_media.assert_called_once_with(
            unittest.mock.ANY, "task-1", "asset-video", "preview"
        )
        self.assertEqual(
            enqueue.call_args.kwargs["payload"],
            {
                "action": "generate_preview",
                "task_id": "task-1",
                "asset_id": "asset-video",
            },
        )

    def test_picture_lock_requires_the_media_versions_shown_to_the_user(self):
        self.seed_task()
        with CatalogDatabase.open(self.db_path) as db:
            with db.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO scene_cards(
                      card_id,task_id,idx,narrative_start,narrative_end,mix_form,
                      enter_tc,exit_tc,audio_policy_draft,confirm_status
                    ) VALUES('task-1#0','task-1',0,0,1,'数字人全屏',0,0,'keep',0)
                    """
                )

        status, _, payload = self.request(
            "POST", "/api/tasks/task-1/picture/lock", {}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

    def test_agent_task_web_storyboard_button_cannot_invoke_text_provider(self):
        with CatalogDatabase.open(self.db_path) as db:
            create_task(
                db,
                "agent-task",
                "Agent 任务",
                "digital_human",
                brief={"final_script": {"sha256": "abc"}},
                orchestration_mode="agent",
            )
            set_status(db, "agent-task", "brief_locked")

        status, _, payload = self.request(
            "POST",
            "/api/tasks/agent-task/storyboard/recommend",
            {"confirmPaidCall": True, "requestId": "must-not-run"},
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "AGENT_PLANNING_REQUIRED")

    def test_packaging_compose_and_confirm_are_distinct_actions(self):
        self.seed_task()
        with patch(
            "workbench.api.project_gates", return_value={"picture_lock": True}
        ), patch("workbench.api.create_blank_plan", return_value="draft-1"):
            status, _, draft = self.request(
                "POST", "/api/tasks/task-1/packaging/compose", {"planName": "自选组合"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(draft["planId"], "draft-1")
        self.assertEqual(draft["status"], "draft")

        with patch(
            "workbench.api.get_plan",
            return_value={"plan_id": "draft-1", "task_id": "task-1"},
        ), patch(
            "workbench.api.confirm_plan_after_preview",
            return_value={"plan_id": "draft-1", "status": "confirmed"},
        ) as confirm:
            status, _, confirmed = self.request(
                "POST",
                "/api/tasks/task-1/packaging/draft-1/confirm",
                {"previewId": "pv-1"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        confirm.assert_called_once_with(
            unittest.mock.ANY,
            "task-1",
            "draft-1",
            "pv-1",
            project_root=self.root,
        )

    def test_packaging_preview_template_and_catalog_check_routes(self):
        self.seed_task()
        artifact = {
            "preview_id": "pv-1",
            "plan_id": "draft-1",
            "status": "ready",
            "preview_profile": "portrait-540p-v1",
            "plan_digest": "plan-a",
            "picture_lock_digest": "picture-a",
            "viewed_at": None,
            "coverage": [],
            "error_code": None,
        }
        with ExitStack() as stack:
            stack.enter_context(patch(
                "workbench.api.get_plan",
                return_value={"plan_id": "draft-1", "task_id": "task-1"},
            ))
            request = stack.enter_context(patch(
                "workbench.api.request_preview",
                return_value={
                    "previewId": "pv-1",
                    "planId": "draft-1",
                    "status": "queued",
                    "jobId": "job-1",
                    "job": {
                        "job_id": "job-1", "job_type": "packaging_preview",
                        "task_id": "task-1", "library_id": None,
                        "status": "queued", "progress_current": 0,
                        "progress_total": 1, "phase_label": "等待中",
                        "queue_position": 1, "retry_policy": "explicit",
                        "outcome_status": None, "updated_at": "now",
                    },
                },
            ))
            current = stack.enter_context(patch(
                "workbench.api.current_preview_for_plan", return_value=artifact,
            ))
            viewed = stack.enter_context(patch(
                "workbench.api.mark_preview_viewed", return_value={**artifact, "viewed_at": "now"},
            ))
            save = stack.enter_context(patch(
                "workbench.api.save_personal_template", return_value={"template_id": "pt-1", "status": "template"},
            ))
            templates = stack.enter_context(patch(
                "workbench.api.list_personal_templates", return_value=[{"template_id": "pt-1"}],
            ))
            check = stack.enter_context(patch(
                "workbench.api.request_packaging_catalog_check",
                return_value={"checkId": "pcc-1", "status": "queued", "jobId": "job-1"},
            ))
            latest = stack.enter_context(patch(
                "workbench.api.latest_packaging_catalog_check",
                return_value={"checkId": "pcc-1", "status": "completed", "items": []},
            ))

            self.assertEqual(self.request("POST", "/api/tasks/task-1/packaging/draft-1/preview", {})[0], 202)
            status, _, payload = self.request("GET", "/api/tasks/task-1/packaging/draft-1/preview")
            self.assertEqual((status, payload["previewId"]), (200, "pv-1"))
            self.assertEqual(self.request("POST", "/api/tasks/task-1/packaging-previews/pv-1/viewed", {})[0], 200)
            self.assertEqual(self.request("POST", "/api/tasks/task-1/packaging/draft-1/save-template", {"templateName": "我的包装"})[0], 200)
            status, _, payload = self.request("GET", "/api/packaging-templates?route=digital_human")
            self.assertEqual((status, payload["templates"][0]["template_id"]), (200, "pt-1"))
            self.assertEqual(self.request("POST", "/api/packaging-catalog/check", {})[0], 202)
            self.assertEqual(self.request("GET", "/api/packaging-catalog/checks/latest")[0], 200)

        request.assert_called_once_with(unittest.mock.ANY, "task-1", "draft-1")
        self.assertEqual(current.call_count, 2)
        current.assert_any_call(unittest.mock.ANY, "task-1", "draft-1")
        viewed.assert_called_once_with(unittest.mock.ANY, "task-1", "pv-1")
        save.assert_called_once_with(unittest.mock.ANY, "draft-1", "我的包装")
        templates.assert_called_once_with(unittest.mock.ANY, "digital_human")
        check.assert_called_once_with(unittest.mock.ANY, self.root)
        latest.assert_called_once_with(unittest.mock.ANY)

    def test_real_gate_projection_blocks_packaging_render_and_publish_without_evidence(self):
        self.seed_task()
        cases = [
            ("/api/tasks/task-1/packaging/compose", {}, "PICTURE_LOCK_MISSING"),
            ("/api/tasks/task-1/render", {}, "PACKAGING_PLAN_MISSING"),
            ("/api/tasks/task-1/publish", {"platform": "视频号"}, "REVIEW_NOT_APPROVED"),
        ]
        for path, body, code in cases:
            with self.subTest(path=path):
                status, _, payload = self.request("POST", path, body)
                self.assertEqual(status, 409)
                self.assertEqual(payload["error"]["code"], code)
        with CatalogDatabase.open(self.db_path) as db:
            self.assertEqual(
                db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                0,
            )

    def test_api_module_contains_no_sql_or_shell_execution_escape_hatch(self):
        import workbench.api as api

        source = inspect.getsource(api).upper()
        self.assertNotIn("SELECT ", source)
        self.assertNotIn("INSERT INTO", source)
        self.assertNotIn("UPDATE ", source)
        self.assertNotIn("DELETE FROM", source)
        self.assertNotIn("SHELL=TRUE", source)
        self.assertNotIn("SUBPROCESS", source)

    def test_libraries_can_be_listed_registered_and_queued_for_incremental_check(self):
        source = self.root / "park-materials"
        source.mkdir()
        status, _, created = self.request(
            "POST",
            "/api/libraries",
            {
                "name": "园区实拍库",
                "mountPath": str(source),
                "purpose": "task_specific",
                "kind": "local",
                "isDefaultGeneric": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(created["purpose"], "task_specific")
        self.assertTrue(created["online"])
        self.assertEqual(created["assetCount"], 0)
        self.assertIn("lastSyncAt", created)

        status, _, payload = self.request("GET", "/api/libraries")
        self.assertEqual(status, 200)
        self.assertEqual(payload["libraries"][0]["name"], "园区实拍库")

        status, _, job = self.request(
            "POST", f"/api/libraries/{created['libraryId']}/check"
        )
        self.assertEqual(status, 202)
        self.assertEqual(job["kind"], "scan")
        self.assertEqual(job["status"], "queued")

    def test_offline_library_can_be_reconnected_without_replacing_its_identity(self):
        old_source = self.root / "old-park-materials"
        new_source = self.root / "new-park-materials"
        old_source.mkdir()
        new_source.mkdir()
        with CatalogDatabase.open(self.db_path) as db:
            register_library(
                db,
                "park-library",
                "园区实拍库",
                "local",
                str(old_source),
                purpose="task_specific",
            )
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE libraries SET online=0 WHERE library_id=?",
                    ("park-library",),
                )

        status, _, result = self.request(
            "POST",
            "/api/libraries/park-library/mount",
            {"mountPath": str(new_source)},
        )
        self.assertEqual(status, 202)
        self.assertEqual(result["library"]["libraryId"], "park-library")
        self.assertEqual(result["library"]["mountPath"], str(new_source))
        self.assertFalse(result["library"]["online"])
        self.assertEqual(result["job"]["kind"], "scan")

        status, _, payload = self.request("GET", "/api/libraries")
        self.assertEqual(status, 200)
        self.assertEqual(payload["libraries"][0]["libraryId"], "park-library")
        self.assertEqual(payload["libraries"][0]["mountPath"], str(new_source))

    def test_packaging_catalog_uses_manifest_categories_and_license_status(self):
        status, _, payload = self.request("GET", "/api/packaging-catalog")
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["label"] for item in payload["categories"]],
            ["字幕", "音色", "BGM", "音效", "文字卡", "贴纸", "花字", "Logo / 水印"],
        )
        bgm = next(item for item in payload["categories"] if item["key"] == "bgm")
        self.assertGreaterEqual(bgm["count"], 1)
        self.assertTrue(
            all("authorizationStatus" in asset for asset in bgm["assets"])
        )
        sfx = next(item for item in payload["categories"] if item["key"] == "sfx")
        self.assertGreaterEqual(sfx["count"], 1)
        self.assertTrue(all("availabilityStatus" in asset for asset in sfx["assets"]))
        self.assertTrue(all("previewUrl" in asset for asset in sfx["assets"]))
        self.assertEqual(
            [item["layer"] for item in sfx["directories"]],
            ["core", "extension", "local"],
        )

        preview_url = sfx["assets"][0]["previewUrl"]
        host, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("HEAD", preview_url, headers={"Cookie": self.cookie})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("audio/"))
        self.assertGreater(int(response.getheader("Content-Length")), 0)
        connection.close()

    def test_storage_status_previews_then_safely_cleans_only_registered_cache(self):
        cache_file = self.root / "cache" / "asset-1.proxy.mp4"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"proxy")
        with CatalogDatabase.open(self.db_path) as db:
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                    "VALUES('asset-1','video',5,'fingerprint',datetime('now'))"
                )
                connection.execute(
                    "INSERT INTO preview_cache(asset_id,kind,rel_path,size_bytes) "
                    "VALUES('asset-1','proxy','asset-1.proxy.mp4',5)"
                )

        status, _, payload = self.request("GET", "/api/settings/storage")
        self.assertEqual(status, 200)
        self.assertEqual(payload["cache"]["totalBytes"], 5)
        self.assertEqual(
            [item["key"] for item in payload["directories"]],
            ["project", "database", "cache", "coreAssets", "extensionPacks", "personalAssets", "outputs"],
        )

        status, _, preview = self.request(
            "POST", "/api/settings/storage/cache/preview"
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview["dryRun"])
        self.assertEqual(preview["freedBytes"], 5)
        self.assertTrue(preview["impactToken"])
        self.assertTrue(cache_file.exists())

        status, _, rejected = self.request(
            "POST", "/api/settings/storage/cache/clean"
        )
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"]["code"], "CACHE_PREVIEW_REQUIRED")
        self.assertTrue(cache_file.exists())

        newer_cache = self.root / "cache" / "asset-2.thumb.jpg"
        newer_cache.write_bytes(b"new-preview")
        with CatalogDatabase.open(self.db_path) as db:
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                    "VALUES('asset-2','image',11,'fingerprint-2',datetime('now'))"
                )
                connection.execute(
                    "INSERT INTO preview_cache(asset_id,kind,rel_path,size_bytes) "
                    "VALUES('asset-2','thumbnail','asset-2.thumb.jpg',11)"
                )

        status, _, cleaned = self.request(
            "POST", "/api/settings/storage/cache/clean",
            {"impactToken": preview["impactToken"]},
        )
        self.assertEqual(status, 200)
        self.assertFalse(cleaned["dryRun"])
        self.assertEqual(cleaned["freedBytes"], 5)
        self.assertFalse(cache_file.exists())
        self.assertTrue(newer_cache.exists())

        status, _, replayed = self.request(
            "POST", "/api/settings/storage/cache/clean",
            {"impactToken": preview["impactToken"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(replayed["error"]["code"], "CACHE_PREVIEW_STALE")
        self.assertTrue(newer_cache.exists())

    def test_cache_cleanup_validates_the_whole_preview_before_deleting_anything(self):
        cache = self.root / "cache"
        cache.mkdir()
        first = cache / "first.proxy.mp4"
        second = cache / "second.proxy.mp4"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        with CatalogDatabase.open(self.db_path) as db:
            with db.transaction() as connection:
                connection.executemany(
                    "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                    "VALUES(?,?,?,?,datetime('now'))",
                    (("first", "video", 5, "fp-first"), ("second", "video", 6, "fp-second")),
                )
                connection.executemany(
                    "INSERT INTO preview_cache(asset_id,kind,rel_path,size_bytes) VALUES(?,?,?,?)",
                    (("first", "proxy", first.name, 5), ("second", "proxy", second.name, 6)),
                )
        status, _, preview = self.request(
            "POST", "/api/settings/storage/cache/preview"
        )
        self.assertEqual(status, 200)
        second.write_bytes(b"changed")

        status, _, rejected = self.request(
            "POST", "/api/settings/storage/cache/clean",
            {"impactToken": preview["impactToken"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"]["code"], "CACHE_PREVIEW_STALE")
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_settings_persists_only_credential_reference_and_never_returns_secret(self):
        from workbench import api as workbench_api

        with patch.object(
            workbench_api, "save_secret", return_value="cutroom/general_text"
        ) as saved:
            status, _, configured = self.request(
                "PUT",
                "/api/settings/capabilities/general_text",
                {
                    "provider": "火山方舟",
                    "model": "deepseek-v4-pro",
                    "secret": "sk-never-return-this",
                },
            )
        self.assertEqual(status, 200)
        saved.assert_called_once()
        self.assertTrue(configured["configured"])
        self.assertNotIn(
            "sk-never-return-this",
            json.dumps(configured, ensure_ascii=False),
        )
        self.assertNotIn("credentialRef", configured)

        with CatalogDatabase.open(self.db_path) as db:
            row = db.connection.execute(
                "SELECT credential_ref FROM capability_settings "
                "WHERE capability='general_text'"
            ).fetchone()
        self.assertEqual(row["credential_ref"], "cutroom/general_text")

        with patch.object(
            workbench_api, "read_secret", return_value="stored-secret"
        ):
            status, _, payload = self.request(
                "GET", "/api/settings/capabilities"
            )
        self.assertEqual(status, 200)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("stored-secret", text)
        self.assertNotIn("credentialRef", text)
        self.assertEqual(
            {item["capability"] for item in payload["capabilities"]},
            {
                "planning",
                "broll",
                "tts",
            },
        )

        with patch.object(workbench_api, "delete_secret") as deleted, patch.object(
            workbench_api, "read_secret", return_value=None
        ):
            status, _, removed = self.request(
                "DELETE", "/api/settings/capabilities/general_text"
            )
        self.assertEqual(status, 200)
        deleted.assert_called_once_with("general_text")
        self.assertFalse(removed["configured"])

    def test_settings_supports_multiple_named_profiles_and_real_connection_test(self):
        from workbench import api as workbench_api

        with patch.object(
            workbench_api,
            "save_provider_secret",
            return_value="cutroom/provider/gateway-local",
        ) as save_provider:
            status, _, account = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "gateway-local",
                    "name": "本机 New API",
                    "provider": "new_api",
                    "baseUrl": "http://127.0.0.1:3000",
                    "channelId": 7,
                    "secret": "sk-never-return",
                },
            )
        self.assertEqual(status, 200)
        save_provider.assert_called_once_with("gateway-local", "sk-never-return")
        self.assertNotIn("sk-never-return", json.dumps(account, ensure_ascii=False))

        created_ids = []
        for profile_id, name, model, default in (
            ("text-a", "豆包文案", "doubao-seed", True),
            ("text-b", "DeepSeek 文案", "deepseek-v4", False),
        ):
            status, _, profile = self.request(
                "POST",
                "/api/settings/model-profiles",
                {
                    "profileId": profile_id,
                    "capability": "general_text",
                    "displayName": name,
                    "accountId": "gateway-local",
                    "model": model,
                    "protocol": "openai_chat",
                    "isDefault": default,
                },
            )
            self.assertEqual(status, 200)
            created_ids.append(profile["profileId"])
        self.assertEqual(created_ids, ["text-a", "text-b"])

        tested = {
            "profileId": "text-b",
            "capability": "general_text",
            "displayName": "DeepSeek 文案",
            "accountId": "gateway-local",
            "accountName": "本机 New API",
            "provider": "new_api",
            "baseUrl": "http://127.0.0.1:3000",
            "model": "deepseek-v4",
            "settings": {"protocol": "openai_chat"},
            "channelId": 7,
            "isDefault": False,
            "enabled": True,
            "availabilityStatus": "available",
            "lastCheckedAt": "2026-09-09T02:00:00Z",
            "lastErrorCode": None,
            "lastErrorMessage": None,
            "verificationLevel": "request",
        }
        with patch.object(
            workbench_api, "probe_model_profile", return_value=tested
        ) as probe:
            status, _, result = self.request(
                "POST", "/api/settings/model-profiles/text-b/test"
            )
        self.assertEqual(status, 200)
        self.assertEqual(result["availabilityStatus"], "available")
        probe.assert_called_once()

        status, _, _ = self.request(
            "POST", "/api/settings/model-profiles/text-b/default"
        )
        self.assertEqual(status, 200)
        status, _, payload = self.request("GET", "/api/settings/model-profiles")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["profiles"]), 2)
        self.assertEqual(
            [item["profileId"] for item in payload["profiles"] if item["isDefault"]],
            ["text-b"],
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("credentialRef", serialized)
        self.assertNotIn("sk-never-return", serialized)

    def test_doubao_account_stores_catalog_credentials_locally(self):
        from media_catalog.credentials import decode_provider_credentials
        from workbench import api as workbench_api

        saved = {}

        def capture(account_id, raw):
            saved["account_id"] = account_id
            saved["raw"] = raw
            return f"cutroom/provider/{account_id}"

        with patch.object(
            workbench_api,
            "save_provider_secret",
            side_effect=capture,
        ):
            status, _, account = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "doubao-local",
                    "name": "豆包配音",
                    "preset": "doubao_tts",
                    "accessKeyId": "AKLT-account-id",
                    "secretAccessKey": "cloud-secret-never-return",
                    "appId": "tts-app-id",
                    "secret": "tts-token-never-return",
                    "ttsApiKey": "speech-api-key-never-return",
                    "ttsResourceId": "seed-tts-2.0",
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(saved["account_id"], "doubao-local")
        self.assertEqual(
            decode_provider_credentials(saved["raw"]),
            {
                "access_token": "tts-token-never-return",
                "secret_access_key": "cloud-secret-never-return",
                "api_key": "speech-api-key-never-return",
            },
        )
        self.assertEqual(account["settings"]["access_key_id"], "AKLT-account-id")
        self.assertEqual(account["settings"]["app_id"], "tts-app-id")
        self.assertEqual(account["settings"]["tts_resource_id"], "seed-tts-2.0")
        self.assertTrue(account["ttsConfigured"])
        self.assertTrue(account["ttsApiKeyConfigured"])
        self.assertTrue(account["legacyTtsConfigured"])
        self.assertTrue(account["voiceCatalogConfigured"])
        serialized = json.dumps(account, ensure_ascii=False)
        self.assertNotIn("tts-token-never-return", serialized)
        self.assertNotIn("cloud-secret-never-return", serialized)
        self.assertNotIn("speech-api-key-never-return", serialized)

    def test_doubao_account_rejects_unsupported_voice_resource_before_saving(self):
        from workbench import api as workbench_api

        with patch.object(
            workbench_api,
            "save_provider_secret",
        ) as save_provider, patch.object(
            workbench_api,
            "delete_provider_secret",
        ) as delete_provider:
            status, _, payload = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "doubao-forged-resource",
                    "name": "伪造服务版本",
                    "preset": "doubao_tts",
                    "accessKeyId": "AKLT-account-id",
                    "secretAccessKey": "cloud-secret-never-return",
                    "appId": "tts-app-id",
                    "secret": "tts-token-never-return",
                    "ttsResourceId": "forged-resource",
                },
            )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
        save_provider.assert_not_called()
        delete_provider.assert_not_called()

    def test_legacy_doubao_account_reports_missing_catalog_credentials(self):
        from media_catalog.model_profiles import create_provider_account
        from workbench import api as workbench_api

        with CatalogDatabase.open(self.db_path) as db:
            create_provider_account(
                db,
                account_id="legacy-doubao",
                name="旧豆包账号",
                provider="doubao_tts",
                base_url="https://openspeech.bytedance.com/api/v1/tts",
                credential_ref="cutroom/provider/legacy-doubao",
                settings={
                    "preset": "doubao_tts",
                    "protocol": "volcengine_tts",
                    "app_id": "legacy-app",
                },
            )

        with patch.object(
            workbench_api,
            "read_provider_secret",
            return_value="legacy-token",
        ):
            status, _, payload = self.request(
                "GET", "/api/settings/model-profiles"
            )

        self.assertEqual(status, 200)
        account = next(
            item for item in payload["accounts"]
            if item["accountId"] == "legacy-doubao"
        )
        self.assertTrue(account["ttsConfigured"])
        self.assertFalse(account["voiceCatalogConfigured"])
        self.assertNotIn("legacy-token", json.dumps(account, ensure_ascii=False))

    def test_doubao_credential_update_preserves_omitted_tts_token(self):
        from media_catalog.credentials import decode_provider_credentials
        from media_catalog.model_profiles import create_provider_account
        from workbench import api as workbench_api

        with CatalogDatabase.open(self.db_path) as db:
            create_provider_account(
                db,
                account_id="doubao-update",
                name="待补充目录权限",
                provider="doubao_tts",
                base_url="https://openspeech.bytedance.com/api/v1/tts",
                credential_ref="cutroom/provider/doubao-update",
                settings={
                    "preset": "doubao_tts",
                    "protocol": "volcengine_tts",
                    "app_id": "existing-app",
                },
            )

        saved = {}

        def capture(account_id, raw):
            saved["raw"] = raw
            return f"cutroom/provider/{account_id}"

        with patch.object(
            workbench_api,
            "read_provider_secret",
            return_value="existing-tts-token",
        ), patch.object(
            workbench_api,
            "save_provider_secret",
            side_effect=capture,
        ):
            status, _, account = self.request(
                "PUT",
                "/api/settings/provider-accounts/doubao-update/credentials",
                {
                    "accessKeyId": "AKLT-new",
                    "secretAccessKey": "new-cloud-sk",
                    "ttsResourceId": "seed-tts-1.0",
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            decode_provider_credentials(saved["raw"]),
            {
                "access_token": "existing-tts-token",
                "secret_access_key": "new-cloud-sk",
                "api_key": None,
            },
        )
        self.assertEqual(account["settings"]["app_id"], "existing-app")
        self.assertEqual(account["settings"]["access_key_id"], "AKLT-new")
        self.assertEqual(account["settings"]["tts_resource_id"], "seed-tts-1.0")
        self.assertTrue(account["ttsConfigured"])
        self.assertTrue(account["voiceCatalogConfigured"])

    def test_doubao_api_key_only_synthesis_credentials_are_accepted_and_preserved(self):
        from media_catalog.credentials import (
            decode_provider_credentials,
            encode_provider_credentials,
        )
        from media_catalog.model_profiles import create_provider_account
        from workbench import api as workbench_api

        with CatalogDatabase.open(self.db_path) as db:
            create_provider_account(
                db,
                account_id="doubao-api-key-update",
                name="新版豆包",
                provider="doubao_tts",
                base_url="https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
                credential_ref="cutroom/provider/doubao-api-key-update",
                settings={
                    "preset": "doubao_tts",
                    "protocol": "volcengine_tts",
                    "tts_resource_id": "seed-tts-1.0",
                },
            )

        saved = {}

        def capture(account_id, raw):
            saved["raw"] = raw
            return f"cutroom/provider/{account_id}"

        with patch.object(
            workbench_api,
            "read_provider_secret",
            return_value=encode_provider_credentials(
                None, "existing-catalog-sk", "existing-speech-api-key"
            ),
        ), patch.object(
            workbench_api,
            "save_provider_secret",
            side_effect=capture,
        ):
            status, _, account = self.request(
                "PUT",
                "/api/settings/provider-accounts/doubao-api-key-update/credentials",
                {
                    "accessKeyId": "AKLT-existing",
                    "ttsResourceId": "seed-tts-1.0",
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            decode_provider_credentials(saved["raw"]),
            {
                "access_token": None,
                "secret_access_key": "existing-catalog-sk",
                "api_key": "existing-speech-api-key",
            },
        )
        self.assertTrue(account["ttsConfigured"])
        self.assertTrue(account["ttsApiKeyConfigured"])
        self.assertFalse(account["legacyTtsConfigured"])

    def test_settings_accepts_cc_switch_style_direct_provider_without_channel(self):
        from workbench import api as workbench_api

        with patch.object(
            workbench_api,
            "save_provider_secret",
            return_value="cutroom/provider/ark-direct",
        ) as save_provider:
            status, _, account = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "ark-direct",
                    "name": "火山方舟直连",
                    "preset": "volcengine_ark",
                    "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
                    "defaultModel": "ep-example",
                    "note": "分镜与包装",
                    "secret": "ark-never-return",
                },
            )

        self.assertEqual(status, 200)
        save_provider.assert_called_once_with("ark-direct", "ark-never-return")
        self.assertEqual(account["provider"], "volcengine_ark")
        self.assertEqual(account["preset"], "volcengine_ark")
        self.assertEqual(account["note"], "分镜与包装")
        self.assertEqual(account["defaultModel"], "ep-example")
        self.assertIsNone(account["channelId"])
        self.assertNotIn("ark-never-return", json.dumps(account, ensure_ascii=False))

        status, _, profile = self.request(
            "POST",
            "/api/settings/model-profiles",
            {
                "profileId": "storyboard-direct",
                "capability": "storyboard",
                "displayName": "方舟分镜",
                "accountId": "ark-direct",
                "model": "ep-example",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(profile["settings"]["protocol"], "openai")

    def test_settings_can_fetch_provider_models_without_exposing_credentials(self):
        from workbench import api as workbench_api

        with patch.object(
            workbench_api,
            "save_provider_secret",
            return_value="cutroom/provider/catalog-direct",
        ):
            status, _, _account = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "catalog-direct",
                    "name": "模型目录直连",
                    "preset": "openai_compatible",
                    "baseUrl": "https://models.example/v1",
                    "secret": "catalog-never-return",
                },
            )
        self.assertEqual(status, 200)

        with patch.object(
            workbench_api,
            "list_provider_models",
            return_value=["model-a", "model-b"],
        ) as discover:
            status, _, payload = self.request(
                "POST",
                "/api/settings/provider-accounts/catalog-direct/models",
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"models": ["model-a", "model-b"]})
        discover.assert_called_once()
        self.assertNotIn(
            "catalog-never-return",
            json.dumps(payload, ensure_ascii=False),
        )

    def test_doubao_voice_routes_use_cached_catalog_and_server_validation(self):
        from media_catalog.model_profiles import create_provider_account
        from media_catalog.tts_voice_catalog import replace_provider_voices
        from workbench import api as workbench_api

        with CatalogDatabase.open(self.db_path) as db:
            create_provider_account(
                db,
                account_id="voice-account",
                name="豆包可选音色",
                provider="doubao_tts",
                base_url="https://openspeech.bytedance.com/api/v1/tts",
                credential_ref="cutroom/provider/voice-account",
                settings={
                    "preset": "doubao_tts",
                    "protocol": "volcengine_tts",
                    "app_id": "app-id",
                    "tts_resource_id": "seed-tts-2.0",
                },
            )
            replace_provider_voices(
                db,
                "voice-account",
                "seed-tts-2.0",
                [
                    {
                        "voiceId": "voice-a",
                        "displayName": "阳光青年 2.0",
                        "resourceId": "seed-tts-2.0",
                    },
                    {
                        "voiceId": "voice-b",
                        "displayName": "灿灿 2.0",
                        "resourceId": "seed-tts-2.0",
                    },
                ],
            )

        with patch.object(
            workbench_api,
            "refresh_provider_voice_catalog",
        ) as refresh:
            status, _, cached = self.request(
                "GET",
                "/api/settings/provider-accounts/voice-account/voices",
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            [voice["displayName"] for voice in cached["voices"]],
            ["阳光青年 2.0", "灿灿 2.0"],
        )
        self.assertEqual(cached["state"]["status"], "success")
        refresh.assert_not_called()

        refreshed = {"voices": cached["voices"], "state": cached["state"]}
        with patch.object(
            workbench_api,
            "refresh_provider_voice_catalog",
            return_value=refreshed,
        ) as refresh:
            status, _, payload = self.request(
                "POST",
                "/api/settings/provider-accounts/voice-account/voices/refresh",
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload, refreshed)
        refresh.assert_called_once()

        status, _, profile = self.request(
            "POST",
            "/api/settings/model-profiles",
            {
                "profileId": "catalog-tts",
                "capability": "tts",
                "displayName": "豆包配音服务",
                "accountId": "voice-account",
                "resourceId": "seed-tts-2.0",
                "defaultVoiceId": "voice-a",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(profile["model"], "seed-tts-2.0")
        self.assertEqual(profile["settings"]["resource_id"], "seed-tts-2.0")
        self.assertEqual(profile["settings"]["default_voice_id"], "voice-a")
        self.assertEqual(profile["settings"]["voice"], "voice-a")

        status, _, choices = self.request(
            "GET", "/api/settings/model-profiles/catalog-tts/voices"
        )
        self.assertEqual(status, 200)
        self.assertEqual(choices["defaultVoiceId"], "voice-a")
        self.assertEqual(
            [voice["voiceId"] for voice in choices["voices"]],
            ["voice-a", "voice-b"],
        )

        frozen_snapshot = '{"profileSettings":{"voice":"voice-a"}}'
        with CatalogDatabase.open(self.db_path) as db:
            create_task(
                db,
                "already-bound-task",
                "已经创建的任务",
                "ad_mix",
            )
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO task_model_bindings("
                    "task_id,capability,profile_id,snapshot_json,snapshot_digest"
                    ") VALUES(?,?,?,?,?)",
                    (
                        "already-bound-task",
                        "tts",
                        "catalog-tts",
                        frozen_snapshot,
                        "frozen-before-default-change",
                    ),
                )

        status, _, updated = self.request(
            "PUT",
            "/api/settings/model-profiles/catalog-tts/default-voice",
            {"voiceId": "voice-b"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["settings"]["default_voice_id"], "voice-b")
        self.assertEqual(updated["settings"]["voice"], "voice-b")
        status, _, choices = self.request(
            "GET", "/api/settings/model-profiles/catalog-tts/voices"
        )
        self.assertEqual(status, 200)
        self.assertEqual(choices["defaultVoiceId"], "voice-b")
        with CatalogDatabase.open(self.db_path) as db:
            frozen = db.connection.execute(
                "SELECT snapshot_json,snapshot_digest "
                "FROM task_model_bindings "
                "WHERE task_id='already-bound-task' AND capability='tts'"
            ).fetchone()
        self.assertEqual(
            tuple(frozen),
            (frozen_snapshot, "frozen-before-default-change"),
        )

        status, _, invalid_update = self.request(
            "PUT",
            "/api/settings/model-profiles/catalog-tts/default-voice",
            {"voiceId": "not-in-catalog"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            invalid_update["error"]["code"],
            "VOICE_NOT_SELECTABLE",
        )

        status, _, invalid = self.request(
            "POST",
            "/api/settings/model-profiles",
            {
                "profileId": "forged-tts",
                "capability": "tts",
                "displayName": "伪造音色",
                "accountId": "voice-account",
                "resourceId": "seed-tts-2.0",
                "defaultVoiceId": "not-in-catalog",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "VOICE_NOT_SELECTABLE")

        with patch.object(
            workbench_api,
            "create_tts_voice_preview",
            return_value={
                "voiceId": "voice-b",
                "previewUrl": "/api/voice-previews/a1/b2.mp3",
            },
        ) as preview:
            status, _, tested = self.request(
                "POST",
                "/api/settings/model-profiles/catalog-tts/voice-test",
                {"voiceId": "voice-b"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            tested["previewUrl"],
            "/api/voice-previews/a1/b2.mp3",
        )
        preview.assert_called_once()

    def test_provider_account_can_be_deleted_when_unused(self):
        from workbench import api as workbench_api

        with patch.object(
            workbench_api,
            "save_provider_secret",
            return_value="cutroom/provider/delete-me",
        ):
            status, _, _account = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "delete-me",
                    "name": "重复智谱账号",
                    "preset": "openai_compatible",
                    "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
                    "secret": "never-return-this",
                },
            )
        self.assertEqual(status, 200)

        with patch.object(workbench_api, "delete_provider_secret") as delete_secret:
            status, _, payload = self.request(
                "DELETE", "/api/settings/provider-accounts/delete-me"
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"accountId": "delete-me", "deleted": True})
        delete_secret.assert_called_once_with("cutroom/provider/delete-me")
        status, _, settings = self.request("GET", "/api/settings/model-profiles")
        self.assertEqual(status, 200)
        self.assertNotIn(
            "delete-me",
            [item["accountId"] for item in settings["accounts"]],
        )

    def test_provider_account_delete_is_blocked_while_profiles_use_it(self):
        from workbench import api as workbench_api

        with patch.object(
            workbench_api,
            "save_provider_secret",
            return_value="cutroom/provider/in-use",
        ):
            status, _, _account = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "in-use",
                    "name": "正在使用的智谱",
                    "preset": "openai_compatible",
                    "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
                    "secret": "never-return-this",
                },
            )
        self.assertEqual(status, 200)
        status, _, _profile = self.request(
            "POST",
            "/api/settings/model-profiles",
            {
                "profileId": "in-use-copywriting",
                "capability": "copywriting",
                "displayName": "智谱文案",
                "accountId": "in-use",
                "model": "glm-5.3",
            },
        )
        self.assertEqual(status, 200)

        with patch.object(workbench_api, "delete_provider_secret") as delete_secret:
            status, _, payload = self.request(
                "DELETE", "/api/settings/provider-accounts/in-use"
            )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "PROVIDER_ACCOUNT_IN_USE")
        delete_secret.assert_not_called()

    def test_model_profile_can_be_deleted_when_unused(self):
        from workbench import api as workbench_api

        with patch.object(
            workbench_api,
            "save_provider_secret",
            return_value="cutroom/provider/delete-profile-account",
        ):
            status, _, _account = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "delete-profile-account",
                    "name": "可清理模型账号",
                    "preset": "openai_compatible",
                    "baseUrl": "https://example.invalid/v1",
                    "secret": "never-return-this",
                },
            )
        self.assertEqual(status, 200)
        status, _, _profile = self.request(
            "POST",
            "/api/settings/model-profiles",
            {
                "profileId": "delete-profile",
                "capability": "planning",
                "displayName": "待删除策划模型",
                "accountId": "delete-profile-account",
                "model": "model-delete",
            },
        )
        self.assertEqual(status, 200)

        status, _, payload = self.request(
            "DELETE",
            "/api/settings/model-profiles/delete-profile",
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {"profileId": "delete-profile", "deleted": True},
        )

    def test_model_probe_failure_returns_actionable_safe_error(self):
        from media_catalog.model_profile_probe import ModelProfileProbeError
        from media_catalog.model_profiles import mark_profile_check
        from workbench import api as workbench_api

        with patch.object(
            workbench_api,
            "save_provider_secret",
            return_value="cutroom/provider/probe-error-account",
        ):
            status, _, _account = self.request(
                "POST",
                "/api/settings/provider-accounts",
                {
                    "accountId": "probe-error-account",
                    "name": "检测失败账号",
                    "preset": "openai_compatible",
                    "baseUrl": "https://example.invalid/v4",
                    "secret": "never-return-this",
                },
            )
        self.assertEqual(status, 200)
        status, _, _profile = self.request(
            "POST",
            "/api/settings/model-profiles",
            {
                "profileId": "probe-error-profile",
                "capability": "planning",
                "displayName": "检测失败模型",
                "accountId": "probe-error-account",
                "model": "missing-model",
            },
        )
        self.assertEqual(status, 200)

        def fail_probe(db, profile_id):
            mark_profile_check(
                db,
                profile_id,
                available=False,
                error_code="MODEL_OR_ENDPOINT_NOT_FOUND",
                error_message="请求地址或模型不存在",
            )
            raise ModelProfileProbeError("模型连接测试失败")

        with patch.object(workbench_api, "probe_model_profile", side_effect=fail_probe):
            status, _, payload = self.request(
                "POST",
                "/api/settings/model-profiles/probe-error-profile/test",
            )

        self.assertEqual(status, 422)
        self.assertEqual(
            payload["error"]["code"],
            "MODEL_OR_ENDPOINT_NOT_FOUND",
        )
        self.assertEqual(
            payload["error"]["details"]["lastErrorMessage"],
            "请求地址或模型不存在",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("credential", serialized.lower())
        self.assertNotIn("never-return-this", serialized)


if __name__ == "__main__":
    unittest.main()
