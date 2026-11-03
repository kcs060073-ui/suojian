-- Advice lives beside the task; neither reports nor requirements approve/edit shots.
CREATE TABLE reshoot_requirements (
  gap_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  script_revision TEXT NOT NULL,
  script_segment_id TEXT NOT NULL REFERENCES script_segments(segment_id),
  origin_key TEXT NOT NULL,
  card_id TEXT,
  query TEXT NOT NULL,
  media_kind TEXT NOT NULL CHECK(media_kind IN ('video','image')),
  timing_basis TEXT NOT NULL DEFAULT 'measured_timeline',
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  start_us INTEGER NOT NULL,
  end_us INTEGER NOT NULL CHECK(end_us>start_us),
  UNIQUE(task_id,script_revision,origin_key)
);
CREATE TABLE reshoot_reports (
  report_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  task_revision TEXT NOT NULL,
  inputs_digest TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(task_id,task_revision,inputs_digest)
);
CREATE INDEX idx_reshoot_reports_task ON reshoot_reports(task_id,created_at);
CREATE TABLE task_reshoot_current (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  report_id TEXT NOT NULL REFERENCES reshoot_reports(report_id)
);
