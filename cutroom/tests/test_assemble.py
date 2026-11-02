import io
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog.assemble import assemble, gate_status
from media_catalog.cli import build_parser
from media_catalog.snapshots import latest_review_candidate, list_snapshots
from tests.review_helpers import approve_review
from media_catalog.tasks import create_task
from media_catalog.workflow import project_gates
from tests.test_render_mix_adapter import RenderMixCatalogFixture


class AssembleTests(RenderMixCatalogFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.work_dir = self.root / "work"
        self.output_path = self.root / "out" / "task-1.mp4"
        render_script = self.root / "scripts" / "render-mix.ts"
        ts_node = self.root / "node_modules" / "ts-node" / "dist" / "bin.js"
        render_script.parent.mkdir(parents=True, exist_ok=True)
        ts_node.parent.mkdir(parents=True, exist_ok=True)
        render_script.write_text("// fake", encoding="utf-8")
        ts_node.write_text("// fake", encoding="utf-8")
        self.render_calls = []

    def fake_render_runner(self, command, **kwargs):
        self.render_calls.append((command, kwargs))
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_bytes(b"rendered-video")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    @staticmethod
    def qa_result(db, task_id, video_path, *, status="passed", evidence=None):
        report_id = f"qa-{uuid.uuid4().hex[:12]}"
        blocking = 0 if status == "passed" else 1
        issues = [
            {"check": name, "severity": "info", "ok": True, "detail": "ok"}
            for name in (
                "video_evidence", "black_frame", "freeze", "silence", "clipping",
                "subtitle_evidence", "loudness_integrated", "true_peak",
                "material_fingerprint", "material_scope", "material_authorization",
            )
        ]
        if blocking:
            issues.append({"check": "synthetic_block", "severity": "blocking",
                           "ok": False, "detail": "blocked"})
        report = {
            "report_id": report_id,
            "task_id": task_id,
            "video_path": str(Path(video_path).resolve()),
            "status": status,
            "issue_count": len(issues),
            "blocking_count": blocking,
            "issues": issues,
            "video_spec": {"duration": 10.0},
        }
        with db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO qa_reports(
                  report_id, task_id, video_path, status, issue_count,
                  blocking_count, report_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    task_id,
                    report["video_path"],
                    status,
                    len(issues),
                    blocking,
                    json.dumps(report),
                ),
            )
        return report

    def run_first_stage(self, *, qa_status="passed"):
        def qa(db, task_id, video_path, **_kwargs):
            return self.qa_result(db, task_id, video_path, status=qa_status)

        with patch("media_catalog.assemble.run_qa", side_effect=qa):
            return assemble(
                self.db,
                "task-1",
                render=True,
                ass_path=self.bound_ass_path,
                output_path=self.output_path,
                work_dir=self.work_dir,
                runner=self.fake_render_runner,
            )

    def test_first_stage_renders_snapshots_qa_and_waits_for_human_review(self):
        with patch("media_catalog.assemble.build_publish_package") as publish:
            result = self.run_first_stage()

        self.assertEqual(result["mode"], "render")
        self.assertEqual(result["status"], "awaiting_human_review")
        self.assertFalse(result["go"])
        self.assertEqual(result["qa"]["status"], "passed")
        self.assertIs(result["gates"].get("qa_passed"), True)
        self.assertNotIn("任务状态", result["message"])
        self.assertEqual(Path(result["output_path"]), self.output_path.resolve())
        self.assertEqual(len(self.render_calls), 1)
        publish.assert_not_called()
        self.assertEqual(
            self.db.connection.execute(
                "SELECT status FROM tasks WHERE task_id='task-1'"
            ).fetchone()[0],
            "packaging_confirmed",
        )
        candidate = latest_review_candidate(self.db, "task-1")
        self.assertEqual(candidate["qa_report_id"], result["qa"]["report_id"])
        self.assertEqual(Path(candidate["output_path"]), self.output_path.resolve())
        self.assertTrue(candidate["dependency_digest"])
        snapshots = list_snapshots(self.db, "task-1")
        mix = next(item for item in snapshots if item["stage"] == "mix")
        qa = next(item for item in snapshots if item["stage"] == "qa")
        self.assertEqual(qa["parent_snapshot"], mix["snapshot_id"])

    def test_changed_locked_cards_can_render_a_new_immutable_candidate(self):
        from media_catalog.scene_cards import set_picture_lock
        from media_catalog.packaging_plan import confirm_plan
        first = self.run_first_stage()
        old_path = self.output_path
        old_bytes = old_path.read_bytes()
        with self.db.transaction() as connection:
            connection.execute("UPDATE scene_cards SET enter_tc=1,exit_tc=4 WHERE card_id='task-1#1'")
        set_picture_lock(self.db, "task-1", confirm_all=True)
        confirm_plan(self.db, "plan-1")
        self.output_path = self.root / "out" / "candidate-v2.mp4"
        self.work_dir = self.root / "work-v2"
        second = self.run_first_stage()
        self.assertEqual(second["status"], "awaiting_human_review")
        self.assertNotEqual(first["snapshots"]["qa_snapshot_id"], second["snapshots"]["qa_snapshot_id"])
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertTrue(self.output_path.is_file())
        self.assertEqual(len(list_snapshots(self.db, "task-1", "mix")), 2)
        self.assertFalse(gate_status(self.db, "task-1")["human_reviewed"])

    def test_workbench_rework_keeps_candidates_and_validates_same_input_binding(self):
        from media_catalog import workbench_actions as actions
        from media_catalog.scene_cards import set_picture_lock
        from media_catalog.packaging_plan import confirm_plan
        task_root = self.root / "runtime" / "workbench" / "tasks" / "task-1"
        artifacts = task_root / "artifacts"
        storyboard = artifacts / "storyboard"
        storyboard.mkdir(parents=True)
        script = artifacts / "script.txt"
        script.write_text("fixture script", encoding="utf-8")
        (artifacts / "transcript.words.json").write_text("[]", encoding="utf-8")
        (storyboard / "brief.json").write_text('{"keywords":[],"punchLines":[],"broll":[]}', encoding="utf-8")
        job = SimpleNamespace(task_id="task-1", payload={})
        def render(command, **options):
            self.fake_render_runner(command, **options)
            return {"started": True, "timed_out": False, "returncode": 0}
        def use_bound_subtitle(db, task_id, **options):
            options.pop("subtitle_sources")
            return assemble(db, task_id, ass_path=self.bound_ass_path, **options)
        with (
            patch.object(actions, "_locked_inputs", return_value=(task_root, {}, script, self.digital_human, "script-digest")),
            patch.object(actions, "assemble", side_effect=use_bound_subtitle),
            patch("media_catalog.assemble.run_qa", side_effect=self.qa_result),
        ):
            first = actions.render_and_qa(self.db, job, self.root, render)
            repeated = actions.render_and_qa(self.db, job, self.root, render)
            self.assertTrue(repeated["result"]["reused"])
            self.assertEqual(len(self.render_calls), 1)
            old_output = Path(first["result"]["output_path"])
            old_bytes = old_output.read_bytes()
            with self.db.transaction() as connection:
                connection.execute("UPDATE scene_cards SET enter_tc=1,exit_tc=4 WHERE card_id='task-1#1'")
            set_picture_lock(self.db, "task-1", confirm_all=True)
            confirm_plan(self.db, "plan-1")
            second = actions.render_and_qa(self.db, job, self.root, render)
            self.assertNotEqual(first["result"]["output_path"], second["result"]["output_path"])
            self.assertEqual(old_output.read_bytes(), old_bytes)
            self.assertEqual(len(self.render_calls), 2)
            self.assertEqual(len(list_snapshots(self.db, "task-1", "mix")), 2)
            self.assertEqual(len(list(artifacts.glob("render-action.*.json"))), 2)
            state_path = max(
                artifacts.glob("render-action.*.json"),
                key=lambda path: path.stat().st_mtime_ns,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["result"]["snapshots"]["qa_snapshot_id"] = "snapshot-replaced"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "ACTION_CANDIDATE_CHANGED"):
                actions.render_and_qa(self.db, job, self.root, render)
            self.assertEqual(len(self.render_calls), 2)

    def test_transient_runner_failure_can_retry_with_identical_plan(self):
        def failing_runner(command, **kwargs):
            return SimpleNamespace(returncode=7, stdout="", stderr="transient")
        with self.assertRaisesRegex(RuntimeError, r"^RENDER_PROCESS_FAILED:"):
            assemble(self.db, "task-1", render=True, output_path=self.output_path,
                     ass_path=self.bound_ass_path, work_dir=self.work_dir,
                     runner=failing_runner)
        result = self.run_first_stage()
        self.assertEqual(result["status"], "awaiting_human_review")
        self.assertEqual(len(self.render_calls), 1)

    def test_qa_blocked_render_can_rerun_qa_without_rerendering_or_overwrite(self):
        qa_statuses = iter(("blocked", "passed"))
        def qa(db, task_id, video_path, **_kwargs):
            return self.qa_result(db, task_id, video_path, status=next(qa_statuses))
        with patch("media_catalog.assemble.run_qa", side_effect=qa):
            first = assemble(self.db, "task-1", render=True, output_path=self.output_path,
                             ass_path=self.bound_ass_path, work_dir=self.work_dir,
                             runner=self.fake_render_runner)
            second = assemble(
                self.db, "task-1", render=True, output_path=self.output_path,
                ass_path=self.bound_ass_path, work_dir=self.work_dir,
                runner=lambda *_args, **_kwargs: self.fail("QA retry must not rerender"),
            )
        self.assertEqual(first["status"], "qa_blocked")
        self.assertEqual(second["status"], "awaiting_human_review")
        self.assertEqual(len(self.render_calls), 1)
        self.assertEqual(len(list_snapshots(self.db, "task-1", "mix")), 1)
    def test_qa_blocked_retry_revalidates_current_brief_gate_without_rerender(self):
        qa_calls = []

        def blocked_qa(db, task_id, video_path, **_kwargs):
            qa_calls.append(video_path)
            return self.qa_result(db, task_id, video_path, status="blocked")

        with patch("media_catalog.assemble.run_qa", side_effect=blocked_qa):
            first = assemble(
                self.db, "task-1", render=True, output_path=self.output_path,
                ass_path=self.bound_ass_path, work_dir=self.work_dir,
                runner=self.fake_render_runner,
            )
            original_output = self.output_path.read_bytes()
            create_task(
                self.db, "task-1", "task", task_type="数字人混剪",
                brief={"text": "changed after blocked QA"},
            )
            with self.assertRaisesRegex(RuntimeError, r"^BRIEF_GATE_DIGEST_MISMATCH:"):
                assemble(
                    self.db, "task-1", render=True, output_path=self.output_path,
                    ass_path=self.bound_ass_path, work_dir=self.work_dir,
                    runner=lambda *_args, **_kwargs: self.fail("must not rerender"),
                )

        self.assertEqual(first["status"], "qa_blocked")
        self.assertEqual(len(self.render_calls), 1)
        self.assertEqual(len(qa_calls), 1)
        self.assertEqual(self.output_path.read_bytes(), original_output)

    def test_dependency_snapshot_is_recorded_before_qa_runs(self):
        def qa(db, task_id, video_path, **kwargs):
            mix_snapshots = list_snapshots(db, task_id, "mix")
            self.assertEqual(len(mix_snapshots), 1)
            self.assertEqual(Path(mix_snapshots[0]["output_path"]), Path(video_path))
            evidence = kwargs["evidence"]
            self.assertEqual(evidence["dependency_snapshot"]["task_id"], "task-1")
            self.assertEqual(Path(evidence["subtitle_path"]), self.bound_ass_path)
            return self.qa_result(db, task_id, video_path)

        with patch("media_catalog.assemble.run_qa", side_effect=qa):
            result = assemble(
                self.db,
                "task-1",
                render=True,
                ass_path=self.bound_ass_path,
                output_path=self.output_path,
                work_dir=self.work_dir,
                runner=self.fake_render_runner,
            )
        self.assertEqual(result["status"], "awaiting_human_review")

    def test_reviewed_second_stage_publishes_only_bound_render_without_rerender(self):
        first = self.run_first_stage()
        candidate = latest_review_candidate(self.db, "task-1")
        approve_review(
            self.db, "task-1", candidate["qa_snapshot_id"],
            candidate["qa_report_id"], "Peter", "完整观看通过",
        )
        self.db.connection.execute(
            "UPDATE tasks SET status='reviewed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()
        self.render_calls.clear()
        package = {"record_id": "pub-1", "status": "cleared"}

        with patch(
            "media_catalog.assemble.build_publish_package", return_value=package
        ) as publish:
            result = assemble(
                self.db,
                "task-1",
                render=True,
                ass_path=self.bound_ass_path,
                output_path=self.output_path,
                work_dir=self.work_dir,
                runner=lambda *_args, **_kwargs: self.fail("review stage must not rerender"),
                platform="视频号",
                title="title",
                description="description",
                tags=["tag"],
            )

        self.assertEqual(first["status"], "awaiting_human_review")
        self.assertEqual(result["status"], "published")
        self.assertTrue(result["go"])
        self.assertEqual(result["publish"], package)
        self.assertEqual(self.render_calls, [])
        publish.assert_called_once()
        publish_args, publish_kwargs = publish.call_args
        self.assertEqual(publish_args, (self.db, "task-1", "视频号"))
        self.assertEqual(publish_kwargs["title"], "title")
        self.assertEqual(publish_kwargs["description"], "description")
        self.assertEqual(publish_kwargs["tags"], ["tag"])
        self.assertEqual(
            Path(publish_kwargs["video_path"]), self.output_path.resolve()
        )
        self.assertEqual(
            publish_kwargs["qa_report"]["report_id"], first["qa"]["report_id"]
        )
        self.assertEqual(
            Path(publish_kwargs["qa_report"]["video_path"]),
            self.output_path.resolve(),
        )

    def test_human_review_gate_tracks_bound_approval_not_task_status(self):
        result = self.run_first_stage()
        self.assertFalse(result["gates"]["human_reviewed"])
        self.assertFalse(gate_status(self.db, "task-1")["human_reviewed"])
        candidate = latest_review_candidate(self.db, "task-1")
        approve_review(self.db, "task-1", candidate["qa_snapshot_id"],
                       candidate["qa_report_id"], "Peter", "watched")
        status = self.db.connection.execute(
            "SELECT status FROM tasks WHERE task_id='task-1'"
        ).fetchone()[0]
        self.assertNotEqual(status, "reviewed")
        self.assertTrue(gate_status(self.db, "task-1")["human_reviewed"])

    def test_gate_status_matches_trusted_project_gate_projection(self):
        gates = gate_status(self.db, "task-1")
        self.assertEqual(gates, project_gates(self.db, "task-1"))
        self.assertEqual(
            set(gates),
            {
                "brief_locked",
                "picture_lock",
                "packaging_confirmed",
                "qa_passed",
                "human_reviewed",
            },
        )

    def test_gate_status_includes_brief_lock_from_trusted_evidence(self):
        gates = gate_status(self.db, "task-1")
        self.assertIn("brief_locked", gates)
        self.assertTrue(gates["brief_locked"])

    def test_reviewed_status_alone_cannot_publish_without_bound_approval(self):
        self.run_first_stage()
        self.db.connection.execute("UPDATE tasks SET status='reviewed' WHERE task_id='task-1'")
        self.db.connection.commit()
        self.assertFalse(gate_status(self.db, "task-1")["human_reviewed"])
        with patch("media_catalog.assemble.build_publish_package") as publish:
            result = assemble(
                self.db, "task-1", render=True, output_path=self.output_path,
                ass_path=self.bound_ass_path, work_dir=self.work_dir,
            )
        self.assertEqual(result["status"], "review_binding_invalid")
        self.assertIn("review_approval_missing", result["blocked_by"])
        publish.assert_not_called()

    def test_review_is_blocked_if_latest_qa_points_to_another_output(self):
        self.run_first_stage()
        other_output = self.root / "other.mp4"
        other_output.write_bytes(b"other")
        self.qa_result(self.db, "task-1", other_output)
        self.db.connection.execute(
            "UPDATE tasks SET status='reviewed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()

        with patch("media_catalog.assemble.build_publish_package") as publish:
            result = assemble(
                self.db,
                "task-1",
                render=True,
                ass_path=self.bound_ass_path,
                output_path=self.output_path,
                work_dir=self.work_dir,
            )

        self.assertEqual(result["status"], "review_binding_invalid")
        self.assertIn("qa_report_mismatch", result["blocked_by"])
        self.assertFalse(result["go"])
        publish.assert_not_called()

    def test_review_is_blocked_if_dependency_files_changed_after_render(self):
        self.run_first_stage()
        self.broll.write_bytes(b"changed-after-render")
        self.db.connection.execute(
            "UPDATE tasks SET status='reviewed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()

        with patch("media_catalog.assemble.build_publish_package") as publish:
            result = assemble(
                self.db,
                "task-1",
                render=True,
                ass_path=self.bound_ass_path,
                output_path=self.output_path,
                work_dir=self.work_dir,
            )

        self.assertEqual(result["status"], "review_binding_invalid")
        self.assertIn("dependency_snapshot_changed", result["blocked_by"])
        publish.assert_not_called()

    def test_review_is_blocked_if_rendered_output_changed_after_qa(self):
        self.run_first_stage()
        self.output_path.write_bytes(b"replaced-after-qa")
        self.db.connection.execute(
            "UPDATE tasks SET status='reviewed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()

        with patch("media_catalog.assemble.build_publish_package") as publish:
            result = assemble(
                self.db,
                "task-1",
                render=True,
                ass_path=self.bound_ass_path,
                output_path=self.output_path,
                work_dir=self.work_dir,
            )

        self.assertEqual(result["status"], "review_binding_invalid")
        self.assertIn("render_output_changed", result["blocked_by"])
        publish.assert_not_called()

    def test_blocked_qa_does_not_advance_to_human_review_or_publish(self):
        with patch("media_catalog.assemble.build_publish_package") as publish:
            result = self.run_first_stage(qa_status="blocked")

        self.assertEqual(result["status"], "qa_blocked")
        self.assertFalse(result["go"])
        self.assertIs(result["gates"].get("qa_passed"), False)
        self.assertIn("qa_passed", result["blocked_by"])
        publish.assert_not_called()

    def test_qa_passed_without_expected_video_evidence_is_still_blocked(self):
        def incomplete_qa(db, task_id, video_path, **_kwargs):
            report = self.qa_result(db, task_id, video_path)
            report["issues"] = []
            return report
        with patch("media_catalog.assemble.run_qa", side_effect=incomplete_qa):
            result = assemble(
                self.db, "task-1", render=True, output_path=self.output_path,
                ass_path=self.bound_ass_path, work_dir=self.work_dir,
                runner=self.fake_render_runner,
            )
        self.assertEqual(result["status"], "qa_blocked")
        self.assertIn("qa_video_evidence", result["blocked_by"])

    def test_imported_video_mode_is_qa_only_even_when_task_is_reviewed(self):
        imported = self.root / "imported.mp4"
        imported.write_bytes(b"imported")
        self.db.connection.execute(
            "UPDATE tasks SET status='reviewed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()

        def qa(db, task_id, video_path, **_kwargs):
            return self.qa_result(db, task_id, video_path)

        with (
            patch("media_catalog.assemble.run_qa", side_effect=qa),
            patch("media_catalog.assemble.build_publish_package") as publish,
        ):
            result = assemble(self.db, "task-1", video_path=imported)

        self.assertEqual(result["mode"], "imported_video_qa")
        self.assertEqual(result["status"], "imported_video_qa")
        self.assertFalse(result["go"])
        self.assertNotIn("端到端验收通过", result["message"])
        publish.assert_not_called()

    def test_reviewed_task_without_bound_candidate_still_runs_first_stage_not_publish(self):
        self.db.connection.execute(
            "UPDATE tasks SET status='reviewed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()
        with patch("media_catalog.assemble.build_publish_package") as publish:
            result = self.run_first_stage()
        self.assertEqual(result["status"], "awaiting_human_review")
        publish.assert_not_called()

    def test_changed_picture_lock_is_not_reported_as_passed_gate(self):
        self.db.connection.execute(
            "UPDATE picture_lock SET changed_since_lock=1 WHERE task_id='task-1'"
        )
        self.db.connection.commit()
        self.assertFalse(gate_status(self.db, "task-1")["picture_lock"])


class AssembleCliTests(unittest.TestCase):
    def test_parser_exposes_render_ass_and_output_and_separates_import_mode(self):
        parser = build_parser()
        args = parser.parse_args([
            "assemble",
            "--task", "task-1",
            "--render",
            "--ass", "subtitle.ass",
            "--output", "out/final.mp4",
        ])
        self.assertTrue(args.render)
        self.assertEqual(args.ass, "subtitle.ass")
        self.assertEqual(args.output, "out/final.mp4")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([
                    "assemble",
                    "--task", "task-1",
                    "--render",
                    "--video", "imported.mp4",
                ])

    def test_parser_accepts_complete_subtitle_sources_and_rejects_partial_or_ass_combination(self):
        parser = build_parser()
        args = parser.parse_args([
            "assemble", "--task", "task-1", "--render",
            "--script", "locked.script.txt",
            "--words", "locked.words.json",
            "--semantics", "locked.semantics.json",
            "--output", "out/final.mp4",
        ])
        self.assertEqual(args.script, "locked.script.txt")
        self.assertEqual(args.words, "locked.words.json")
        self.assertEqual(args.semantics, "locked.semantics.json")

        invalid = (
            ["--script", "locked.script.txt"],
            ["--words", "locked.words.json"],
            ["--semantics", "locked.semantics.json"],
            ["--script", "locked.script.txt", "--words", "locked.words.json"],
            [
                "--script", "locked.script.txt",
                "--words", "locked.words.json",
                "--semantics", "locked.semantics.json",
                "--ass", "external.ass",
            ],
        )
        for extra in invalid:
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args([
                        "assemble", "--task", "task-1", "--render", *extra,
                    ])


if __name__ == "__main__":
    unittest.main()
