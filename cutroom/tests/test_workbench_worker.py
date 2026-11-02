import importlib
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog.jobs import HeavyJobBusy, JobManager
from media_catalog.libraries import register_library
from media_catalog.tasks import create_task


def worker_api():
    """Load the wished-for Task 7 API lazily so every RED case is discoverable."""
    return importlib.import_module("media_catalog.workbench_worker")


class WorkbenchWorkerTests(unittest.TestCase):
    BASE_TIME = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)

    def test_packaging_preview_jobs_are_local_nonpaid_heavy_fifo_work(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                create_task(db, "task-1", "包装预览", "digital_human")
                api.enqueue_job(
                    db,
                    job_id="packaging-preview-1",
                    job_type="packaging_preview",
                    task_id="task-1",
                    payload={"preview_id": "pv-1"},
                )
                api.enqueue_job(
                    db,
                    job_id="packaging-catalog-2",
                    job_type="packaging_catalog_check",
                    payload={"scope": "all"},
                )

                claimed = api.claim_next_heavy_job(
                    db, worker_id="worker-packaging"
                )

                self.assertEqual(claimed.job_id, "packaging-preview-1")
                self.assertFalse(claimed.is_paid)
                self.assertIsNone(
                    api.claim_next_heavy_job(db, worker_id="worker-second")
                )
                self.assertEqual(
                    self.job_status(db, "packaging-catalog-2"), "queued"
                )

    def test_second_heavy_job_stays_queued_until_the_global_slot_is_free(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(db, job_id="render-1", job_type="render", payload={})
                api.enqueue_job(db, job_id="scan-2", job_type="scan", payload={})

                first = api.claim_next_heavy_job(db, worker_id="worker-a")
                second = api.claim_next_heavy_job(db, worker_id="worker-b")

                self.assertEqual(first.job_id, "render-1")
                self.assertIsNone(second)
                self.assertEqual(self.job_status(db, "scan-2"), "queued")

    def test_all_heavy_types_share_one_persistent_queue(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                for index, job_type in enumerate(sorted(JobManager.HEAVY_TYPES)):
                    api.enqueue_job(
                        db,
                        job_id=f"heavy-{index}",
                        job_type=job_type,
                        payload={"index": index},
                    )

                claimed = api.claim_next_heavy_job(db, worker_id="worker-a")
                self.assertIsNotNone(claimed)
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM jobs WHERE status='running'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM jobs WHERE status='queued'"
                    ).fetchone()[0],
                    len(JobManager.HEAVY_TYPES) - 1,
                )

    def test_two_workers_compete_atomically_and_only_one_claims(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            with self.open_catalog(tmp) as db:
                api.enqueue_job(db, job_id="render-1", job_type="render", payload={})

            barrier = threading.Barrier(2)
            claims = []
            errors = []

            def compete(worker_id):
                try:
                    with CatalogDatabase.open(path) as db:
                        barrier.wait()
                        claim = api.claim_next_heavy_job(db, worker_id=worker_id)
                        claims.append(None if claim is None else claim.job_id)
                except BaseException as error:  # Preserve thread failures for assertion.
                    errors.append(error)

            threads = [
                threading.Thread(target=compete, args=("worker-a",)),
                threading.Thread(target=compete, args=("worker-b",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertFalse(errors)
            self.assertCountEqual(claims, ["render-1", None])

    def test_worker_processes_persisted_job_without_browser_or_session(self):
        api = worker_api()
        handled = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            with self.open_catalog(tmp) as browser_request_db:
                api.enqueue_job(
                    browser_request_db,
                    job_id="render-1",
                    job_type="render",
                    payload={"task_id": "task-1"},
                )

            with CatalogDatabase.open(path) as worker_db:
                worker = api.WorkbenchWorker(
                    worker_db,
                    worker_id="worker-after-browser-closed",
                    handlers={"render": lambda job: handled.append(job.payload)},
                )
                result = worker.run_once()

                self.assertEqual(result.job_id, "render-1")
                self.assertEqual(handled, [{"task_id": "task-1"}])
                self.assertEqual(self.job_status(worker_db, "render-1"), "completed")

    def test_worker_persists_stable_action_cursor_progress_and_phase(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db,
                    job_id="render-result",
                    job_type="render",
                    payload={"task_id": "task-1"},
                )
                worker = api.WorkbenchWorker(
                    db,
                    worker_id="worker-result",
                    handlers={
                        "render": lambda _job: {
                            "cursor": "render_and_qa:complete",
                            "progress": {"current": 1, "total": 1},
                            "phase_label": "精剪成片与自动质检完成",
                            "result": {"output_path": "out/task-1/video-final.mp4"},
                        }
                    },
                )
                worker.run_once()

                row = db.connection.execute(
                    "SELECT status,cursor,progress_current,progress_total,phase_label "
                    "FROM jobs WHERE job_id='render-result'"
                ).fetchone()
                self.assertEqual(row["status"], "completed")
                self.assertEqual(row["cursor"], "render_and_qa:complete")
                self.assertEqual((row["progress_current"], row["progress_total"]), (1, 1))
                self.assertEqual(row["phase_label"], "精剪成片与自动质检完成")

    def test_active_lease_is_not_recovered_by_another_worker_and_can_be_renewed(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(db, job_id="render-1", job_type="render", payload={})
                api.claim_next_heavy_job(
                    db,
                    worker_id="worker-a",
                    clock=lambda: self.BASE_TIME,
                    lease_seconds=60,
                )
                original_lease = db.connection.execute(
                    "SELECT lease_expires_at FROM jobs WHERE job_id='render-1'"
                ).fetchone()[0]

                recovered = api.recover_interrupted_jobs(
                    db,
                    worker_id="worker-b",
                    clock=lambda: self.BASE_TIME + timedelta(seconds=30),
                )
                self.assertEqual(recovered, 0)
                self.assertEqual(self.job_status(db, "render-1"), "running")

                api.renew_job_lease(
                    db,
                    "render-1",
                    worker_id="worker-a",
                    clock=lambda: self.BASE_TIME + timedelta(seconds=30),
                    lease_seconds=60,
                )
                renewed_lease = db.connection.execute(
                    "SELECT lease_expires_at FROM jobs WHERE job_id='render-1'"
                ).fetchone()[0]
                self.assertGreater(renewed_lease, original_lease)

    def test_only_expired_lease_is_recovered_even_for_same_worker_id(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db, job_id="render-1", job_type="render", payload={}
                )
                api.claim_next_heavy_job(
                    db,
                    worker_id="worker-a",
                    clock=lambda: self.BASE_TIME,
                    lease_seconds=60,
                )
                self.assertEqual(
                    api.recover_interrupted_jobs(
                        db,
                        worker_id="worker-a",
                        clock=lambda: self.BASE_TIME + timedelta(seconds=30),
                    ),
                    0,
                )
                self.assertEqual(self.job_status(db, "render-1"), "running")
                self.assertEqual(
                    api.recover_interrupted_jobs(
                        db,
                        worker_id="worker-b",
                        clock=lambda: self.BASE_TIME + timedelta(seconds=61),
                    ),
                    1,
                )
                self.assertEqual(
                    self.job_status(db, "render-1"), "interrupted"
                )

    def test_long_handler_renews_lease_automatically(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            with self.open_catalog(tmp) as request_db:
                api.enqueue_job(
                    request_db,
                    job_id="render-long",
                    job_type="render",
                    payload={},
                )

            clock_value = [self.BASE_TIME]
            handler_started = threading.Event()
            release_handler = threading.Event()
            worker_errors = []

            def handler(_job):
                handler_started.set()
                release_handler.wait(timeout=5)

            def run_worker():
                try:
                    with CatalogDatabase.open(path) as worker_db:
                        api.WorkbenchWorker(
                            worker_db,
                            worker_id="worker-a",
                            handlers={"render": handler},
                            clock=lambda: clock_value[0],
                            lease_seconds=1,
                            heartbeat_interval=0.02,
                        ).run_once()
                except BaseException as error:
                    worker_errors.append(error)

            worker_thread = threading.Thread(target=run_worker)
            worker_thread.start()
            self.assertTrue(handler_started.wait(timeout=5))

            with CatalogDatabase.open(path) as observer_db:
                original_lease = observer_db.connection.execute(
                    "SELECT lease_expires_at FROM jobs WHERE job_id='render-long'"
                ).fetchone()[0]
                clock_value[0] = self.BASE_TIME + timedelta(milliseconds=800)
                renewed_lease = original_lease
                for _ in range(100):
                    renewed_lease = observer_db.connection.execute(
                        "SELECT lease_expires_at FROM jobs WHERE job_id='render-long'"
                    ).fetchone()[0]
                    if renewed_lease > original_lease:
                        break
                    time.sleep(0.01)
                self.assertGreater(renewed_lease, original_lease)
                self.assertEqual(
                    api.recover_interrupted_jobs(
                        observer_db,
                        worker_id="worker-b",
                        clock=lambda: self.BASE_TIME
                        + timedelta(milliseconds=1100),
                    ),
                    0,
                )

            release_handler.set()
            worker_thread.join(timeout=5)
            self.assertFalse(worker_thread.is_alive())
            self.assertFalse(worker_errors)
            with CatalogDatabase.open(path) as final_db:
                self.assertEqual(
                    self.job_status(final_db, "render-long"), "completed"
                )

    def test_heartbeat_failure_cannot_silently_complete_the_job(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db,
                    job_id="render-heartbeat-failed",
                    job_type="render",
                    payload={},
                )
                heartbeat_attempted = threading.Event()

                def fail_heartbeat(*_args, **_kwargs):
                    heartbeat_attempted.set()
                    raise RuntimeError("heartbeat database unavailable")

                def handler(_job):
                    self.assertTrue(heartbeat_attempted.wait(timeout=5))

                with patch.object(
                    api, "renew_job_lease", side_effect=fail_heartbeat
                ):
                    api.WorkbenchWorker(
                        db,
                        worker_id="worker-a",
                        handlers={"render": handler},
                        heartbeat_interval=0.01,
                    ).run_once()

                row = db.connection.execute(
                    "SELECT status, error_summary FROM jobs "
                    "WHERE job_id='render-heartbeat-failed'"
                ).fetchone()
                self.assertEqual(row["status"], "interrupted")
                self.assertIn("心跳续租失败", row["error_summary"])

    def test_cancel_request_only_becomes_cancelled_at_a_safe_point(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(db, job_id="render-1", job_type="render", payload={})
                api.claim_next_heavy_job(db, worker_id="worker-a")

                api.request_cancel(db, "render-1", reason="同事取消")
                self.assertEqual(self.job_status(db, "render-1"), "running")

                self.assertTrue(
                    api.cancel_at_safe_point(db, "render-1", worker_id="worker-a")
                )
                self.assertEqual(self.job_status(db, "render-1"), "cancelled")

    def test_queued_and_interrupted_cancel_reach_terminal_cancelled(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db, job_id="queued-cancel", job_type="render", payload={}
                )
                api.request_cancel(db, "queued-cancel", reason="排队时取消")
                self.assertEqual(
                    self.job_status(db, "queued-cancel"), "cancelled"
                )
                self.assertIsNone(
                    api.claim_next_heavy_job(db, worker_id="worker-a")
                )

                api.enqueue_job(
                    db, job_id="interrupted-cancel", job_type="scan", payload={}
                )
                api.claim_next_heavy_job(
                    db,
                    worker_id="worker-a",
                    clock=lambda: self.BASE_TIME,
                    lease_seconds=1,
                )
                api.recover_interrupted_jobs(
                    db,
                    worker_id="worker-a",
                    clock=lambda: self.BASE_TIME + timedelta(seconds=2),
                )
                api.request_cancel(
                    db, "interrupted-cancel", reason="恢复前取消"
                )
                self.assertEqual(
                    self.job_status(db, "interrupted-cancel"), "cancelled"
                )

    def test_failed_or_cancelled_job_releases_the_next_queued_job(self):
        api = worker_api()
        for terminal_status in ("failed", "cancelled"):
            with self.subTest(terminal_status=terminal_status):
                with tempfile.TemporaryDirectory() as tmp:
                    with self.open_catalog(tmp) as db:
                        api.enqueue_job(db, job_id="render-1", job_type="render", payload={})
                        api.enqueue_job(db, job_id="scan-2", job_type="scan", payload={})
                        api.claim_next_heavy_job(db, worker_id="worker-a")

                        if terminal_status == "failed":
                            api.fail_job(
                                db,
                                "render-1",
                                worker_id="worker-a",
                                error="ffmpeg failed",
                            )
                        else:
                            api.request_cancel(db, "render-1", reason="同事取消")
                            api.cancel_at_safe_point(
                                db, "render-1", worker_id="worker-a"
                            )

                        next_job = api.claim_next_heavy_job(
                            db, worker_id="worker-b"
                        )
                        self.assertEqual(next_job.job_id, "scan-2")

    def test_paid_provider_failure_persists_unknown_outcome_and_never_retries(self):
        api = worker_api()
        calls = []

        def fail_generation(job):
            calls.append(job.job_id)
            raise RuntimeError("provider timeout after request accepted")

        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db,
                    job_id="generate-1",
                    job_type="generate_broll",
                    payload={"prompt": "园区航拍"},
                    cost_status="approved",
                    provider_request_id="provider-request-001",
                    retry_policy="never",
                )
                worker = api.WorkbenchWorker(
                    db,
                    worker_id="worker-a",
                    handlers={"generate_broll": fail_generation},
                )

                worker.run_once()
                self.assertIsNone(worker.run_once())
                self.assertEqual(calls, ["generate-1"])
                self.assertEqual(
                    self.job_status(db, "generate-1"), "interrupted"
                )
                outcome = db.connection.execute(
                    """
                    SELECT outcome_status, provider_state, cost_status,
                           outcome_reason
                    FROM job_payloads WHERE job_id='generate-1'
                    """
                ).fetchone()
                self.assertEqual(outcome["outcome_status"], "unknown")
                self.assertEqual(outcome["cost_status"], "unknown")
                self.assertTrue(outcome["provider_state"])
                self.assertIn("provider timeout", outcome["outcome_reason"])
                with self.assertRaisesRegex(RuntimeError, "不能重试"):
                    api.resume_job(db, "generate-1")

    def test_non_generate_paid_type_is_forced_to_never_retry(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db,
                    job_id="tts-1",
                    job_type="tts",
                    payload={"text": "园区介绍"},
                    cost_status="approved",
                    provider_request_id="tts-request-1",
                    retry_policy="explicit",
                )
                row = db.connection.execute(
                    """
                    SELECT j.retry_policy, p.is_paid
                    FROM jobs AS j JOIN job_payloads AS p USING(job_id)
                    WHERE j.job_id='tts-1'
                    """
                ).fetchone()
                self.assertEqual(tuple(row), ("never", 1))

    def test_crashed_paid_request_recovers_as_unknown_not_failed(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db,
                    job_id="tts-crash",
                    job_type="tts",
                    payload={"text": "园区介绍"},
                    cost_status="approved",
                    provider_request_id="tts-request-crash",
                )
                api.claim_next_heavy_job(
                    db,
                    worker_id="dead-worker",
                    clock=lambda: self.BASE_TIME,
                    lease_seconds=10,
                )
                api.recover_interrupted_jobs(
                    db,
                    worker_id="new-worker",
                    clock=lambda: self.BASE_TIME + timedelta(seconds=11),
                )
                row = db.connection.execute(
                    """
                    SELECT j.status, j.retry_policy, p.outcome_status,
                           p.provider_state
                    FROM jobs AS j JOIN job_payloads AS p USING(job_id)
                    WHERE j.job_id='tts-crash'
                    """
                ).fetchone()
                self.assertEqual(
                    tuple(row),
                    ("interrupted", "never", "unknown", "worker_crashed"),
                )

    def test_provider_request_id_is_persistent_and_idempotent(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            with self.open_catalog(tmp) as db:
                first_id = api.enqueue_job(
                    db,
                    job_id="generate-1",
                    job_type="generate_broll",
                    payload={"prompt": "园区航拍"},
                    cost_status="approved",
                    provider_request_id="provider-request-001",
                    retry_policy="never",
                )

            with CatalogDatabase.open(path) as reopened_db:
                duplicate_id = api.enqueue_job(
                    reopened_db,
                    job_id="generate-duplicate",
                    job_type="generate_broll",
                    payload={"prompt": "园区航拍"},
                    cost_status="approved",
                    provider_request_id="provider-request-001",
                    retry_policy="never",
                )
                self.assertEqual(first_id, "generate-1")
                self.assertEqual(duplicate_id, "generate-1")
                self.assertEqual(
                    reopened_db.connection.execute(
                        "SELECT count(*) FROM job_payloads WHERE provider_request_id=?",
                        ("provider-request-001",),
                    ).fetchone()[0],
                    1,
                )

    def test_provider_request_id_stays_idempotent_after_terminal_outcomes(self):
        api = worker_api()
        cases = (
            ("confirmed", lambda _job: None),
            (
                "unknown",
                lambda _job: (_ for _ in ()).throw(
                    api.ProviderOutcomeUnknown("供应商已接收但结果未知")
                ),
            ),
        )
        for expected_outcome, handler in cases:
            with self.subTest(expected_outcome=expected_outcome):
                with tempfile.TemporaryDirectory() as tmp:
                    with self.open_catalog(tmp) as db:
                        api.enqueue_job(
                            db,
                            job_id="tts-original",
                            job_type="tts",
                            payload={"text": "园区介绍"},
                            cost_status="approved",
                            provider_request_id="tts-request-terminal",
                        )
                        api.WorkbenchWorker(
                            db,
                            worker_id="worker-a",
                            handlers={"tts": handler},
                        ).run_once()

                        duplicate_id = api.enqueue_job(
                            db,
                            job_id="tts-duplicate",
                            job_type="tts",
                            payload={"text": "园区介绍"},
                            cost_status="approved",
                            provider_request_id="tts-request-terminal",
                        )

                        self.assertEqual(duplicate_id, "tts-original")
                        self.assertEqual(
                            db.connection.execute(
                                "SELECT outcome_status FROM job_payloads "
                                "WHERE job_id='tts-original'"
                            ).fetchone()[0],
                            expected_outcome,
                        )

    def test_waived_provider_job_does_not_claim_cost_was_incurred(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db,
                    job_id="tts-waived",
                    job_type="tts",
                    payload={"text": "园区介绍"},
                    cost_status="waived",
                    provider_request_id="tts-request-waived",
                )
                api.WorkbenchWorker(
                    db,
                    worker_id="worker-a",
                    handlers={"tts": lambda _job: None},
                ).run_once()

                row = db.connection.execute(
                    "SELECT requested_cost_status, cost_status, outcome_status "
                    "FROM job_payloads WHERE job_id='tts-waived'"
                ).fetchone()
                self.assertEqual(
                    tuple(row), ("waived", "waived", "confirmed")
                )

    def test_provider_request_id_rejects_any_nonidentical_request(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                db.connection.executemany(
                    "INSERT INTO tasks(task_id, title) VALUES(?, ?)",
                    (("task-a", "任务A"), ("task-b", "任务B")),
                )
                db.connection.commit()
                api.enqueue_job(
                    db,
                    job_id="original",
                    task_id="task-a",
                    job_type="tts",
                    payload={"text": "原文"},
                    cost_status="approved",
                    provider_request_id="provider-same",
                )
                collisions = (
                    {
                        "task_id": "task-b",
                        "job_type": "tts",
                        "payload": {"text": "原文"},
                        "cost_status": "approved",
                    },
                    {
                        "task_id": "task-a",
                        "job_type": "tts",
                        "payload": {"text": "改文"},
                        "cost_status": "approved",
                    },
                    {
                        "task_id": "task-a",
                        "job_type": "render",
                        "payload": {"text": "原文"},
                        "cost_status": "approved",
                    },
                    {
                        "task_id": "task-a",
                        "job_type": "tts",
                        "payload": {"text": "原文"},
                        "cost_status": "waived",
                    },
                )
                for index, request in enumerate(collisions):
                    with self.subTest(request=request):
                        with self.assertRaises(api.JobRequestConflict) as caught:
                            api.enqueue_job(
                                db,
                                job_id=f"collision-{index}",
                                provider_request_id="provider-same",
                                **request,
                            )
                        self.assertEqual(
                            caught.exception.code,
                            "JOB_IDEMPOTENCY_CONFLICT",
                        )

    def test_provider_request_id_cannot_move_between_libraries(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                register_library(db, "park-02", "园区二", "local", tmp)
                api.enqueue_job(
                    db,
                    job_id="provider-library-original",
                    job_type="tts",
                    library_id="park-01",
                    payload={"text": "园区介绍"},
                    cost_status="approved",
                    provider_request_id="provider-library-request",
                )

                with self.assertRaises(api.JobRequestConflict):
                    api.enqueue_job(
                        db,
                        job_id="provider-library-collision",
                        job_type="tts",
                        library_id="park-02",
                        payload={"text": "园区介绍"},
                        cost_status="approved",
                        provider_request_id="provider-library-request",
                    )

    def test_enqueue_persists_task_id_and_fifo_ignores_job_id_order(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                db.connection.execute(
                    "INSERT INTO tasks(task_id, title) VALUES('task-a', '任务A')"
                )
                db.connection.commit()
                api.enqueue_job(
                    db,
                    job_id="z-first",
                    task_id="task-a",
                    job_type="render",
                    payload={},
                )
                api.enqueue_job(
                    db,
                    job_id="a-second",
                    task_id="task-a",
                    job_type="scan",
                    payload={},
                )
                db.connection.execute(
                    """
                    UPDATE jobs SET created_at='2026-09-07 08:00:00'
                    WHERE job_id IN ('z-first', 'a-second')
                    """
                )
                db.connection.commit()

                first = api.claim_next_heavy_job(
                    db, worker_id="worker-a"
                )
                self.assertEqual(first.job_id, "z-first")
                self.assertEqual(
                    db.connection.execute(
                        "SELECT task_id FROM jobs WHERE job_id='z-first'"
                    ).fetchone()[0],
                    "task-a",
                )

    def test_deterministic_job_requires_explicit_resume_and_audits_attempts(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db,
                    job_id="scan-1",
                    job_type="scan",
                    payload={"root": "园区A"},
                    retry_policy="explicit",
                )
                first = api.claim_next_heavy_job(
                    db,
                    worker_id="worker-a",
                    clock=lambda: self.BASE_TIME,
                    lease_seconds=1,
                )
                self.assertEqual(first.attempt_no, 1)
                api.recover_interrupted_jobs(
                    db,
                    worker_id="worker-a",
                    clock=lambda: self.BASE_TIME + timedelta(seconds=2),
                )

                self.assertIsNone(
                    api.claim_next_heavy_job(db, worker_id="worker-b")
                )
                api.resume_job(db, "scan-1")
                second = api.claim_next_heavy_job(db, worker_id="worker-b")

                self.assertEqual(second.attempt_no, 2)
                attempts = api.list_job_attempts(db, "scan-1")
                self.assertEqual(
                    [attempt.attempt_no for attempt in attempts],
                    [1, 2],
                )
                self.assertEqual(attempts[0].status, "interrupted")

    def test_run_forever_continues_after_terminal_jobs_without_browser(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            with self.open_catalog(tmp) as request_db:
                api.enqueue_job(
                    request_db, job_id="render-fail", job_type="render", payload={}
                )
                api.enqueue_job(
                    request_db, job_id="scan-ok", job_type="scan", payload={}
                )

            handled = []
            stop_event = threading.Event()

            def fail(_job):
                handled.append("failed")
                raise RuntimeError("deterministic failure")

            def succeed(_job):
                handled.append("completed")
                stop_event.set()

            with CatalogDatabase.open(path) as worker_db:
                worker = api.WorkbenchWorker(
                    worker_db,
                    worker_id="background-worker",
                    handlers={"render": fail, "scan": succeed},
                )
                worker.run_forever(
                    stop_event,
                    poll_interval=0,
                    poll=lambda _seconds: None,
                )
                self.assertEqual(handled, ["failed", "completed"])
                self.assertEqual(
                    [
                        tuple(row)
                        for row in worker_db.connection.execute(
                            """
                            SELECT job_id, status FROM jobs
                            ORDER BY created_at, job_id
                            """
                        )
                    ],
                    [("render-fail", "failed"), ("scan-ok", "completed")],
                )

    def test_cli_handle_never_claims_a_non_cli_job_with_same_type_and_library(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                register_library(db, "park", "园区", "local", tmp)
                api.enqueue_job(
                    db,
                    job_id="workbench-scan",
                    job_type="scan",
                    library_id="park",
                    payload={"owner": "workbench"},
                )
                cli_job = api.DurableCliJob(
                    db, "scan:park", "scan", "park"
                )

                with self.assertRaises(HeavyJobBusy):
                    cli_job.start()

                statuses = {
                    row["job_id"]: row["status"]
                    for row in db.connection.execute(
                        "SELECT job_id, status FROM jobs"
                    )
                }
                self.assertEqual(statuses["workbench-scan"], "queued")
                self.assertEqual(statuses["scan:park"], "queued")

    def test_cli_handle_resumes_cursor_then_starts_a_new_audited_run(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                first = api.DurableCliJob(
                    db, "scan:park-01", "scan", "park-01"
                )
                first.recover_interrupted(300)
                first.start()
                first.heartbeat("scan:园区/厂房.mp4")
                first.fail("temporary scan failure")

                resumed = api.DurableCliJob(
                    db, "scan:park-01", "scan", "park-01"
                )
                resumed.recover_interrupted(300)
                resumed.start()
                self.assertEqual(resumed.job_id, first.job_id)
                self.assertEqual(
                    resumed.resume_cursor(), "scan:园区/厂房.mp4"
                )
                resumed.complete()

                repeated = api.DurableCliJob(
                    db, "scan:park-01", "scan", "park-01"
                )
                repeated.start()
                self.assertNotEqual(repeated.job_id, resumed.job_id)
                repeated.complete()
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM job_attempts "
                        "WHERE job_id IN (?, ?)",
                        (resumed.job_id, repeated.job_id),
                    ).fetchone()[0],
                    3,
                )

    def test_queued_job_pauses_immediately_and_resume_requeues_at_tail(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(db, job_id="queued-1", job_type="render", payload={})
                api.enqueue_job(db, job_id="queued-2", job_type="scan", payload={})

                api.request_job_pause(db, "queued-1")

                self.assertEqual(api.get_job(db, "queued-1")["public_status"], "paused")
                self.assertEqual(api.get_job(db, "queued-2")["queue_position"], 1)
                api.resume_job(db, "queued-1")
                self.assertEqual(api.get_job(db, "queued-1")["queue_position"], 2)

    def test_running_paid_job_pause_waits_for_checkpoint_without_resetting_outcome(self):
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(
                    db,
                    job_id="paid-running",
                    job_type="tts",
                    payload={"text": "园区介绍"},
                    cost_status="approved",
                    provider_request_id="pause-paid-1",
                )

                def handler(job):
                    api.request_job_pause(db, job.job_id)
                    self.assertEqual(api.get_job(db, job.job_id)["public_status"], "running")
                    self.assertTrue(api.get_job(db, job.job_id)["pause_requested"])
                    return {
                        "cursor": "tts:checkpoint",
                        "progress": {"current": 1, "total": 2},
                        "phase_label": "配音已返回，等待后续合成",
                        "result": {"audio": "ready"},
                    }

                worker = api.WorkbenchWorker(
                    db,
                    worker_id="worker-pause",
                    handlers={"tts": handler},
                )
                worker.run_once()

                paused = api.get_job(db, "paid-running")
                self.assertEqual(paused["public_status"], "paused")
                self.assertEqual(paused["cursor"], "tts:checkpoint")
                outcome = db.connection.execute(
                    "SELECT outcome_status FROM job_payloads WHERE job_id='paid-running'"
                ).fetchone()[0]
                self.assertEqual(outcome, "in_progress")

    def test_eta_uses_completed_same_type_samples_and_progress(self):
        timing_api = importlib.import_module("media_catalog.job_timing")
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(db, job_id="render-sample", job_type="render", payload={})
                with db.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status='completed',started_at='2026-09-07T08:00:00Z',
                            finished_at='2026-09-07T08:01:00Z'
                        WHERE job_id='render-sample'
                        """
                    )
                    connection.execute(
                        "DELETE FROM job_queue_entries WHERE job_id='render-sample'"
                    )
                api.enqueue_job(db, job_id="render-running", job_type="render", payload={})
                api.claim_next_heavy_job(
                    db,
                    worker_id="worker-eta",
                    clock=lambda: self.BASE_TIME + timedelta(minutes=2),
                )
                with db.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE jobs SET progress_current=1,progress_total=2
                        WHERE job_id='render-running'
                        """
                    )

                timing = timing_api.job_timing(
                    db,
                    api.get_job(db, "render-running"),
                    observed_at=self.BASE_TIME + timedelta(minutes=2, seconds=10),
                )

                self.assertEqual(timing["estimate_status"], "estimated")
                self.assertEqual(timing["estimated_remaining_seconds"], 30)

    def test_eta_is_unknown_without_valid_samples(self):
        timing_api = importlib.import_module("media_catalog.job_timing")
        api = worker_api()
        with tempfile.TemporaryDirectory() as tmp:
            with self.open_catalog(tmp) as db:
                api.enqueue_job(db, job_id="new-kind", job_type="scan", payload={})

                timing = timing_api.job_timing(
                    db,
                    api.get_job(db, "new-kind"),
                    observed_at=self.BASE_TIME,
                )

                self.assertIsNone(timing["estimated_start_at"])
                self.assertEqual(timing["estimate_status"], "unknown")

    @staticmethod
    def job_status(db, job_id):
        return db.connection.execute(
            "SELECT status FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0]

    @staticmethod
    def open_catalog(tmp):
        db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
        db.migrate()
        register_library(db, "park-01", "园区库", "local", tmp)
        return db


if __name__ == "__main__":
    unittest.main()
