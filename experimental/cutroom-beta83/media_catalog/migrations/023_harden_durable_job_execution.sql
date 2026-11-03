-- 023: harden leases, FIFO ordering, provider outcomes, and paid retry policy.

ALTER TABLE jobs ADD COLUMN lease_expires_at TEXT;

ALTER TABLE job_payloads ADD COLUMN payload_digest TEXT;
ALTER TABLE job_payloads ADD COLUMN is_paid INTEGER NOT NULL DEFAULT 0
  CHECK(is_paid IN (0, 1));
ALTER TABLE job_payloads ADD COLUMN outcome_status TEXT NOT NULL DEFAULT 'not_started'
  CHECK(outcome_status IN ('not_started', 'in_progress', 'confirmed', 'unknown'));
ALTER TABLE job_payloads ADD COLUMN provider_state TEXT;
ALTER TABLE job_payloads ADD COLUMN outcome_reason TEXT;

CREATE TABLE job_queue_entries(
  queue_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL UNIQUE,
  enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

INSERT INTO job_queue_entries(job_id, enqueued_at)
SELECT job_id, created_at
FROM jobs
WHERE status='queued'
ORDER BY created_at, rowid;

UPDATE job_payloads
SET is_paid=1
WHERE provider_request_id IS NOT NULL
   OR cost_status IN ('estimated', 'approved', 'incurred')
   OR job_id IN (
     SELECT job_id FROM jobs
     WHERE job_type IN ('ai_storyboard', 'generate_broll', 'seedance', 'tts')
   );

UPDATE jobs
SET retry_policy='never'
WHERE job_id IN (
  SELECT job_id FROM job_payloads WHERE is_paid=1
);

UPDATE job_payloads
SET outcome_status='confirmed',
    provider_state='completed'
WHERE is_paid=1
  AND job_id IN (SELECT job_id FROM jobs WHERE status='completed');

UPDATE job_payloads
SET outcome_status='unknown',
    provider_state='legacy_unconfirmed',
    outcome_reason='历史付费或生成任务结果未经确认，需人工核对',
    cost_status='unknown'
WHERE is_paid=1
  AND job_id IN (
    SELECT job_id FROM jobs
    WHERE status IN ('running', 'failed', 'interrupted')
  );

UPDATE jobs
SET status='interrupted',
    worker_id=NULL,
    finished_at=COALESCE(finished_at, datetime('now')),
    error_summary='供应商结果未经确认，需人工核对',
    lease_expires_at=NULL,
    updated_at=datetime('now')
WHERE job_id IN (
  SELECT job_id FROM job_payloads WHERE outcome_status='unknown'
);

UPDATE jobs
SET lease_expires_at=heartbeat_at
WHERE status='running' AND lease_expires_at IS NULL;

CREATE INDEX idx_job_queue_entries_job
  ON job_queue_entries(job_id, queue_sequence);
CREATE INDEX idx_jobs_active_lease
  ON jobs(status, lease_expires_at, worker_id);
CREATE INDEX idx_job_payloads_outcome
  ON job_payloads(outcome_status, job_id);
