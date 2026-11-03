-- A creation-time source check has its own durable state, separate from the
-- catalog snapshot and from the later material_sync workflow stage.
CREATE TABLE task_source_checks(
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  job_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN (
    'queued', 'running', 'ready', 'review_required', 'offline', 'failed'
  )),
  source_revision INTEGER NOT NULL,
  summary_json TEXT,
  checked_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
