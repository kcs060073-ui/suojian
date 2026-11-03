-- Server-owned generation attempts and immutable full PCM voice registrations.
CREATE TABLE voice_generation_runs (
 request_id TEXT PRIMARY KEY REFERENCES voice_edit_requests(request_id),
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id) DEFERRABLE INITIALLY DEFERRED,
 input_json TEXT NOT NULL,
 parameters_json TEXT,
 status TEXT NOT NULL CHECK(status IN ('queued','running','ready','failed','unknown')),
 execution_json TEXT,
 bundle_id TEXT,
 created_at TEXT NOT NULL DEFAULT(datetime('now'))
);
CREATE TABLE voice_registered_bundles (
 bundle_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 draft_id TEXT NOT NULL REFERENCES voice_edit_drafts(draft_id),
 request_id TEXT NOT NULL UNIQUE REFERENCES voice_generation_runs(request_id),
 context_digest TEXT NOT NULL,
 config_fingerprint TEXT NOT NULL,
 package_path TEXT NOT NULL,
 audio_json TEXT NOT NULL,
 timeline_json TEXT NOT NULL,
 files_json TEXT NOT NULL,
 execution_digest TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT(datetime('now'))
);
CREATE INDEX voice_bundles_task ON voice_registered_bundles(task_id);
CREATE TRIGGER voice_bundle_immutable_update BEFORE UPDATE ON voice_registered_bundles BEGIN
 SELECT RAISE(ABORT,'voice bundle registration is immutable'); END;
CREATE TRIGGER voice_bundle_immutable_delete BEFORE DELETE ON voice_registered_bundles BEGIN
 SELECT RAISE(ABORT,'voice bundle registration is immutable'); END;
