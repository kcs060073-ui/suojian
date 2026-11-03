-- 003: P2 场景卡片 + 画面锁定
-- 在 P1（任务目录）之上增加：每张场景卡片（文案—事实—素材映射）、画面锁定门禁。
-- 场景卡片是"哪段用哪段素材、怎么切"的可追查记录；画面锁定后系统知道
-- 修改素材/文案会重跑哪些字幕/声音/包装/质检步骤。

CREATE TABLE IF NOT EXISTS scene_cards(
  card_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  narrative_text TEXT,
  narrative_start REAL,
  narrative_end REAL,
  asset_id TEXT,
  segment_id TEXT,
  mix_form TEXT,
  enter_tc REAL,
  exit_tc REAL,
  audio_policy_draft TEXT,
  source_type TEXT,
  system_suggestion TEXT,
  final_decision TEXT,
  confirm_status INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_scene_cards_task ON scene_cards(task_id, idx);

CREATE TABLE IF NOT EXISTS picture_lock(
  task_id TEXT PRIMARY KEY,
  lock_version TEXT NOT NULL,
  locked_at TEXT NOT NULL DEFAULT (datetime('now')),
  changed_since_lock INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, datetime('now'));
