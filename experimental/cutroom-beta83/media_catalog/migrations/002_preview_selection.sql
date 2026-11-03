-- 002: P1 预览选材与隐私标记数据底座
-- 在 P0（技术探测）之上增加：场景片段、候选决策、任务目录、包装资产、
-- 隐私标记、预览缓存索引、非破坏性调色参数。

CREATE TABLE IF NOT EXISTS segments(
  segment_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  start_sec REAL NOT NULL,
  end_sec REAL NOT NULL,
  label TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_segments_asset ON segments(asset_id);

CREATE TABLE IF NOT EXISTS selection_decisions(
  asset_id TEXT PRIMARY KEY,
  decision TEXT NOT NULL CHECK(decision IN ('candidate', 'rejected', 'confirmed')),
  reason TEXT,
  decided_by TEXT,
  decided_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_selection_decision ON selection_decisions(decision);

CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  task_type TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  brief_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_assets(
  task_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  role TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(task_id, asset_id),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS packaging_assets(
  asset_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT,
  source_path TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS privacy_flags(
  flag_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  flag_type TEXT NOT NULL,
  start_sec REAL,
  end_sec REAL,
  confidence REAL,
  needs_human_confirm INTEGER NOT NULL DEFAULT 1,
  confirmed INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_privacy_asset ON privacy_flags(asset_id);

CREATE TABLE IF NOT EXISTS preview_cache(
  asset_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  size_bytes INTEGER,
  generated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(asset_id, kind),
  FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_preview_cache_kind ON preview_cache(kind);

CREATE TABLE IF NOT EXISTS color_params(
  asset_id TEXT PRIMARY KEY,
  params_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, datetime('now'));
