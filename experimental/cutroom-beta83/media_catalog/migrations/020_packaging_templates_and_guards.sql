-- 020: task-independent immutable packaging templates and confirmed-plan guard.

CREATE TABLE IF NOT EXISTS packaging_templates(
  template_id TEXT PRIMARY KEY,
  template_name TEXT NOT NULL,
  route TEXT NOT NULL CHECK(route IN ('数字人混剪', '园区宣传', '信息流广告')),
  snapshot_json TEXT NOT NULL,
  snapshot_digest TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER IF NOT EXISTS packaging_templates_immutable_update
BEFORE UPDATE ON packaging_templates
BEGIN
  SELECT RAISE(ABORT, 'packaging template snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS packaging_plans_confirmed_immutable_update
BEFORE UPDATE ON packaging_plans
WHEN OLD.status = 'confirmed'
BEGIN
  SELECT RAISE(ABORT, 'confirmed packaging plan is immutable');
END;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(20, datetime('now'));
