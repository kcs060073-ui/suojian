import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog import reviews
from media_catalog.assemble import gate_status
from media_catalog.cli import build_parser, main
from media_catalog.database import CatalogDatabase
from media_catalog.publish import build_publish_package
from media_catalog.snapshots import record_render_review_candidate
from tests.review_helpers import complete_watch_session


class ReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "candidate.mp4"
        self.video.write_bytes(b"trusted-video")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,title,status) VALUES('task-1','task','draft')"
            )
        self.qa = {
            "report_id": "qa-1", "task_id": "task-1",
            "video_path": str(self.video.resolve()), "status": "passed",
            "issue_count": 0, "blocking_count": 0, "issues": [],
            "video_spec": {"duration": 12.0},
        }
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO qa_reports(
                  report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                ) VALUES(?,?,?,?,?,?,?)""",
                (self.qa["report_id"], self.qa["task_id"], self.qa["video_path"],
                 self.qa["status"], 0, 0, json.dumps(self.qa)),
            )
        self.candidate = record_render_review_candidate(
            self.db, "task-1",
            {"output_path": str(self.video.resolve()), "dependency_digest": "digest",
             "dependency_snapshot": {"files": []}, "plan_path": str(self.root / "plan.json"),
             "ass_path": None, "packaging": {"plan_id": "plan-1"}},
            self.qa,
        )
        self.watch_session_id = complete_watch_session(
            self.db,
            "task-1",
            self.candidate["qa_snapshot_id"],
            self.qa["report_id"],
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _approve_review(self, *args, **kwargs):
        kwargs["watch_session_id"] = self.watch_session_id
        return reviews.approve_review(*args, **kwargs)

    def test_rejected_api_records_reviewer_and_notes(self):
        reject = getattr(reviews, "reject_review", None)
        self.assertTrue(callable(reject), "reject_review must be a supported review API")
        row = reject(
            self.db, "task-1", self.candidate["qa_snapshot_id"], self.qa["report_id"],
            "Peter", "字幕不同步",
        )
        self.assertEqual(row["decision"], "rejected")
        self.assertEqual(row["reviewed_by"], "Peter")
        self.assertEqual(row["notes"], "字幕不同步")

    def test_changed_candidate_blocks_both_decisions_before_writing(self):
        self.video.write_bytes(b"changed-video")
        for decision in ("approved", "rejected"):
            with self.subTest(decision=decision):
                before = self.db.connection.execute(
                    "SELECT COUNT(*) FROM review_approvals"
                ).fetchone()[0]
                with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
                    if decision == "approved":
                        self._approve_review(
                            self.db, "task-1", self.candidate["qa_snapshot_id"],
                            self.qa["report_id"], "Peter",
                        )
                    else:
                        reviews.reject_review(
                            self.db, "task-1", self.candidate["qa_snapshot_id"],
                            self.qa["report_id"], "Peter",
                        )
                after = self.db.connection.execute(
                    "SELECT COUNT(*) FROM review_approvals"
                ).fetchone()[0]
                self.assertEqual(after, before)
    def test_missing_candidate_blocks_both_decisions_before_writing(self):
        self.video.unlink()
        for decision in ("approved", "rejected"):
            with self.subTest(decision=decision):
                before = self.db.connection.execute(
                    "SELECT COUNT(*) FROM review_approvals"
                ).fetchone()[0]
                try:
                    if decision == "approved":
                        self._approve_review(
                            self.db, "task-1", self.candidate["qa_snapshot_id"],
                            self.qa["report_id"], "Peter",
                        )
                    else:
                        reviews.reject_review(
                            self.db, "task-1", self.candidate["qa_snapshot_id"],
                            self.qa["report_id"], "Peter",
                        )
                except Exception as error:
                    self.assertIsInstance(error, RuntimeError)
                    self.assertIn("UNTRUSTED_REVIEW_CANDIDATE", str(error))
                else:
                    self.fail("missing candidate must fail before a review row is written")
                after = self.db.connection.execute(
                    "SELECT COUNT(*) FROM review_approvals"
                ).fetchone()[0]
                self.assertEqual(after, before)
    def test_cli_parser_exposes_review_reject(self):
        parser = build_parser()
        review = next(
            action for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        review_parser = review.choices["review"]
        command = next(
            action for action in review_parser._actions
            if getattr(action, "dest", None) == "review_command"
        )
        self.assertIn("reject", command.choices)
    def test_cli_review_reject_handler_records_rejection(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main([
                "--db", str(self.db.path), "review", "reject", "--task", "task-1",
                "--snapshot", self.candidate["qa_snapshot_id"], "--qa", self.qa["report_id"],
                "--by", "Peter", "--notes", "首版不通过", "--json",
            ])
        self.assertEqual(result, 0)
        row = json.loads(output.getvalue())
        self.assertEqual(row["decision"], "rejected")
        self.assertEqual(row["notes"], "首版不通过")
    def test_invalid_snapshot_or_qa_blocks_both_decisions_without_rows(self):
        for decision, snapshot, report_id in (
            ("approved", "wrong-snapshot", self.qa["report_id"]),
            ("rejected", "wrong-snapshot", self.qa["report_id"]),
            ("approved", self.candidate["qa_snapshot_id"], "wrong-qa"),
            ("rejected", self.candidate["qa_snapshot_id"], "wrong-qa"),
        ):
            with self.subTest(decision=decision, snapshot=snapshot, qa=report_id):
                before = self.db.connection.execute(
                    "SELECT COUNT(*) FROM review_approvals"
                ).fetchone()[0]
                with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
                    reviews.record_review(
                        self.db, "task-1", snapshot, report_id, decision, "Peter",
                    )
                after = self.db.connection.execute(
                    "SELECT COUNT(*) FROM review_approvals"
                ).fetchone()[0]
                self.assertEqual(after, before)

    def test_non_assemble_candidate_blocks_both_decisions_without_rows(self):
        self.db.connection.execute(
            "UPDATE render_snapshots SET producer='manual' WHERE snapshot_id=?",
            (self.candidate["qa_snapshot_id"],),
        )
        self.db.connection.commit()
        for decision in ("approved", "rejected"):
            with self.subTest(decision=decision):
                before = self.db.connection.execute(
                    "SELECT COUNT(*) FROM review_approvals"
                ).fetchone()[0]
                with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
                    reviews.record_review(
                        self.db, "task-1", self.candidate["qa_snapshot_id"],
                        self.qa["report_id"], decision, "Peter",
                    )
                after = self.db.connection.execute(
                    "SELECT COUNT(*) FROM review_approvals"
                ).fetchone()[0]
                self.assertEqual(after, before)

    def test_latest_rejection_blocks_then_later_approval_allows_release(self):
        approved = self._approve_review(
            self.db, "task-1", self.candidate["qa_snapshot_id"], self.qa["report_id"], "Peter",
        )
        rejected = reviews.reject_review(
            self.db, "task-1", self.candidate["qa_snapshot_id"], self.qa["report_id"],
            "Peter", "发现需要修改的问题",
        )
        latest = reviews.matching_approval(
            self.db, "task-1", self.candidate["qa_snapshot_id"], self.qa["report_id"],
        )
        self.assertNotEqual(approved["approval_id"], rejected["approval_id"])
        self.assertEqual(latest["approval_id"], rejected["approval_id"])
        self.assertEqual(latest["decision"], "rejected")
        self.assertFalse(gate_status(self.db, "task-1")["human_reviewed"])
        with self.assertRaisesRegex(ValueError, "REVIEW_APPROVAL_REQUIRED"):
            build_publish_package(
                self.db, "task-1", "视频号", video_path=self.video, qa_report=self.qa,
            )

        later_approved = self._approve_review(
            self.db, "task-1", self.candidate["qa_snapshot_id"], self.qa["report_id"], "Peter",
        )
        self.assertEqual(
            reviews.matching_approval(
                self.db, "task-1", self.candidate["qa_snapshot_id"], self.qa["report_id"],
            )["approval_id"],
            later_approved["approval_id"],
        )
        self.assertTrue(gate_status(self.db, "task-1")["human_reviewed"])

        def fake_ffmpeg(command, **_kwargs):
            Path(command[-1]).write_bytes(b"jpeg")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with (
            patch("media_catalog.publish.DELIVER_ROOT", self.root / "deliverables"),
            patch("media_catalog.publish.find_ffmpeg", return_value=Path("ffmpeg")),
            patch("media_catalog.publish.subprocess.run", side_effect=fake_ffmpeg),
        ):
            package = build_publish_package(
                self.db, "task-1", "视频号", video_path=self.video, qa_report=self.qa,
            )
        self.assertEqual(package["status"], "cleared")
    def test_newer_candidate_before_locked_review_validation_blocks_stale_approval(self):
        called = False

        def add_newer_candidate():
            nonlocal called
            called = True
            newer_qa = {**self.qa, "report_id": "qa-newer"}
            with self.db.transaction() as connection:
                connection.execute(
                    """INSERT INTO qa_reports(
                      report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (newer_qa["report_id"], newer_qa["task_id"], newer_qa["video_path"],
                     newer_qa["status"], 0, 0, json.dumps(newer_qa)),
                )
            record_render_review_candidate(
                self.db, "task-1",
                {"output_path": str(self.video.resolve()), "dependency_digest": "newer-digest",
                 "dependency_snapshot": {"files": []}, "plan_path": str(self.root / "newer.json"),
                 "ass_path": None, "packaging": {"plan_id": "plan-1"}},
                newer_qa,
            )

        with patch.object(reviews, "_before_review_lock_hook", add_newer_candidate, create=True):
            with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
                self._approve_review(
                    self.db, "task-1", self.candidate["qa_snapshot_id"],
                    self.qa["report_id"], "Peter",
                )
        self.assertTrue(called)
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM review_approvals").fetchone()[0],
            0,
        )

    def test_video_mutation_before_locked_review_insert_writes_no_row(self):
        with patch.object(
            reviews,
            "_before_review_insert_hook",
            lambda: self.video.write_bytes(b"changed-during-review"),
            create=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
                self._approve_review(
                    self.db, "task-1", self.candidate["qa_snapshot_id"],
                    self.qa["report_id"], "Peter",
                )
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM review_approvals").fetchone()[0],
            0,
        )
    def test_review_rejects_caller_owned_transaction_without_rollback(self):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,title,status) VALUES('caller-marker','marker','draft')"
            )
            try:
                self._approve_review(
                    self.db, "task-1", self.candidate["qa_snapshot_id"],
                    self.qa["report_id"], "Peter",
                )
            except Exception as error:
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("ACTIVE_TRANSACTION_UNSUPPORTED", str(error))
            else:
                self.fail("review must fail before touching a caller-owned transaction")
            self.assertTrue(self.db.connection.in_transaction)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE task_id='caller-marker'"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_id='caller-marker'"
            ).fetchone()[0],
            1,
        )
    def test_review_does_not_rollback_when_begin_immediate_fails(self):
        class BeginFailureConnection:
            in_transaction = False

            def __init__(self):
                self.rollback_calls = 0

            def execute(self, _sql):
                raise sqlite3.OperationalError("begin failed")

            def rollback(self):
                self.rollback_calls += 1

        connection = BeginFailureConnection()
        db = SimpleNamespace(connection=connection)
        with self.assertRaisesRegex(sqlite3.OperationalError, "begin failed"):
            reviews.record_review(
                db, "task-1", "snapshot", "qa", "approved", "Peter",
            )
        self.assertEqual(connection.rollback_calls, 0)

if __name__ == "__main__":
    unittest.main()
