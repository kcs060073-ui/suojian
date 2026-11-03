-- 018: reject unsafe task-source paths and dirty first-time configurations.

DROP TRIGGER IF EXISTS task_source_relative_path_validate_insert;
CREATE TRIGGER task_source_relative_path_validate_insert
BEFORE INSERT ON task_source_scopes
WHEN NEW.relative_path IS NULL
  OR instr(NEW.relative_path, char(0)) > 0
  OR (
    replace(NEW.relative_path, char(92), '/') != ''
    AND (
      substr(replace(NEW.relative_path, char(92), '/'), 1, 1) = '/'
      OR substr(replace(NEW.relative_path, char(92), '/'), 2, 1) = ':'
      OR substr(replace(NEW.relative_path, char(92), '/'), -1, 1) = '/'
      OR instr(replace(NEW.relative_path, char(92), '/'), '//') > 0
      OR replace(NEW.relative_path, char(92), '/') = '.'
      OR replace(NEW.relative_path, char(92), '/') = '..'
      OR replace(NEW.relative_path, char(92), '/') LIKE './%'
      OR replace(NEW.relative_path, char(92), '/') LIKE '../%'
      OR replace(NEW.relative_path, char(92), '/') LIKE '%/./%'
      OR replace(NEW.relative_path, char(92), '/') LIKE '%/../%'
      OR replace(NEW.relative_path, char(92), '/') LIKE '%/.'
      OR replace(NEW.relative_path, char(92), '/') LIKE '%/..'
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'task source relative_path is unsafe');
END;

DROP TRIGGER IF EXISTS task_source_relative_path_validate_update;
CREATE TRIGGER task_source_relative_path_validate_update
BEFORE UPDATE OF relative_path ON task_source_scopes
WHEN NEW.relative_path IS NULL
  OR instr(NEW.relative_path, char(0)) > 0
  OR (
    replace(NEW.relative_path, char(92), '/') != ''
    AND (
      substr(replace(NEW.relative_path, char(92), '/'), 1, 1) = '/'
      OR substr(replace(NEW.relative_path, char(92), '/'), 2, 1) = ':'
      OR substr(replace(NEW.relative_path, char(92), '/'), -1, 1) = '/'
      OR instr(replace(NEW.relative_path, char(92), '/'), '//') > 0
      OR replace(NEW.relative_path, char(92), '/') = '.'
      OR replace(NEW.relative_path, char(92), '/') = '..'
      OR replace(NEW.relative_path, char(92), '/') LIKE './%'
      OR replace(NEW.relative_path, char(92), '/') LIKE '../%'
      OR replace(NEW.relative_path, char(92), '/') LIKE '%/./%'
      OR replace(NEW.relative_path, char(92), '/') LIKE '%/../%'
      OR replace(NEW.relative_path, char(92), '/') LIKE '%/.'
      OR replace(NEW.relative_path, char(92), '/') LIKE '%/..'
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'task source relative_path is unsafe');
END;

-- Exercise the UPDATE guard so an unsafe pre-018 row aborts this migration.
UPDATE task_source_scopes SET relative_path=relative_path;

DROP TRIGGER IF EXISTS task_source_settings_validate_task_assets_insert;
CREATE TRIGGER task_source_settings_validate_task_assets_insert
BEFORE INSERT ON task_source_settings
WHEN EXISTS (
  SELECT 1
  FROM task_assets task_asset
  WHERE task_asset.task_id = NEW.task_id
    AND NOT EXISTS (
      SELECT 1
      FROM task_source_scopes scope
      JOIN asset_locations location
        ON location.library_id = scope.library_id
      WHERE scope.task_id = NEW.task_id
        AND scope.active = 1
        AND location.asset_id = task_asset.asset_id
        AND location.state = 'available'
        AND (
          replace(scope.relative_path, char(92), '/') = ''
          OR trim(replace(location.relative_path, char(92), '/'), '/') =
             replace(scope.relative_path, char(92), '/')
          OR substr(
               trim(replace(location.relative_path, char(92), '/'), '/'),
               1,
               length(replace(scope.relative_path, char(92), '/')) + 1
             ) = replace(scope.relative_path, char(92), '/') || '/'
        )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'existing task asset is outside proposed source scope');
END;

DROP TRIGGER IF EXISTS task_source_settings_validate_scene_cards_insert;
CREATE TRIGGER task_source_settings_validate_scene_cards_insert
BEFORE INSERT ON task_source_settings
WHEN EXISTS (
  SELECT 1
  FROM scene_cards card
  WHERE card.task_id = NEW.task_id
    AND card.asset_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM task_source_scopes scope
      JOIN asset_locations location
        ON location.library_id = scope.library_id
      WHERE scope.task_id = NEW.task_id
        AND scope.active = 1
        AND location.asset_id = card.asset_id
        AND location.state = 'available'
        AND (
          replace(scope.relative_path, char(92), '/') = ''
          OR trim(replace(location.relative_path, char(92), '/'), '/') =
             replace(scope.relative_path, char(92), '/')
          OR substr(
               trim(replace(location.relative_path, char(92), '/'), '/'),
               1,
               length(replace(scope.relative_path, char(92), '/')) + 1
             ) = replace(scope.relative_path, char(92), '/') || '/'
        )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'existing scene card is outside proposed source scope');
END;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(18, datetime('now'));
