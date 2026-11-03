-- 022: queued/cancelled job states plus durable worker ownership and attempts.

CREATE TABLE jobs_v22 (
  job_id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,
  library_id TEXT REFERENCES libraries(library_id),
  status TEXT NOT NULL CHECK(status IN (
    'pending', 'queued', 'running', 'completed', 'failed',
    'cancelled', 'interrupted', 'stale'
  )),
  cursor TEXT,
  heartbeat_at TEXT,
  error_summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
  progress_current INTEGER NOT NULL DEFAULT 0 CHECK(progress_current >= 0),
  progress_total INTEGER NOT NULL DEFAULT 0 CHECK(progress_total >= 0),
  phase_label TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
  cancel_reason TEXT,
  started_at TEXT,
  finished_at TEXT,
  worker_id TEXT,
  retry_policy TEXT NOT NULL DEFAULT 'explicit'
    CHECK(retry_policy IN ('explicit', 'never'))
);

INSERT INTO jobs_v22(
  job_id, job_type, library_id, status, cursor, heartbeat_at,
  error_summary, created_at, updated_at, task_id, progress_current,
  progress_total, phase_label, cancel_requested, cancel_reason,
  started_at, finished_at, worker_id, retry_policy
)
SELECT
  job_id, job_type, library_id, status, cursor, heartbeat_at,
  error_summary, created_at, updated_at, task_id, progress_current,
  progress_total, phase_label, cancel_requested, cancel_reason,
  started_at, finished_at, NULL, 'explicit'
FROM jobs;

CREATE TABLE job_payloads_v22(
  job_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  cost_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK(cost_status IN ('unknown', 'estimated', 'approved', 'incurred', 'waived')),
  provider_request_id TEXT UNIQUE,
  estimated_cost REAL CHECK(estimated_cost IS NULL OR estimated_cost >= 0),
  actual_cost REAL CHECK(actual_cost IS NULL OR actual_cost >= 0),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(job_id) REFERENCES jobs_v22(job_id) ON DELETE CASCADE
);

INSERT INTO job_payloads_v22(
  job_id, payload_json, cost_status, provider_request_id,
  estimated_cost, actual_cost, created_at, updated_at
)
SELECT
  job_id, payload_json, cost_status, provider_request_id,
  estimated_cost, actual_cost, created_at, updated_at
FROM job_payloads;

DROP TABLE job_payloads;
DROP TABLE jobs;
ALTER TABLE jobs_v22 RENAME TO jobs;
ALTER TABLE job_payloads_v22 RENAME TO job_payloads;

CREATE INDEX idx_jobs_task_created ON jobs(task_id, created_at);
CREATE INDEX idx_jobs_queue ON jobs(status, created_at, job_id);

CREATE TABLE job_attempts(
  job_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
  worker_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'running', 'completed', 'failed', 'cancelled', 'interrupted'
  )),
  error_summary TEXT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  PRIMARY KEY(job_id, attempt_no),
  FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX idx_job_attempts_status
  ON job_attempts(status, started_at);
