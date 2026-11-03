-- 004: P2-M4 包装方案（packaging_plan）落库
-- 在 scene_cards / picture_lock 之上增加"包装确认门禁"：每条任务推 2–3 套包装方案，
-- 每套方案逐槽位（音色/BGM/字幕模板/文字卡/贴纸/花字/品牌）锁定 asset_ref + 备用项，
-- 无论命中默认模板都必须人工确认后才能进入 qa / 渲染。

CREATE TABLE IF NOT EXISTS packaging_plans(
  plan_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  route TEXT NOT NULL,
  plan_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'confirmed')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  confirmed_at TEXT,
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_packaging_plans_task ON packaging_plans(task_id);

CREATE TABLE IF NOT EXISTS packaging_plan_items(
  item_id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  slot TEXT NOT NULL,
  asset_ref TEXT,
  asset_file TEXT,
  asset_version TEXT,
  is_backup INTEGER NOT NULL DEFAULT 0,
  rank INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  FOREIGN KEY(plan_id) REFERENCES packaging_plans(plan_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_packaging_plan_items_plan ON packaging_plan_items(plan_id);

INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, datetime('now'));
