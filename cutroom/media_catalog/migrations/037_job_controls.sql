-- 037: checkpoint-safe pause controls without weakening durable job status rules.

CREATE TABLE job_controls(
  job_id TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT 'active'
    CHECK(state IN ('active','pause_requested','paused')),
  requested_at TEXT,
  paused_at TEXT,
  FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

INSERT INTO job_controls(job_id,state)
SELECT job_id,'active' FROM jobs;

CREATE INDEX idx_job_controls_state ON job_controls(state,job_id);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(37, datetime('now'));
