-- Frozen PK-04/06/07/14: preserve legacy slot plans and add explicit task-local elements.
-- Existing plans keep schema_version=1; new plans opt into the instance compiler.
ALTER TABLE packaging_plans ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE packaging_plans ADD COLUMN instance_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE packaging_plans ADD COLUMN source_template_id TEXT;
ALTER TABLE packaging_plans ADD COLUMN source_template_digest TEXT;

CREATE TABLE packaging_plan_instances(
  instance_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES packaging_plans(plan_id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN (
    'title_card','info_card','brand','sfx','sticker','fancy_text'
  )),
  asset_ref TEXT NOT NULL,
  content_text TEXT,
  anchor_kind TEXT NOT NULL DEFAULT 'timeline'
    CHECK(anchor_kind IN ('timeline','scene','instance')),
  anchor_ref TEXT,
  start_seconds REAL,
  end_seconds REAL,
  x REAL,
  y REAL,
  scale REAL,
  gain REAL,
  timing_rule TEXT NOT NULL DEFAULT 'explicit'
    CHECK(timing_rule IN ('explicit','legacy_v1')),
  origin TEXT NOT NULL DEFAULT 'manual'
    CHECK(origin IN ('manual','recommendation','legacy')),
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_packaging_instances_plan_role
ON packaging_plan_instances(plan_id,role,created_at,instance_id);

-- Retain identity and old timing behavior: historical confirmed digests are unchanged.
INSERT INTO packaging_plan_instances(
  instance_id,plan_id,role,asset_ref,timing_rule,origin
)
SELECT 'legacy-' || item_id,plan_id,slot,asset_ref,'legacy_v1','legacy'
FROM packaging_plan_items
WHERE is_backup=0 AND slot IN (
  'title_card','brand','sfx','sticker','fancy_text'
);

CREATE TRIGGER packaging_instances_confirmed_insert_guard
BEFORE INSERT ON packaging_plan_instances
WHEN EXISTS(SELECT 1 FROM packaging_plans WHERE plan_id=NEW.plan_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'confirmed packaging instance is immutable'); END;
CREATE TRIGGER packaging_instances_confirmed_update_guard
BEFORE UPDATE ON packaging_plan_instances
WHEN EXISTS(SELECT 1 FROM packaging_plans WHERE plan_id=OLD.plan_id AND status='confirmed')
  OR EXISTS(SELECT 1 FROM packaging_plans WHERE plan_id=NEW.plan_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'confirmed packaging instance is immutable'); END;
CREATE TRIGGER packaging_instances_confirmed_delete_guard
BEFORE DELETE ON packaging_plan_instances
WHEN EXISTS(SELECT 1 FROM packaging_plans WHERE plan_id=OLD.plan_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'confirmed packaging instance is immutable'); END;
