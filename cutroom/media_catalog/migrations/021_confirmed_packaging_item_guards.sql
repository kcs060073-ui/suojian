-- 021: seal item rows belonging to confirmed packaging plans.

CREATE TRIGGER IF NOT EXISTS packaging_plan_items_confirmed_insert_guard
BEFORE INSERT ON packaging_plan_items
WHEN EXISTS(
  SELECT 1
  FROM packaging_plans AS plan
  WHERE plan.plan_id = NEW.plan_id
    AND plan.status = 'confirmed'
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed packaging item is immutable');
END;

CREATE TRIGGER IF NOT EXISTS packaging_plan_items_confirmed_update_guard
BEFORE UPDATE ON packaging_plan_items
WHEN EXISTS(
  SELECT 1
  FROM packaging_plans AS plan
  WHERE plan.plan_id = OLD.plan_id
    AND plan.status = 'confirmed'
)
OR EXISTS(
  SELECT 1
  FROM packaging_plans AS plan
  WHERE plan.plan_id = NEW.plan_id
    AND plan.status = 'confirmed'
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed packaging item is immutable');
END;

CREATE TRIGGER IF NOT EXISTS packaging_plan_items_confirmed_delete_guard
BEFORE DELETE ON packaging_plan_items
WHEN EXISTS(
  SELECT 1
  FROM packaging_plans AS plan
  JOIN tasks AS task ON task.task_id = plan.task_id
  WHERE plan.plan_id = OLD.plan_id
    AND plan.status = 'confirmed'
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed packaging item is immutable');
END;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(21, datetime('now'));
