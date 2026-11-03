-- 012: durable, auditable rough-cut edit history.

CREATE TABLE IF NOT EXISTS rough_cut_edits(
  edit_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  impact_digest TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending', 'applied', 'reverted')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rough_cut_edits_task
  ON rough_cut_edits(task_id, created_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(12, datetime('now'));
