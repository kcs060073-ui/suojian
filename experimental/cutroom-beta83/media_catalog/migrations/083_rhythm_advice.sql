-- Immutable local advice, outside all business approval and render tables.
CREATE TABLE rhythm_reports (
 report_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
 task_revision TEXT NOT NULL,
 inputs_digest TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 UNIQUE(task_id,task_revision,inputs_digest)
);
CREATE TABLE task_rhythm_current (
 task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
 report_id TEXT NOT NULL REFERENCES rhythm_reports(report_id)
);
