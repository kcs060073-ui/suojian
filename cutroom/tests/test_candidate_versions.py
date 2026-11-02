import json
import tempfile
import unittest
from pathlib import Path

from media_catalog.candidate_versions import (
    attach_candidate_qa,
    candidate_for_snapshot,
    ensure_candidate_version,
    get_candidate,
    list_candidates,
    record_candidate_qa,
    resolve_candidate_media,
)
from media_catalog.database import CatalogDatabase
from media_catalog.snapshots import (
    latest_review_candidate,
    record_qa_review_snapshot,
    record_render_dependency_snapshot,
)
from media_catalog.full_review import start_watch


class CandidateVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,title,status) VALUES('task-1','Task','draft')"
            )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def mix(self, name: str, digest: str) -> dict:
        video = self.root / f"{name}.mp4"
        video.write_bytes(f"video-{name}".encode("utf-8"))
        return record_render_dependency_snapshot(
            self.db,
            "task-1",
            {
                "output_path": str(video),
                "dependency_digest": digest,
                "dependency_snapshot": {"files": []},
                "plan_path": str(self.root / f"{name}.plan.json"),
                "ass_path": None,
                "packaging": {"plan_id": "plan-1"},
            },
        )

    def qa(self, report_id: str, video_path: str, *, status="blocked") -> dict:
        blocking_count = 0 if status == "passed" else 1
        issues = [] if status == "passed" else [
            {"severity": "blocking", "check": "black", "detail": "黑帧"}
        ]
        report = {
            "report_id": report_id,
            "task_id": "task-1",
            "video_path": str(Path(video_path).resolve()),
            "status": status,
            "issue_count": len(issues),
            "blocking_count": blocking_count,
            "issues": issues,
            "video_spec": {"duration": 12.0},
        }
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO qa_reports(
                  report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    report_id,
                    "task-1",
                    report["video_path"],
                    status,
                    len(issues),
                    blocking_count,
                    json.dumps(report, ensure_ascii=False),
                ),
            )
        return report

    def test_migration_creates_candidate_and_rework_tables(self):
        names = {
            row[0]
            for row in self.db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("candidate_versions", names)
        self.assertIn("rework_requests", names)

    def test_same_mix_snapshot_reuses_candidate_and_versions_are_monotonic(self):
        first_mix = self.mix("mix-1", "d1")
        first = ensure_candidate_version(self.db, "task-1", first_mix)
        replay = ensure_candidate_version(self.db, "task-1", first_mix)
        second = ensure_candidate_version(
            self.db, "task-1", self.mix("mix-2", "d2")
        )

        self.assertEqual(first["candidate_id"], replay["candidate_id"])
        self.assertEqual([first["version_no"], second["version_no"]], [1, 2])
        self.assertEqual(
            [item["candidate_id"] for item in list_candidates(self.db, "task-1")],
            [second["candidate_id"], first["candidate_id"]],
        )

    def test_blocked_qa_is_visible_without_becoming_reviewable(self):
        mix = self.mix("mix-1", "d1")
        candidate = ensure_candidate_version(self.db, "task-1", mix)
        updated = attach_candidate_qa(
            self.db,
            candidate["candidate_id"],
            self.qa("qa-1", mix["output_path"]),
        )

        self.assertEqual(updated["status"], "qa_blocked")
        self.assertIsNone(updated["qa_snapshot_id"])
        self.assertEqual(updated["qa_report_id"], "qa-1")
        self.assertEqual(
            get_candidate(self.db, "task-1", candidate["candidate_id"])["status"],
            "qa_blocked",
        )

    def test_candidate_for_snapshot_uses_mix_or_qa_identity(self):
        mix = self.mix("mix-1", "d1")
        candidate = ensure_candidate_version(self.db, "task-1", mix)
        resolved = candidate_for_snapshot(self.db, "task-1", mix["mix_snapshot_id"])
        self.assertEqual(resolved["candidate_id"], candidate["candidate_id"])

    def test_passed_qa_attaches_snapshot_and_enriches_latest_candidate(self):
        mix = self.mix("mix-1", "d1")
        qa = self.qa("qa-pass", mix["output_path"], status="passed")
        qa_snapshot = record_qa_review_snapshot(self.db, "task-1", mix, qa)
        candidate = record_candidate_qa(
            self.db,
            "task-1",
            mix,
            qa,
            qa_snapshot_id=qa_snapshot["qa_snapshot_id"],
        )

        current = latest_review_candidate(self.db, "task-1")
        self.assertEqual(current["candidate_id"], candidate["candidate_id"])
        self.assertEqual(current["version_label"], "candidate-v1")
        self.assertEqual(current["status"], "awaiting_review")

    def test_history_media_rejects_tampered_and_cross_task_candidate(self):
        mix = self.mix("mix-1", "d1")
        candidate = ensure_candidate_version(self.db, "task-1", mix)
        with self.assertRaisesRegex(KeyError, "CANDIDATE_NOT_FOUND"):
            resolve_candidate_media(self.db, "task-2", candidate["candidate_id"])

        Path(candidate["output_path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "CANDIDATE_MEDIA_CHANGED"):
            resolve_candidate_media(self.db, "task-1", candidate["candidate_id"])

    def test_latest_review_candidate_never_falls_back_past_blocked_current_version(self):
        first_mix = self.mix("mix-pass", "d-pass")
        first_qa = self.qa("qa-pass", first_mix["output_path"], status="passed")
        first_snapshot = record_qa_review_snapshot(self.db, "task-1", first_mix, first_qa)
        first = record_candidate_qa(
            self.db, "task-1", first_mix, first_qa,
            qa_snapshot_id=first_snapshot["qa_snapshot_id"],
        )
        second_mix = self.mix("mix-blocked", "d-blocked")
        second = record_candidate_qa(
            self.db,
            "task-1",
            second_mix,
            self.qa("qa-blocked", second_mix["output_path"]),
            eligible_for_review=False,
        )

        self.assertEqual(second["status"], "qa_blocked")
        self.assertIsNone(latest_review_candidate(self.db, "task-1"))
        with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
            start_watch(
                self.db, "task-1", first["qa_snapshot_id"], first["qa_report_id"]
            )

    def test_candidate_media_rejects_noncanonical_registered_path(self):
        mix = self.mix("stable", "d-stable")
        candidate = ensure_candidate_version(self.db, "task-1", mix)
        alias = self.root / "missing-directory" / ".." / "stable.mp4"
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE candidate_versions SET output_path=? WHERE candidate_id=?",
                (str(alias), candidate["candidate_id"]),
            )

        with self.assertRaisesRegex(RuntimeError, "CANDIDATE_MEDIA_BOUNDARY_CHANGED"):
            resolve_candidate_media(self.db, "task-1", candidate["candidate_id"])


if __name__ == "__main__":
    unittest.main()
