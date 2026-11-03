CREATE TABLE task_create_requests (
  request_id TEXT PRIMARY KEY,
  input_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
  response_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);
