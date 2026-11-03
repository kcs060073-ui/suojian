-- 019: require validated task-source APIs for configured scope mutations.

DROP TRIGGER IF EXISTS configured_task_source_protect_update;
CREATE TRIGGER configured_task_source_protect_update
BEFORE UPDATE OF task_id, library_id, role, relative_path, source_mode, active
ON task_source_scopes
WHEN OLD.active = 1
  AND EXISTS (
    SELECT 1 FROM task_source_settings setting
    WHERE setting.task_id = OLD.task_id
  )
  AND (
    EXISTS (
      SELECT 1 FROM task_assets task_asset
      WHERE task_asset.task_id = OLD.task_id
    )
    OR EXISTS (
      SELECT 1 FROM scene_cards card
      WHERE card.task_id = OLD.task_id AND card.asset_id IS NOT NULL
    )
  )
  AND task_source_write_authorized(OLD.task_id) = 0
BEGIN
  SELECT RAISE(ABORT, 'configured task source requires validated API update');
END;

DROP TRIGGER IF EXISTS configured_task_source_protect_delete;
CREATE TRIGGER configured_task_source_protect_delete
BEFORE DELETE ON task_source_scopes
WHEN OLD.active = 1
  AND EXISTS (
    SELECT 1 FROM task_source_settings setting
    WHERE setting.task_id = OLD.task_id
  )
  AND (
    EXISTS (
      SELECT 1 FROM task_assets task_asset
      WHERE task_asset.task_id = OLD.task_id
    )
    OR EXISTS (
      SELECT 1 FROM scene_cards card
      WHERE card.task_id = OLD.task_id AND card.asset_id IS NOT NULL
    )
  )
  AND task_source_write_authorized(OLD.task_id) = 0
BEGIN
  SELECT RAISE(ABORT, 'configured task source requires validated API delete');
END;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(19, datetime('now'));
