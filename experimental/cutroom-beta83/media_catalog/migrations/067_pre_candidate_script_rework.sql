-- Allow a confirmed script to be revised after rough cut, before a QA candidate exists.
-- Existing candidate-bound review rework rows retain their original source identity.

CREATE TABLE rework_requests_new(
  rework_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  source_candidate_id TEXT REFERENCES candidate_versions(candidate_id),
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

INSERT INTO rework_requests_new(
  rework_id,task_id,source_candidate_id,result_candidate_id,category,notes,
  position_seconds,qa_issue_ref,target_stage,impact_json,cost_status,status,
  idempotency_key,execution_key,created_at,updated_at
)
SELECT
  rework_id,task_id,source_candidate_id,result_candidate_id,category,notes,
  position_seconds,qa_issue_ref,target_stage,impact_json,cost_status,status,
  idempotency_key,execution_key,created_at,updated_at
FROM rework_requests;

DROP TABLE rework_requests;
ALTER TABLE rework_requests_new RENAME TO rework_requests;

CREATE INDEX idx_rework_requests_source_status
  ON rework_requests(task_id, source_candidate_id, status);
