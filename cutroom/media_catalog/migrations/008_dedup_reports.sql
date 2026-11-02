-- P3-M2 重复检测报告
CREATE TABLE IF NOT EXISTS dedup_reports (
  report_id   TEXT PRIMARY KEY,
  task_id     TEXT,
  scope       TEXT NOT NULL,                -- 检测范围说明（如 "task:xinjiang 的候选镜头"）
  video_count INTEGER NOT NULL,
  frame_count INTEGER NOT NULL,
  group_count INTEGER NOT NULL,
  report_json TEXT NOT NULL,                -- 重复分组明细
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
