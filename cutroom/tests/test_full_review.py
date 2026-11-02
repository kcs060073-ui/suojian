import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.full_review import (
    approve_after_full_watch,
    complete_watch,
    latest_watch_session,
    record_watch_tick,
    start_watch,
)
from media_catalog.reviews import approve_review, matching_approval, reject_review
from media_catalog.snapshots import record_render_review_candidate


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FullReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "candidate.mp4"
        self.video.write_bytes(b"trusted-review-video")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.db.connection.execute(
            "INSERT INTO tasks(task_id,title,status) VALUES('task-1','任务','draft')"
        )
        self.db.connection.commit()
        self.candidate = self._candidate("qa-1", "digest-1")
        self.clock = MutableClock()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _candidate(self, report_id, digest):
        qa = {
            "report_id": report_id,
            "task_id": "task-1",
            "video_path": str(self.video.resolve()),
            "status": "passed",
            "issue_count": 0,
            "blocking_count": 0,
            "issues": [],
            "video_spec": {"duration": 100.0},
        }
        self.db.connection.execute(
            """
            INSERT INTO qa_reports(
              report_id,task_id,video_path,status,issue_count,
              blocking_count,report_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                report_id,
                "task-1",
                str(self.video.resolve()),
                "passed",
                0,
                0,
                json.dumps(qa),
            ),
        )
        self.db.connection.commit()
        return record_render_review_candidate(
            self.db,
            "task-1",
            {
                "output_path": str(self.video.resolve()),
                "dependency_digest": digest,
                "dependency_snapshot": {"files": []},
                "plan_path": str(self.root / f"{report_id}.plan.json"),
                "ass_path": None,
                "packaging": {"plan_id": "plan-1"},
            },
            qa,
        )

    def _start(self):
        return start_watch(
            self.db,
            "task-1",
            self.candidate["qa_snapshot_id"],
            "qa-1",
            clock=self.clock,
        )

    def test_direct_end_jump_and_client_claim_cannot_complete_watch(self):
        session = self._start()
        self.clock.advance(100)
        tick = record_watch_tick(
            self.db, session["session_id"], 100, clock=self.clock
        )

        self.assertFalse(tick["tick_accepted"])
        self.assertEqual(tick["covered_seconds"], 0)
        self.assertFalse(tick["reached_end"])
        with self.assertRaisesRegex(RuntimeError, "FULL_WATCH_REQUIRED"):
            complete_watch(self.db, session["session_id"])
        with self.assertRaisesRegex(RuntimeError, "FULL_WATCH_REQUIRED"):
            approve_review(
                self.db,
                "task-1",
                self.candidate["qa_snapshot_id"],
                "qa-1",
                "同事",
            )

    def test_server_merges_intervals_and_accepts_98_percent_plus_end(self):
        session = self._start()
        first = None
        for position in range(1, 50):
            self.clock.advance(1)
            first = record_watch_tick(
                self.db, session["session_id"], position, clock=self.clock
            )
        second = record_watch_tick(
            self.db, session["session_id"], 51, clock=self.clock
        )
        final = None
        for position in range(52, 101):
            self.clock.advance(1)
            final = record_watch_tick(
                self.db, session["session_id"], position, clock=self.clock
            )

        self.assertTrue(first["tick_accepted"])
        self.assertFalse(second["tick_accepted"])
        self.assertTrue(final["tick_accepted"])
        self.assertAlmostEqual(final["coverage_ratio"], 0.98)
        completed = complete_watch(self.db, session["session_id"])
        self.assertTrue(completed["can_approve"])
        approval = approve_after_full_watch(
            self.db,
            "task-1",
            self.candidate["qa_snapshot_id"],
            "qa-1",
            session["session_id"],
            "同事",
        )
        self.assertEqual(approval["watch_session_id"], session["session_id"])

    def test_changed_output_invalidates_active_watch(self):
        session = self._start()
        self.video.write_bytes(b"changed-after-watch-start")
        self.clock.advance(1)
        with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
            record_watch_tick(
                self.db, session["session_id"], 1, clock=self.clock
            )

    def test_small_ticks_cannot_accumulate_clock_jitter_into_full_watch(self):
        session = self._start()
        for index in range(1, 251):
            tick = record_watch_tick(
                self.db, session["session_id"], index * 0.4, clock=self.clock
            )
        self.assertLessEqual(tick["covered_seconds"], 0.5)
        with self.assertRaisesRegex(RuntimeError, "FULL_WATCH_REQUIRED"):
            complete_watch(self.db, session["session_id"])

    def test_pause_cannot_bank_time_for_a_burst_of_fake_playback(self):
        session = self._start()
        self.clock.advance(100)
        record_watch_tick(self.db, session["session_id"], 0, clock=self.clock)
        for index in range(1, 101):
            tick = record_watch_tick(
                self.db, session["session_id"], index, clock=self.clock
            )
        self.assertLessEqual(tick["covered_seconds"], 0.5)

    def test_rejected_tiny_seeks_cannot_fill_unwatched_interval_gaps(self):
        session = self._start()
        position = 0.4999
        record_watch_tick(self.db, session["session_id"], position, clock=self.clock)
        for _ in range(100):
            position += 0.0009
            rejected = record_watch_tick(self.db, session["session_id"], position, clock=self.clock)
            self.assertFalse(rejected["tick_accepted"])
            position += 0.0000001
            tick = record_watch_tick(self.db, session["session_id"], position, clock=self.clock)
            self.assertTrue(tick["tick_accepted"])
        self.assertLessEqual(tick["covered_seconds"], 0.5)

    def test_normal_playback_after_pause_and_rewatch_remains_approvable(self):
        session = self._start()
        for position in range(1, 41):
            self.clock.advance(1)
            record_watch_tick(self.db, session["session_id"], position, clock=self.clock)
        self.clock.advance(120)
        record_watch_tick(self.db, session["session_id"], 40, clock=self.clock)
        record_watch_tick(self.db, session["session_id"], 30, clock=self.clock)
        for position in range(31, 101):
            self.clock.advance(1)
            tick = record_watch_tick(self.db, session["session_id"], position, clock=self.clock)
            self.assertTrue(tick["tick_accepted"])
        self.assertTrue(complete_watch(self.db, session["session_id"])["can_approve"])

    def test_new_candidate_invalidates_old_completed_watch(self):
        session = self._start()
        for position in range(1, 101):
            self.clock.advance(1)
            record_watch_tick(
                self.db, session["session_id"], position, clock=self.clock
            )
        complete_watch(self.db, session["session_id"])
        next_candidate = self._candidate("qa-2", "digest-2")

        self.assertIsNone(
            latest_watch_session(
                self.db,
                "task-1",
                next_candidate["qa_snapshot_id"],
                "qa-2",
            )
        )
        self.assertEqual(
            latest_watch_session(
                self.db,
                "task-1",
                self.candidate["qa_snapshot_id"],
                "qa-1",
            )["session_id"],
            session["session_id"],
        )

        with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
            approve_after_full_watch(
                self.db,
                "task-1",
                self.candidate["qa_snapshot_id"],
                "qa-1",
                session["session_id"],
                "同事",
            )

    def test_rejection_requires_a_useful_note_but_not_a_completed_watch(self):
        for notes in (None, "", "   "):
            with self.subTest(notes=notes):
                with self.assertRaisesRegex(ValueError, "REJECTION_NOTES_REQUIRED"):
                    reject_review(
                        self.db,
                        "task-1",
                        self.candidate["qa_snapshot_id"],
                        "qa-1",
                        "同事",
                        notes,
                    )
        rejected = reject_review(
            self.db,
            "task-1",
            self.candidate["qa_snapshot_id"],
            "qa-1",
            "同事",
            "字幕第 12 秒不同步",
        )
        self.assertEqual(rejected["decision"], "rejected")

    def test_legacy_approval_without_watch_evidence_cannot_unlock_release(self):
        self.db.connection.execute(
            """
            INSERT INTO review_approvals(
              approval_id, task_id, snapshot_id, qa_report_id,
              decision, reviewed_by, notes
            ) VALUES(
              'legacy-approval', 'task-1', ?, 'qa-1',
              'approved', '旧版本', '没有观看会话'
            )
            """,
            (self.candidate["qa_snapshot_id"],),
        )
        self.db.connection.commit()

        self.assertIsNone(
            matching_approval(
                self.db,
                "task-1",
                self.candidate["qa_snapshot_id"],
                "qa-1",
            )
        )
