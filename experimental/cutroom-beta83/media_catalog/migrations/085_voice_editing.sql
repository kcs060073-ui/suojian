-- Task-local voice history and explicit generation attempts.
CREATE TABLE voice_edit_heads (
 task_id TEXT PRIMARY KEY,
 revision INTEGER NOT NULL,
 context_digest TEXT NOT NULL,
 config_json TEXT NOT NULL,
 audio_json TEXT
);
CREATE TABLE voice_edit_drafts (
 draft_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES voice_edit_heads(task_id),
 base_revision INTEGER NOT NULL,
 context_digest TEXT NOT NULL,
 config_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','cancelled','applied')),
 applied_revision INTEGER
);
CREATE TABLE voice_edit_requests (
 request_id TEXT PRIMARY KEY,
 draft_id TEXT NOT NULL REFERENCES voice_edit_drafts(draft_id),
 fingerprint TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','running','unknown','failed','ready')),
 audio_json TEXT,
 error_code TEXT,
 cost_json TEXT
);
CREATE INDEX voice_edit_draft_requests ON voice_edit_requests(draft_id);
CREATE TABLE voice_edit_versions (
 task_id TEXT NOT NULL REFERENCES voice_edit_heads(task_id),
 revision INTEGER NOT NULL,
 context_digest TEXT NOT NULL,
 config_json TEXT NOT NULL,
 audio_json TEXT,
 reason TEXT NOT NULL,
 PRIMARY KEY(task_id, revision)
);
