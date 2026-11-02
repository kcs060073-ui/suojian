-- 006: P2-M6 发布记录 + P2-M7 渲染快照
-- publish_records：每条任务每次发布准备（封面/标题/简介/标签/合规）落库可追溯。
-- render_snapshots：每次渲染（mix/ad/qa）的参数与产物快照，支撑局部重跑与对照。

CREATE TABLE IF NOT EXISTS publish_records(
  record_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  title TEXT,
  description TEXT,
  tags TEXT,
  cover_path TEXT,
  sensitive_hits TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'cleared', 'blocked')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_publish_records_task ON publish_records(task_id);

CREATE TABLE IF NOT EXISTS render_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('mix', 'ad', 'qa')),
  params_json TEXT,
  output_path TEXT,
  parent_snapshot TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_render_snapshots_task ON render_snapshots(task_id);

INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, datetime('now'));
