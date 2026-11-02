import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.jobs import HeavyJobBusy, JobManager
from media_catalog.libraries import register_library


class JobTests(unittest.TestCase):
    def test_every_declared_heavy_type_must_use_the_durable_worker_queue(self):
        """Legacy callers must not bypass FIFO, leases, attempts, or retry policy."""
        for job_type in sorted(JobManager.HEAVY_TYPES):
            with self.subTest(job_type=job_type):
                with tempfile.TemporaryDirectory() as tmp:
                    with self.open_catalog(tmp) as db:
                        with self.assertRaisesRegex(
                            HeavyJobBusy, "持久队列"
                        ):
                            JobManager(
                                db, f"{job_type}-direct", job_type,
                                "park-01",
                            ).start()

    def test_legacy_lightweight_stale_running_becomes_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            with CatalogDatabase.open(path) as first_db:
                first_db.migrate()
                register_library(first_db, "park-01", "园区库", "local", tmp)
                first = JobManager(first_db, "search-1", "search", "park-01")
                first.start()
                first_db.connection.execute(
                    "UPDATE jobs SET heartbeat_at='2000-01-01 00:00:00'"
                )
                first_db.connection.commit()
                self.assertEqual(first.recover_interrupted(300), 1)
                status = first_db.connection.execute(
                    "SELECT status FROM jobs WHERE job_id='search-1'"
                ).fetchone()[0]
                self.assertEqual(status, "interrupted")

    def test_resume_returns_last_completed_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                job = JobManager(db, "search-1", "search", "park-01")
                job.start()
                job.heartbeat("厂房/a.mp4")
                job.fail("电脑关机")
                resumed = JobManager(db, "search-1", "search", "park-01")
                resumed.start()
                self.assertEqual(resumed.resume_cursor(), "厂房/a.mp4")

    def test_completed_job_starts_new_run_without_old_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                job = JobManager(db, "search-1", "search", "park-01")
                job.start()
                job.heartbeat("厂房/a.mp4")
                job.complete()
                job.start()
                self.assertIsNone(job.resume_cursor())

    def test_lightweight_job_can_run_while_heavy_job_runs(self):
        from media_catalog.workbench_worker import (
            claim_next_heavy_job,
            enqueue_job,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                enqueue_job(db, job_id="scan-1", job_type="scan", payload={})
                claim_next_heavy_job(db, worker_id="worker-a")
                JobManager(db, "search-1", "search", "park-01").start()
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM jobs WHERE status='running'"
                    ).fetchone()[0],
                    2,
                )

    def test_never_retry_job_cannot_be_restarted_by_legacy_manager(self):
        from media_catalog.workbench_worker import (
            claim_next_heavy_job,
            enqueue_job,
            fail_job,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                enqueue_job(
                    db,
                    job_id="generate-1",
                    job_type="generate_broll",
                    payload={"prompt": "园区航拍"},
                    cost_status="approved",
                    provider_request_id="provider-1",
                )
                claim_next_heavy_job(db, worker_id="worker-a")
                fail_job(
                    db,
                    "generate-1",
                    worker_id="worker-a",
                    error="provider uncertain",
                )
                with self.assertRaisesRegex(HeavyJobBusy, "持久队列"):
                    JobManager(
                        db,
                        "generate-1",
                        "generate_broll",
                        "park-01",
                    ).start()

    @staticmethod
    def open_catalog(tmp: str) -> CatalogDatabase:
        db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
        db.migrate()
        register_library(db, "park-01", "园区库", "local", tmp)
        return db


if __name__ == "__main__":
    unittest.main()
