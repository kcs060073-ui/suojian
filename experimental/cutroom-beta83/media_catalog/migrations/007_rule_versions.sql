-- P3-M1 规则版本化
-- 新任务默认最新规则；旧任务锁定创建时的规则版本（设计固定边界：新任务默认最新规则，原任务锁定创建时的规则和资产版本）

CREATE TABLE IF NOT EXISTS rule_versions (
  rule_set_id TEXT NOT NULL DEFAULT 'default',
  version     INTEGER NOT NULL,
  payload     TEXT NOT NULL,            -- JSON：qa 阈值等可迭代改进的规则
  notes       TEXT,
  applied_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (rule_set_id, version)
);

-- tasks 锁定创建时生效的规则版本
ALTER TABLE tasks ADD COLUMN rule_version INTEGER;
