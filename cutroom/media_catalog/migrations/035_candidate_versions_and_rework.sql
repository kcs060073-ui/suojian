-- 035: durable candidate lineage, categorized rework, and publish binding.

CREATE TABLE candidate_versions(
  candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL CHECK(version_no > 0),
  source_candidate_id TEXT REFERENCES candidate_versions(candidate_id),
  rework_request_id TEXT,
  mix_snapshot_id TEXT NOT NULL UNIQUE REFERENCES render_snapshots(snapshot_id),
  qa_snapshot_id TEXT UNIQUE REFERENCES render_snapshots(snapshot_id),
  qa_report_id TEXT REFERENCES qa_reports(report_id),
  input_digest TEXT NOT NULL,
  output_path TEXT NOT NULL,
  output_signature_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN(
    'rendered','qa_blocked','awaiting_review','rejected','approved','published'
  )),
  change_summary TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(task_id, version_no)
);

CREATE INDEX idx_candidate_versions_task_version
  ON candidate_versions(task_id, version_no DESC);
CREATE INDEX idx_candidate_versions_task_status
  ON candidate_versions(task_id, status, created_at);

CREATE TABLE rework_requests(
  rework_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  source_candidate_id TEXT NOT NULL REFERENCES candidate_versions(candidate_id),
  result_candidate_id TEXT REFERENCES candidate_versions(candidate_id),
  category TEXT NOT NULL CHECK(category IN(
    'script','material','rhythm','subtitle','audio','packaging','technical'
  )),
  notes TEXT NOT NULL,
  position_seconds REAL CHECK(position_seconds IS NULL OR position_seconds >= 0),
  qa_issue_ref TEXT,
  target_stage TEXT NOT NULL,
  impact_json TEXT NOT NULL,
  cost_status TEXT NOT NULL DEFAULT 'none'
    CHECK(cost_status IN('none','confirmation_required','approved')),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN(
      'draft','pending_confirmation','executing','applied','failed','cancelled'
    )),
  idempotency_key TEXT NOT NULL,
  execution_key TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(task_id, idempotency_key)
);

CREATE INDEX idx_rework_requests_source_status
  ON rework_requests(task_id, source_candidate_id, status);

ALTER TABLE publish_records ADD COLUMN candidate_id TEXT
  REFERENCES candidate_versions(candidate_id);
ALTER TABLE publish_records ADD COLUMN package_status TEXT NOT NULL DEFAULT 'current'
  CHECK(package_status IN('current','expired'));

