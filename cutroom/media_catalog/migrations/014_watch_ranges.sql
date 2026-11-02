-- 014: server-merged review watch ranges.

CREATE TABLE IF NOT EXISTS review_watch_ranges(
  range_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  start_sec REAL NOT NULL CHECK(start_sec >= 0),
  end_sec REAL NOT NULL CHECK(end_sec >= start_sec),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(task_id, snapshot_id, start_sec, end_sec),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(snapshot_id, task_id)
    REFERENCES render_snapshots(snapshot_id, task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_review_watch_ranges_candidate
  ON review_watch_ranges(task_id, snapshot_id, start_sec, end_sec);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(14, datetime('now'));
