-- 013: durable job requests, cost state, and provider idempotency keys.

CREATE TABLE IF NOT EXISTS job_payloads(
  job_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  cost_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK(cost_status IN ('unknown', 'estimated', 'approved', 'incurred', 'waived')),
  provider_request_id TEXT UNIQUE,
  estimated_cost REAL CHECK(estimated_cost IS NULL OR estimated_cost >= 0),
  actual_cost REAL CHECK(actual_cost IS NULL OR actual_cost >= 0),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(13, datetime('now'));
