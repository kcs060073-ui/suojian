-- 068: New local deliveries do not have a target platform. Preserve legacy values.
CREATE TABLE publish_records_next(
  record_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  platform TEXT,
  title TEXT,
  description TEXT,
  tags TEXT,
  cover_path TEXT,
  sensitive_hits TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'cleared', 'blocked')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  candidate_id TEXT REFERENCES candidate_versions(candidate_id),
  package_status TEXT NOT NULL DEFAULT 'current' CHECK(package_status IN ('current','expired')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
INSERT INTO publish_records_next(
  record_id,task_id,platform,title,description,tags,cover_path,
  sensitive_hits,status,created_at,candidate_id,package_status
)
SELECT record_id,task_id,platform,title,description,tags,cover_path,
  sensitive_hits,status,created_at,candidate_id,package_status
FROM publish_records;
DROP TABLE publish_records;
ALTER TABLE publish_records_next RENAME TO publish_records;
CREATE INDEX idx_publish_records_task ON publish_records(task_id);
