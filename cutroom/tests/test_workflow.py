import json
import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.reviews import reject_review
from media_catalog.snapshots import file_content_signature
from media_catalog.workflow import (
    WORKFLOW_STAGES,
    complete_stage,
    invalidate_from,
    project_gates,
    project_workflow,
    seed_stage_states,
)
from tests.review_helpers import approve_review


EXPECTED_STAGES = (
    "create_preflight",
    "brief_lock",
    "material_sync",
    "ai_storyboard",
    "rough_cut",
    "picture_order",
    "picture_lock",
    "packaging_confirm",
    "fine_render",
    "auto_qa",
    "full_review",
    "publish_package",
)
VALID_STAGE_STATUSES = {
    "not_started",
    "ready",
    "in_progress",
    "completed",
    "invalidated",
    "blocked",
}


class WorkflowProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase.open(Path(self.tmp.name) / "catalog.sqlite")
        self.db.migrate()
        self._create_task("task-1")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _create_task(self, task_id, status="draft"):
        self.db.connection.execute(
            "INSERT INTO tasks(task_id, title, status) VALUES(?, ?, ?)",
            (task_id, f"Workflow {task_id}", status),
        )
        self.db.connection.commit()

    def _insert_gate(self, task_id, gate_type, suffix="1"):
        self.db.connection.execute(
            """
            INSERT INTO task_gate_events(
              event_id, task_id, gate_type, gate_digest, details_json
            ) VALUES(?, ?, ?, ?, '{}')
            """,
            (f"gate-{task_id}-{gate_type}-{suffix}", task_id, gate_type, f"digest-{suffix}"),
        )

    def _insert_brief(self, task_id="task-1"):
        self._insert_gate(task_id, "brief")
        self.db.connection.commit()

    def _insert_picture_lock(self, task_id="task-1"):
        self.db.connection.execute(
            """
            INSERT INTO scene_cards(card_id, task_id, idx, confirm_status)
            VALUES(?, ?, 0, 1)
            """,
            (f"card-{task_id}", task_id),
        )
        self.db.connection.execute(
            """
            INSERT INTO picture_lock(
              task_id, lock_version, changed_since_lock, selection_digest
            ) VALUES(?, 'v1', 0, 'selection-1')
            """,
            (task_id,),
        )
        self._insert_gate(task_id, "picture_lock")
        self.db.connection.commit()

    def _insert_packaging(self, task_id="task-1"):
        self.db.connection.execute(
            """
            INSERT INTO packaging_plans(
              plan_id, task_id, route, plan_name, status, confirmed_at
            ) VALUES(?, ?, '数字人混剪', '方案A', 'confirmed', '2026-09-05 10:00:00')
            """,
            (f"plan-{task_id}", task_id),
        )
        self._insert_gate(task_id, "packaging")
        self.db.connection.commit()

    def _insert_trusted_candidate(self, task_id="task-1"):
        from media_catalog.candidate_versions import record_candidate_qa

        output = Path(self.tmp.name) / f"{task_id}.mp4"
        output.write_bytes(b"trusted-workflow-video")
        output_path = str(output)
        signature = file_content_signature(output)
        mix_params = {
            "dependency_digest": "dependency-1",
            "dependency_snapshot": {"source": "catalog"},
            "packaging": {"plan_id": f"plan-{task_id}"},
            "output_signature": signature,
        }
        qa_params = {
            "qa_report_id": f"qa-{task_id}",
            "dependency_digest": "dependency-1",
            "output_signature": signature,
        }
        self.db.connection.execute(
            """
            INSERT INTO render_snapshots(
              snapshot_id, task_id, stage, params_json, output_path,
              producer, input_digest
            ) VALUES(?, ?, 'mix', ?, ?, 'assemble-v1', 'dependency-1')
            """,
            (f"mix-{task_id}", task_id, json.dumps(mix_params), output_path),
        )
        self.db.connection.execute(
            """
            INSERT INTO qa_reports(
              report_id, task_id, video_path, status, report_json
            ) VALUES(?, ?, ?, 'passed', ?)
            """,
            (
                f"qa-{task_id}",
                task_id,
                output_path,
                json.dumps({"video_spec": {"duration": 10.0}}),
            ),
        )
        self.db.connection.execute(
            """
            INSERT INTO render_snapshots(
              snapshot_id, task_id, stage, params_json, output_path,
              parent_snapshot, producer, input_digest
            ) VALUES(?, ?, 'qa', ?, ?, ?, 'assemble-v1', 'dependency-1')
            """,
            (
                f"qa-snapshot-{task_id}",
                task_id,
                json.dumps(qa_params),
                output_path,
                f"mix-{task_id}",
            ),
        )
        self.db.connection.commit()
        record_candidate_qa(
            self.db,
            task_id,
            {
                "mix_snapshot_id": f"mix-{task_id}",
                "dependency_digest": "dependency-1",
                "output_path": output_path,
                "output_signature": signature,
            },
            {
                "report_id": f"qa-{task_id}",
                "status": "passed",
                "blocking_count": 0,
            },
            qa_snapshot_id=f"qa-snapshot-{task_id}",
        )

    def _insert_approval(self, task_id="task-1", decision="approved", suffix="1"):
        if decision == "approved":
            approve_review(
                self.db,
                task_id,
                f"qa-snapshot-{task_id}",
                f"qa-{task_id}",
                "Peter",
                "完整观看通过",
            )
        else:
            reject_review(
                self.db,
                task_id,
                f"qa-snapshot-{task_id}",
                f"qa-{task_id}",
                "Peter",
                "发现需要修改的问题",
            )

    def _insert_publish(self, task_id="task-1", status="cleared", *, legacy=False):
        candidate = self.db.connection.execute(
            "SELECT candidate_id FROM candidate_versions WHERE task_id=? ORDER BY version_no DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        self.db.connection.execute(
            """
            INSERT INTO publish_records(
              record_id, task_id, platform, status, candidate_id, package_status
            ) VALUES(?, ?, '抖音', ?, ?, 'current')
            """,
            (
                f"publish-{task_id}",
                task_id,
                status,
                None if legacy or candidate is None else candidate["candidate_id"],
            ),
        )
        self.db.connection.commit()

    def _insert_qa(
        self, task_id="task-1", *, status="passed", created_at, suffix="1"
    ):
        self.db.connection.execute(
            """
            INSERT INTO qa_reports(
              report_id, task_id, video_path, status, report_json, created_at
            ) VALUES(?, ?, ?, ?, '{}', ?)
            """,
            (
                f"qa-{task_id}-{suffix}",
                task_id,
                f"{task_id}-{suffix}.mp4",
                status,
                created_at,
            ),
        )
        self.db.connection.commit()

    def _stage_statuses(self, task_id="task-1"):
        return {
            item["stage_key"]: item["status"]
            for item in project_workflow(self.db, task_id)["stages"]
        }

    def test_missing_task_is_rejected(self):
        with self.assertRaises(KeyError):
            project_workflow(self.db, "missing")
        with self.assertRaises(KeyError):
            project_gates(self.db, "missing")

    def test_new_task_projects_stable_twelve_stage_contract(self):
        result = project_workflow(self.db, "task-1")

        self.assertEqual(
            set(result),
            {"task_id", "current_stage", "run_status", "next_action", "gates", "stages"},
        )
        self.assertEqual(WORKFLOW_STAGES, EXPECTED_STAGES)
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["current_stage"], "create_preflight")
        self.assertEqual(result["run_status"], "ready")
        self.assertEqual(result["next_action"], "complete_preflight")
        self.assertEqual(
            [stage["stage_key"] for stage in result["stages"]],
            list(EXPECTED_STAGES),
        )
        self.assertTrue(
            {stage["status"] for stage in result["stages"]}
            <= VALID_STAGE_STATUSES
        )
        self.assertEqual(
            set(result["gates"]),
            {
                "brief_locked",
                "picture_lock",
                "packaging_confirmed",
                "qa_passed",
                "human_reviewed",
            },
        )
        self.assertIs(result["gates"].get("qa_passed"), False)

    def test_qa_gate_is_false_without_report(self):
        self.assertIs(project_gates(self.db, "task-1").get("qa_passed"), False)

    def test_qa_gate_is_true_when_latest_report_passed(self):
        self._insert_qa(
            status="passed", created_at="2026-09-05 10:00:00"
        )

        self.assertIs(project_gates(self.db, "task-1").get("qa_passed"), True)

    def test_newer_blocked_qa_overrides_older_passed_report(self):
        self._insert_qa(
            status="passed", created_at="2026-09-05 10:00:00", suffix="old"
        )
        self._insert_qa(
            status="blocked", created_at="2026-09-05 11:00:00", suffix="new"
        )

        self.assertIs(project_gates(self.db, "task-1").get("qa_passed"), False)

    def test_qa_gate_breaks_created_at_ties_by_latest_row(self):
        timestamp = "2026-09-05 10:00:00"
        self._insert_qa(status="passed", created_at=timestamp, suffix="first")
        self._insert_qa(status="blocked", created_at=timestamp, suffix="second")

        self.assertIs(project_gates(self.db, "task-1").get("qa_passed"), False)

    def test_seven_trusted_progress_states(self):
        expected = [
            ("create_preflight", "ready", "complete_preflight"),
            ("material_sync", "ready", "sync_materials"),
            ("packaging_confirm", "awaiting_user", "confirm_packaging"),
            ("fine_render", "ready", "render_fine_cut"),
            ("full_review", "awaiting_user", "complete_review"),
            ("publish_package", "ready", "build_publish_package"),
            (None, "completed", None),
        ]

        projections = [project_workflow(self.db, "task-1")]
        self._insert_brief()
        projections.append(project_workflow(self.db, "task-1"))
        self._insert_picture_lock()
        projections.append(project_workflow(self.db, "task-1"))
        self._insert_packaging()
        projections.append(project_workflow(self.db, "task-1"))
        self._insert_trusted_candidate()
        projections.append(project_workflow(self.db, "task-1"))
        self._insert_approval()
        projections.append(project_workflow(self.db, "task-1"))
        self._insert_publish()
        projections.append(project_workflow(self.db, "task-1"))

        self.assertEqual(
            [
                (item["current_stage"], item["run_status"], item["next_action"])
                for item in projections
            ],
            expected,
        )

    def test_legacy_publish_record_does_not_complete_publish_without_current_approval(self):
        self._insert_brief()
        self._insert_picture_lock()
        self._insert_packaging()
        self._insert_trusted_candidate()
        self._insert_publish(legacy=True)

        result = project_workflow(self.db, "task-1")
        statuses = {
            item["stage_key"]: item["status"] for item in result["stages"]
        }

        self.assertFalse(result["gates"]["human_reviewed"])
        self.assertEqual(result["current_stage"], "full_review")
        self.assertNotEqual(statuses["publish_package"], "completed")

    def test_gate_projection_requires_trusted_facts_and_latest_approval(self):
        self.db.connection.execute(
            "UPDATE tasks SET status='reviewed' WHERE task_id='task-1'"
        )
        self.db.connection.execute(
            """
            INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
            VALUES('task-1', 'v1', 0)
            """
        )
        self.db.connection.commit()
        self.assertEqual(
            project_gates(self.db, "task-1"),
            {
                "brief_locked": False,
                "picture_lock": False,
                "packaging_confirmed": False,
                "qa_passed": False,
                "human_reviewed": False,
            },
        )
        self.assertEqual(
            project_workflow(self.db, "task-1")["current_stage"],
            "create_preflight",
        )

        self.db.connection.execute("DELETE FROM picture_lock WHERE task_id='task-1'")
        self._insert_brief()
        self._insert_picture_lock()
        self._insert_packaging()
        self._insert_trusted_candidate()
        self._insert_approval(decision="approved", suffix="1")
        self._insert_approval(decision="rejected", suffix="2")
        gates = project_gates(self.db, "task-1")
        self.assertTrue(gates["brief_locked"])
        self.assertTrue(gates["picture_lock"])
        self.assertTrue(gates["packaging_confirmed"])
        self.assertFalse(gates["human_reviewed"])

        self.db.connection.execute(
            "UPDATE picture_lock SET changed_since_lock=1 WHERE task_id='task-1'"
        )
        self.db.connection.commit()
        self.assertFalse(project_gates(self.db, "task-1")["picture_lock"])

    def test_packaging_gate_requires_confirmed_plan_and_gate_event(self):
        for task_id in ("plan-only", "gate-only", "both-evidence"):
            self._create_task(task_id)
        for task_id in ("plan-only", "both-evidence"):
            self.db.connection.execute(
                """
                INSERT INTO packaging_plans(
                  plan_id, task_id, route, plan_name, status, confirmed_at
                ) VALUES(?, ?, '数字人混剪', '方案A', 'confirmed',
                         '2026-09-05 10:00:00')
                """,
                (f"plan-{task_id}", task_id),
            )
        for task_id in ("gate-only", "both-evidence"):
            self._insert_gate(task_id, "packaging")
        self.db.connection.commit()

        self.assertFalse(project_gates(self.db, "plan-only")["packaging_confirmed"])
        self.assertFalse(project_gates(self.db, "gate-only")["packaging_confirmed"])
        self.assertTrue(project_gates(self.db, "both-evidence")["packaging_confirmed"])

    def test_specialized_facts_override_generic_state_without_frontend_backfill(self):
        seed_stage_states(self.db, "task-1")
        self.db.connection.execute(
            """
            UPDATE task_stage_states SET status='blocked'
            WHERE task_id='task-1' AND stage_key='picture_lock'
            """
        )
        self.db.connection.commit()
        self._insert_brief()
        self._insert_picture_lock()

        statuses = self._stage_statuses()
        self.assertEqual(statuses["ai_storyboard"], "completed")
        self.assertEqual(statuses["picture_lock"], "completed")

    def test_seed_is_idempotent_and_preserves_existing_state(self):
        seed_stage_states(self.db, "task-1")
        complete_stage(
            self.db,
            "task-1",
            "create_preflight",
            input_digest="input-1",
            details={"ok": True},
        )
        seed_stage_states(self.db, "task-1")

        rows = self.db.connection.execute(
            """
            SELECT stage_key, status FROM task_stage_states
            WHERE task_id='task-1' ORDER BY rowid
            """
        ).fetchall()
        self.assertEqual(len(rows), 12)
        self.assertEqual({row["stage_key"] for row in rows}, set(EXPECTED_STAGES))
        self.assertEqual(rows[0]["status"], "completed")

    def test_complete_stage_writes_canonical_json_and_readies_next_stage(self):
        complete_stage(
            self.db,
            "task-1",
            "brief_lock",
            input_digest="digest-中文",
            details={"z": 2, "a": "值"},
        )
        row = self.db.connection.execute(
            """
            SELECT status, detail_json FROM task_stage_states
            WHERE task_id='task-1' AND stage_key='brief_lock'
            """
        ).fetchone()
        next_row = self.db.connection.execute(
            """
            SELECT status FROM task_stage_states
            WHERE task_id='task-1' AND stage_key='material_sync'
            """
        ).fetchone()

        self.assertEqual(row["status"], "completed")
        self.assertEqual(
            row["detail_json"],
            '{"details":{"a":"值","z":2},"input_digest":"digest-中文"}',
        )
        self.assertEqual(next_row["status"], "ready")

    def test_complete_stage_is_idempotent_until_digest_or_details_change(self):
        original = complete_stage(
            self.db,
            "task-1",
            "brief_lock",
            input_digest="digest-1",
            details={"version": 1},
        )
        original_json = original["detail_json"]
        frozen_time = "2000-01-01 00:00:00"
        self.db.connection.execute(
            """
            UPDATE task_stage_states SET updated_at=?
            WHERE task_id='task-1' AND stage_key='brief_lock'
            """,
            (frozen_time,),
        )
        self.db.connection.commit()

        repeated = complete_stage(
            self.db,
            "task-1",
            "brief_lock",
            input_digest="digest-1",
            details={"version": 1},
        )
        self.assertEqual(repeated["detail_json"], original_json)
        self.assertEqual(repeated["updated_at"], frozen_time)

        details_changed = complete_stage(
            self.db,
            "task-1",
            "brief_lock",
            input_digest="digest-1",
            details={"version": 2},
        )
        self.assertNotEqual(details_changed["detail_json"], original_json)
        self.assertNotEqual(details_changed["updated_at"], frozen_time)

        self.db.connection.execute(
            """
            UPDATE task_stage_states SET updated_at=?
            WHERE task_id='task-1' AND stage_key='brief_lock'
            """,
            (frozen_time,),
        )
        self.db.connection.commit()
        digest_changed = complete_stage(
            self.db,
            "task-1",
            "brief_lock",
            input_digest="digest-2",
            details={"version": 2},
        )
        self.assertNotEqual(digest_changed["updated_at"], frozen_time)

    def test_invalidation_boundaries_preserve_history(self):
        cases = {
            "brief_lock": "material_sync",
            "material_sync": "ai_storyboard",
            "rough_cut": "picture_order",
            "packaging_confirm": "fine_render",
            "auto_qa": "full_review",
        }
        for index, (boundary, first_invalidated) in enumerate(cases.items(), start=1):
            task_id = f"invalidate-{index}"
            self._create_task(task_id)
            seed_stage_states(self.db, task_id)
            self.db.connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id=?",
                (task_id,),
            )
            self.db.connection.execute(
                """
                INSERT INTO render_snapshots(
                  snapshot_id, task_id, stage, created_at
                ) VALUES(?, ?, 'mix', '2026-09-05 10:00:00')
                """,
                (f"history-{index}", task_id),
            )
            self.db.connection.commit()

            invalidate_from(self.db, task_id, boundary, reason=f"change-{index}")

            rows = self.db.connection.execute(
                """
                SELECT stage_key, status, detail_json FROM task_stage_states
                WHERE task_id=?
                """,
                (task_id,),
            ).fetchall()
            by_stage = {row["stage_key"]: row for row in rows}
            boundary_index = EXPECTED_STAGES.index(boundary)
            first_index = EXPECTED_STAGES.index(first_invalidated)
            self.assertEqual(first_index, boundary_index + 1)
            self.assertEqual(by_stage[boundary]["status"], "completed")
            for stage_key in EXPECTED_STAGES[first_index:]:
                self.assertEqual(by_stage[stage_key]["status"], "invalidated")
                self.assertEqual(
                    json.loads(by_stage[stage_key]["detail_json"]),
                    {"reason": f"change-{index}"},
                )
            history_count = self.db.connection.execute(
                "SELECT COUNT(*) FROM render_snapshots WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            self.assertEqual(history_count, 1)

    def test_latest_job_status_maps_to_run_status(self):
        expected = {
            "running": "running",
            "pending": "queued",
            "failed": "failed",
            "interrupted": "interrupted",
            "stale": "interrupted",
        }
        for index, (job_status, run_status) in enumerate(expected.items(), start=1):
            task_id = f"job-{index}"
            self._create_task(task_id)
            self.db.connection.execute(
                """
                INSERT INTO jobs(
                  job_id, job_type, status, task_id, created_at, updated_at
                ) VALUES(?, 'render', ?, ?, ?, ?)
                """,
                (
                    f"job-record-{index}",
                    job_status,
                    task_id,
                    f"2026-09-05 10:00:0{index}",
                    f"2026-09-05 10:00:0{index}",
                ),
            )
            self.db.connection.commit()
            self.assertEqual(
                project_workflow(self.db, task_id)["run_status"],
                run_status,
            )

    def test_paused_job_projects_paused_run_status(self):
        from media_catalog.workbench_worker import enqueue_job, request_job_pause

        enqueue_job(
            self.db,
            job_id="paused-job",
            job_type="render",
            task_id="task-1",
            payload={"task_id": "task-1"},
        )
        request_job_pause(self.db, "paused-job")

        self.assertEqual(project_workflow(self.db, "task-1")["run_status"], "paused")

    def test_latest_job_uses_update_time_before_insertion_order(self):
        task_id = "job-currentness"
        self._create_task(task_id)
        self.db.connection.execute(
            """
            INSERT INTO jobs(
              job_id, job_type, status, task_id, created_at, updated_at
            ) VALUES('job-inserted-first', 'render', 'running', ?,
                     '2026-09-05 09:00:00', '2026-09-05 12:00:00')
            """,
            (task_id,),
        )
        self.db.connection.execute(
            """
            INSERT INTO jobs(
              job_id, job_type, status, task_id, created_at, updated_at
            ) VALUES('job-inserted-later', 'render', 'failed', ?,
                     '2026-09-05 11:00:00', '2026-09-05 11:00:00')
            """,
            (task_id,),
        )
        self.db.connection.commit()

        self.assertEqual(project_workflow(self.db, task_id)["run_status"], "running")


if __name__ == "__main__":
    unittest.main()
