import tempfile
import unittest
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog.publish import build_publish_package
from media_catalog.qa import run_qa
from media_catalog.cli import main
from media_catalog.render_mix_adapter import RenderMixJobError
from tests.review_helpers import approve_review
from media_catalog.snapshots import record_render_review_candidate


class Task5FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO tasks(task_id,title,status) VALUES('t','t','draft')")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_qa_missing_video_is_blocking_not_passed(self):
        report = run_qa(self.db, "t", str(self.root / "missing.mp4"))
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any(i["check"] == "video" and i["severity"] == "blocking"
                            for i in report["issues"]))

    def test_qa_tool_or_detection_failure_is_blocking(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"not-a-video")
        with patch("media_catalog.qa.find_ffmpeg", side_effect=RuntimeError("tool failed")):
            report = run_qa(self.db, "t", str(video))
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any(i["check"] == "video_evidence" and i["severity"] == "blocking"
                            for i in report["issues"]))

    def test_publish_rejects_platform_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "INVALID_PLATFORM"):
            build_publish_package(self.db, "t", "../escaped", title="safe")

    def test_publish_write_failure_leaves_no_database_record(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"reviewed")
        qa_report = {
            "report_id": "qa-t",
            "task_id": "t",
            "video_path": str(video.resolve()),
            "status": "passed",
            "blocking_count": 0,
            "video_spec": {"duration": 10.0},
            "issues": [],
        }
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO qa_reports(
                  report_id,task_id,video_path,status,issue_count,
                  blocking_count,report_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    qa_report["report_id"], "t", qa_report["video_path"], "passed",
                    0, 0, json.dumps(qa_report),
                ),
            )
        candidate = record_render_review_candidate(
            self.db,
            "t",
            {
                "output_path": str(video.resolve()),
                "dependency_digest": "task5-dependency",
                "dependency_snapshot": {"files": []},
                "plan_path": str(self.root / "plan.json"),
                "ass_path": None,
                "packaging": {},
            },
            qa_report,
        )
        approve_review(
            self.db, "t", candidate["qa_snapshot_id"], qa_report["report_id"],
            "Peter", "watched",
        )
        deliver_root = self.root / "deliverables"
        with (
            patch("media_catalog.publish.DELIVER_ROOT", deliver_root),
            patch("media_catalog.publish.find_ffmpeg", return_value=Path("ffmpeg")),
            patch("media_catalog.publish._extract_cover"),
            patch("media_catalog.publish._write_exclusive",
                  side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                build_publish_package(
                    self.db, "t", "video", title="safe",
                    video_path=video, qa_report=qa_report,
                )
        count = self.db.connection.execute(
            "SELECT COUNT(*) FROM publish_records WHERE task_id='t'"
        ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertFalse((deliver_root / "t").exists())

    def test_cli_json_preserves_structured_render_code_and_nonzero_exit(self):
        error = io.StringIO()
        with (
            patch("media_catalog.cli.assemble",
                  side_effect=RenderMixJobError("OUTPUT_OUTSIDE_PROJECT", "escaped")),
            redirect_stderr(error),
        ):
            exit_code = main([
                "--db", str(self.root / "cli.sqlite"), "assemble", "--task", "t",
                "--render", "--output", "out/t.mp4", "--json",
            ])
        self.assertEqual(exit_code, 1)
        payload = json.loads(error.getvalue())
        self.assertEqual(payload["error"]["code"], "OUTPUT_OUTSIDE_PROJECT")


if __name__ == "__main__":
    unittest.main()
