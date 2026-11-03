CREATE TABLE audio_review_runs (
 run_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 candidate_id TEXT NOT NULL,
 inputs_digest TEXT NOT NULL,
 binding_json TEXT NOT NULL,
 account_digest TEXT NOT NULL,
 start_seconds REAL NOT NULL,
 end_seconds REAL NOT NULL,
 status TEXT NOT NULL,
 parsed_json TEXT,
 response_path TEXT,
 response_sha256 TEXT,
 audio_sha256 TEXT,
 stale_at_completion INTEGER NOT NULL DEFAULT 0,
 error_reason TEXT,
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audio_review_task ON audio_review_runs(task_id,candidate_id,created_at);