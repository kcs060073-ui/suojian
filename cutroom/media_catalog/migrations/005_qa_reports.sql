-- 005: P2-M5 自动质检报告（qa_reports）
-- 成片/样片渲染后跑一轮自动质检，结果落库可追溯；验收门禁要求 "qa 0 阻断"。

CREATE TABLE IF NOT EXISTS qa_reports(
  report_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  video_path TEXT,
  status TEXT NOT NULL CHECK(status IN ('passed', 'blocked')),
  issue_count INTEGER NOT NULL DEFAULT 0,
  blocking_count INTEGER NOT NULL DEFAULT 0,
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_qa_reports_task ON qa_reports(task_id);

INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, datetime('now'));
