-- 025: bind human approval to server-observed coverage of one trusted candidate.

CREATE TABLE review_watch_sessions(
  session_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  qa_report_id TEXT NOT NULL,
  output_signature TEXT NOT NULL,
  duration_seconds REAL NOT NULL CHECK(duration_seconds > 0),
  covered_seconds REAL NOT NULL DEFAULT 0 CHECK(covered_seconds >= 0),
  last_position_seconds REAL NOT NULL DEFAULT 0
    CHECK(last_position_seconds >= 0),
  reached_end INTEGER NOT NULL DEFAULT 0 CHECK(reached_end IN (0, 1)),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active', 'completed')),
  started_at TEXT NOT NULL,
  last_tick_at TEXT NOT NULL,
  completed_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(snapshot_id, task_id)
    REFERENCES render_snapshots(snapshot_id, task_id) ON DELETE CASCADE,
  FOREIGN KEY(qa_report_id) REFERENCES qa_reports(report_id) ON DELETE CASCADE
);

CREATE TABLE review_watch_intervals(
  interval_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  start_seconds REAL NOT NULL CHECK(start_seconds >= 0),
  end_seconds REAL NOT NULL CHECK(end_seconds > start_seconds),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(session_id) REFERENCES review_watch_sessions(session_id)
    ON DELETE CASCADE
);

CREATE INDEX idx_review_watch_candidate
  ON review_watch_sessions(task_id, snapshot_id, qa_report_id, status);
CREATE INDEX idx_review_watch_intervals_session
  ON review_watch_intervals(session_id, start_seconds, end_seconds);

ALTER TABLE review_approvals ADD COLUMN watch_session_id TEXT
  REFERENCES review_watch_sessions(session_id);
