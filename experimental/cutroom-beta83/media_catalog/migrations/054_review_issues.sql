-- Frozen Step 06: human observations are independent of automatic QA and
-- rework decisions. Withdrawing a mistaken note keeps an audit record.
CREATE TABLE review_issues(
  issue_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  candidate_id TEXT NOT NULL REFERENCES candidate_versions(candidate_id),
  category TEXT NOT NULL CHECK(category IN(
    'material','packaging','script','audio','technical'
  )),
  notes TEXT NOT NULL CHECK(length(trim(notes)) > 0),
  position_seconds REAL NOT NULL CHECK(position_seconds >= 0),
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN('open','withdrawn')),
  request_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  withdrawn_at TEXT,
  UNIQUE(task_id, request_id)
);

CREATE INDEX idx_review_issues_candidate_status
  ON review_issues(task_id, candidate_id, status, created_at);
