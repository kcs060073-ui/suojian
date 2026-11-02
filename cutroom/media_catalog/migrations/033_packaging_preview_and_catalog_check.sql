CREATE TABLE packaging_preview_artifacts(
  preview_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  plan_id TEXT NOT NULL REFERENCES packaging_plans(plan_id) ON DELETE CASCADE,
  canonical_key TEXT NOT NULL UNIQUE,
  picture_lock_digest TEXT NOT NULL,
  plan_digest TEXT NOT NULL,
  renderer_version TEXT NOT NULL,
  preview_profile TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK(status IN('queued','rendering','ready','failed','stale','missing')),
  rel_path TEXT,
  output_signature_json TEXT,
  duration_seconds REAL,
  size_bytes INTEGER,
  coverage_json TEXT NOT NULL DEFAULT '[]',
  error_code TEXT,
  error_detail TEXT,
  viewed_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_packaging_preview_task_plan
  ON packaging_preview_artifacts(task_id, plan_id, created_at);

CREATE TABLE packaging_catalog_checks(
  check_id TEXT PRIMARY KEY,
  scope_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('queued','running','completed','failed')),
  result_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);

CREATE INDEX idx_packaging_catalog_checks_scope
  ON packaging_catalog_checks(scope_digest, created_at);
