import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog.candidate_versions import ensure_candidate_version, record_candidate_qa
from media_catalog.database import CatalogDatabase
from media_catalog.full_review import approve_after_full_watch
from media_catalog.publish import build_publish_package, list_publish
from media_catalog.rework import (
    execute_rework,
    get_rework_request,
    list_rework_requests,
    preview_rework_impact,
    reject_with_rework,
)
from media_catalog.snapshots import (
    record_qa_review_snapshot,
    record_render_dependency_snapshot,
)
from media_catalog.workbench_actions import _render_input_digest
from media_catalog.workflow import WORKFLOW_STAGES, project_workflow, seed_stage_states
from tests.review_helpers import approve_review, complete_watch_session


class ReworkRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "candidate.mp4"
        self.video.write_bytes(b"trusted-video")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,title,status) VALUES('task-1','Task','draft')"
            )
        render = record_render_dependency_snapshot(
            self.db,
            "task-1",
            {
                "output_path": str(self.video),
                "dependency_digest": "digest-1",
                "dependency_snapshot": {"files": []},
                "plan_path": str(self.root / "plan.json"),
                "ass_path": None,
                "packaging": {"plan_id": "plan-1"},
            },
        )
        qa = {
            "report_id": "qa-1",
            "task_id": "task-1",
            "video_path": str(self.video.resolve()),
            "status": "passed",
            "issue_count": 0,
            "blocking_count": 0,
            "issues": [],
            "video_spec": {"duration": 12.0},
        }
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO qa_reports(
                  report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                ) VALUES(?,?,?,?,?,?,?)""",
                ("qa-1", "task-1", str(self.video.resolve()), "passed", 0, 0,
                 json.dumps(qa)),
            )
        qa_snapshot = record_qa_review_snapshot(self.db, "task-1", render, qa)
        self.candidate = record_candidate_qa(
            self.db,
            "task-1",
            render,
            qa,
            qa_snapshot_id=qa_snapshot["qa_snapshot_id"],
        )
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def reject(self, *, category="subtitle", key="reject-1"):
        return reject_with_rework(
            self.db,
            "task-1",
            self.candidate["candidate_id"],
            reviewed_by="Peter",
            category=category,
            notes="字幕慢半拍",
            position_seconds=12.5,
            idempotency_key=key,
        )

    def stage_states(self):
        return {
            row["stage_key"]: row["status"]
            for row in self.db.connection.execute(
                "SELECT stage_key,status FROM task_stage_states WHERE task_id='task-1'"
            )
        }

    def test_categories_map_to_smallest_safe_boundary(self):
        expected = {
            "script": "brief_lock",
            "material": "ai_storyboard",
            "rhythm": "rough_cut",
            "subtitle": "packaging_confirm",
            "audio": "packaging_confirm",
            "packaging": "packaging_confirm",
            "technical": "fine_render",
        }
        self.assertEqual(
            {key: preview_rework_impact(key)["target_stage"] for key in expected},
            expected,
        )
        self.assertEqual(
            preview_rework_impact("technical", technical_scope="qa_only")["target_stage"],
            "auto_qa",
        )

    def test_reject_creates_decision_and_rework_atomically(self):
        result = self.reject()
        self.assertEqual(result["review"]["decision"], "rejected")
        self.assertEqual(result["rework"]["status"], "pending_confirmation")
        self.assertEqual(result["rework"]["target_stage"], "packaging_confirm")
        self.assertEqual(result["rework"]["source_candidate_id"], self.candidate["candidate_id"])
        self.assertEqual(
            get_rework_request(self.db, "task-1", result["rework"]["rework_id"])["notes"],
            "字幕慢半拍",
        )
        self.assertEqual(len(list_rework_requests(self.db, "task-1")), 1)
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM review_approvals").fetchone()[0],
            1,
        )

    def test_reject_idempotency_replays_without_duplicate_review(self):
        first = self.reject()
        replay = self.reject()
        self.assertEqual(first["rework"]["rework_id"], replay["rework"]["rework_id"])
        self.assertEqual(first["review"]["approval_id"], replay["review"]["approval_id"])
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM review_approvals").fetchone()[0],
            1,
        )

    def test_execute_is_idempotent_and_invalidates_only_downstream(self):
        before_digest = _render_input_digest(self.db, "task-1")
        rework_id = self.reject()["rework"]["rework_id"]
        first = execute_rework(self.db, "task-1", rework_id, idempotency_key="run-1")
        replay = execute_rework(self.db, "task-1", rework_id, idempotency_key="run-1")
        self.assertEqual(first["rework_id"], replay["rework_id"])
        states = self.stage_states()
        self.assertEqual(states["packaging_confirm"], "ready")
        self.assertEqual(states["fine_render"], "invalidated")
        self.assertEqual(states["picture_lock"], "completed")
        self.assertEqual(
            get_rework_request(self.db, "task-1", rework_id)["status"], "applied"
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT status FROM candidate_versions WHERE candidate_id=?",
                (self.candidate["candidate_id"],),
            ).fetchone()[0],
            "rejected",
        )
        with self.assertRaisesRegex(RuntimeError, "REWORK_ALREADY_APPLIED"):
            execute_rework(self.db, "task-1", rework_id, idempotency_key="run-2")
        self.assertNotEqual(before_digest, _render_input_digest(self.db, "task-1"))

    def test_applied_rework_cannot_be_reapproved_with_old_watch_or_trusted_facts(self):
        watch = complete_watch_session(
            self.db,
            "task-1",
            self.candidate["qa_snapshot_id"],
            self.candidate["qa_report_id"],
        )
        approve_after_full_watch(
            self.db,
            "task-1",
            self.candidate["qa_snapshot_id"],
            self.candidate["qa_report_id"],
            watch,
            "Peter",
        )
        rework_id = self.reject(key="reject-approved")["rework"]["rework_id"]
        execute_rework(self.db, "task-1", rework_id, idempotency_key="run-approved")

        workflow = project_workflow(self.db, "task-1")
        states = {item["stage_key"]: item["status"] for item in workflow["stages"]}
        self.assertEqual(workflow["current_stage"], "packaging_confirm")
        self.assertEqual(states["packaging_confirm"], "ready")
        self.assertFalse(workflow["gates"]["packaging_confirmed"])
        self.assertFalse(workflow["gates"]["human_reviewed"])
        with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
            approve_after_full_watch(
                self.db,
                "task-1",
                self.candidate["qa_snapshot_id"],
                self.candidate["qa_report_id"],
                watch,
                "Peter",
            )

    def test_unexecuted_rework_is_stale_after_a_new_candidate_exists(self):
        rework_id = self.reject(key="stale-v1")["rework"]["rework_id"]
        second = ensure_candidate_version(
            self.db,
            "task-1",
            self.mix_for_next_candidate("candidate-v2", "digest-v2"),
        )
        self.assertNotEqual(second["candidate_id"], self.candidate["candidate_id"])
        with self.assertRaisesRegex(RuntimeError, "REWORK_SOURCE_STALE"):
            execute_rework(self.db, "task-1", rework_id, idempotency_key="run-stale")

    def test_blocked_candidate_can_record_rework_without_fake_qa_snapshot(self):
        render = self.mix_for_next_candidate("candidate-blocked", "digest-blocked")
        report = {
            "report_id": "qa-blocked",
            "task_id": "task-1",
            "video_path": render["output_path"],
            "status": "blocked",
            "issue_count": 1,
            "blocking_count": 1,
            "issues": [{"severity": "blocking", "check": "black", "detail": "黑帧"}],
            "video_spec": {"duration": 12.0},
        }
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO qa_reports(
                  report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    report["report_id"], "task-1", report["video_path"], "blocked",
                    1, 1, json.dumps(report, ensure_ascii=False),
                ),
            )
        blocked = record_candidate_qa(
            self.db, "task-1", render, report, eligible_for_review=False
        )

        result = reject_with_rework(
            self.db,
            "task-1",
            blocked["candidate_id"],
            reviewed_by="Peter",
            category="technical",
            notes="黑帧阻断",
            qa_issue_ref="black",
            idempotency_key="blocked-rework",
        )

        self.assertEqual(result["review"]["snapshot_id"], blocked["mix_snapshot_id"])
        self.assertEqual(result["review"]["qa_report_id"], blocked["qa_report_id"])
        self.assertIsNone(blocked["qa_snapshot_id"])

    def mix_for_next_candidate(self, name: str, digest: str) -> dict:
        video = self.root / f"{name}.mp4"
        video.write_bytes(name.encode("utf-8"))
        return record_render_dependency_snapshot(
            self.db,
            "task-1",
            {
                "output_path": str(video),
                "dependency_digest": digest,
                "dependency_snapshot": {"files": []},
                "plan_path": str(self.root / f"{name}.json"),
                "ass_path": None,
                "packaging": {"plan_id": f"plan-{name}"},
            },
        )

    def test_next_candidate_claims_applied_rework_lineage_once(self):
        rework_id = self.reject()["rework"]["rework_id"]
        execute_rework(self.db, "task-1", rework_id, idempotency_key="run-lineage")
        next_video = self.root / "candidate-v2.mp4"
        next_video.write_bytes(b"trusted-video-v2")
        next_render = record_render_dependency_snapshot(
            self.db,
            "task-1",
            {
                "output_path": str(next_video),
                "dependency_digest": "digest-2",
                "dependency_snapshot": {"files": []},
                "plan_path": str(self.root / "plan-v2.json"),
                "ass_path": None,
                "packaging": {"plan_id": "plan-2"},
            },
        )

        second = ensure_candidate_version(self.db, "task-1", next_render)
        replay = ensure_candidate_version(self.db, "task-1", next_render)

        self.assertEqual(second["source_candidate_id"], self.candidate["candidate_id"])
        self.assertEqual(second["rework_request_id"], rework_id)
        self.assertEqual(second["candidate_id"], replay["candidate_id"])
        self.assertEqual(
            get_rework_request(self.db, "task-1", rework_id)["result_candidate_id"],
            second["candidate_id"],
        )

    def test_costly_rework_requires_explicit_approval(self):
        rework = self.reject(category="script", key="script-reject")["rework"]
        self.assertEqual(rework["cost_status"], "confirmation_required")
        with self.assertRaisesRegex(RuntimeError, "REWORK_COST_CONFIRMATION_REQUIRED"):
            execute_rework(
                self.db, "task-1", rework["rework_id"], idempotency_key="run-script"
            )
        self.assertEqual(self.stage_states(), {stage: "completed" for stage in WORKFLOW_STAGES})

    def test_confirmed_rework_expires_but_does_not_delete_old_package(self):
        approve_review(
            self.db,
            "task-1",
            self.candidate["qa_snapshot_id"],
            self.candidate["qa_report_id"],
            "Peter",
        )

        def fake_ffmpeg(command, **_kwargs):
            Path(command[-1]).write_bytes(b"jpeg")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with (
            patch("media_catalog.publish.DELIVER_ROOT", self.root / "deliverables"),
            patch("media_catalog.publish.find_ffmpeg", return_value=Path("ffmpeg")),
            patch("media_catalog.publish.subprocess.run", side_effect=fake_ffmpeg),
        ):
            package = build_publish_package(
                self.db,
                "task-1",
                "视频号",
                video_path=self.video,
                qa_report={"report_id": self.candidate["qa_report_id"]},
            )

        rework_id = self.reject(key="expire-package")["rework"]["rework_id"]
        execute_rework(
            self.db, "task-1", rework_id, idempotency_key="execute-expiry"
        )
        listed = list_publish(self.db, "task-1")
        self.assertEqual(listed[0]["record_id"], package["record_id"])
        self.assertEqual(listed[0]["package_status"], "expired")
        self.assertFalse(listed[0]["is_current"])
        self.assertTrue(all(Path(path).is_file() for path in listed[0]["files"]))


if __name__ == "__main__":
    unittest.main()
