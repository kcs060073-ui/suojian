CREATE TABLE IF NOT EXISTS packaging_audio_jobs (
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) DEFERRABLE INITIALLY DEFERRED,
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 input_digest TEXT NOT NULL,
 items_json TEXT NOT NULL,
 updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_packaging_audio_task ON packaging_audio_jobs(task_id);
