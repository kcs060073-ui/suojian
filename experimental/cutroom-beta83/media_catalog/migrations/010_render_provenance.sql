-- 010: trusted render provenance, gate audit, and candidate-bound review.
ALTER TABLE picture_lock ADD COLUMN selection_digest TEXT;
ALTER TABLE render_snapshots ADD COLUMN producer TEXT;
ALTER TABLE render_snapshots ADD COLUMN input_digest TEXT;

CREATE TABLE IF NOT EXISTS task_gate_events(
  event_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  gate_type TEXT NOT NULL CHECK(gate_type IN ('brief','picture_lock','packaging')),
  gate_digest TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_gate_events_task ON task_gate_events(task_id, gate_type, created_at);

CREATE TABLE IF NOT EXISTS review_approvals(
  approval_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  qa_report_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
  reviewed_by TEXT NOT NULL,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(snapshot_id) REFERENCES render_snapshots(snapshot_id) ON DELETE CASCADE,
  FOREIGN KEY(qa_report_id) REFERENCES qa_reports(report_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_review_approvals_candidate
  ON review_approvals(task_id, snapshot_id, qa_report_id, created_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(10, datetime('now'));
