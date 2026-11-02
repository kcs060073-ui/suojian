-- 031: optional Agent orchestration mode and immutable planning artifacts.

ALTER TABLE tasks ADD COLUMN orchestration_mode TEXT NOT NULL DEFAULT 'standalone'
  CHECK(orchestration_mode IN ('standalone', 'agent'));

CREATE TABLE agent_artifacts(
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  artifact_type TEXT NOT NULL
    CHECK(artifact_type IN ('digital_storyboard', 'ad_storyboard')),
  schema_version INTEGER NOT NULL CHECK(schema_version > 0),
  content_json TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  source_agent TEXT NOT NULL,
  source_model_label TEXT,
  status TEXT NOT NULL DEFAULT 'accepted'
    CHECK(status IN ('accepted', 'superseded')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  confirmed_at TEXT,
  UNIQUE(task_id, artifact_type, input_digest, content_digest)
);

CREATE INDEX idx_agent_artifacts_task_type_created
  ON agent_artifacts(task_id, artifact_type, created_at, artifact_id);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(31, datetime('now'));
