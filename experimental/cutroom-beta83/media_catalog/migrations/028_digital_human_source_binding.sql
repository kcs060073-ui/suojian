-- 028: a digital-human task may bind its one locked presenter source even
-- when that source lives outside the task's B-roll material scopes.

DROP TRIGGER IF EXISTS task_asset_membership_validate_insert;
CREATE TRIGGER task_asset_membership_validate_insert
BEFORE INSERT ON task_assets
WHEN EXISTS (
  SELECT 1 FROM task_source_settings setting
  WHERE setting.task_id = NEW.task_id
)
AND NOT (
  NEW.role = 'digital_human'
  AND EXISTS (
    SELECT 1 FROM tasks task
    WHERE task.task_id = NEW.task_id
      AND task.task_type = 'digital_human'
      AND CASE
        WHEN json_valid(COALESCE(task.brief_json, '')) = 1
        THEN json_extract(task.brief_json, '$.digital_human_source.asset_id')
      END = NEW.asset_id
  )
)
AND NOT EXISTS (
  SELECT 1
  FROM task_source_scopes scope
  JOIN asset_locations location ON location.library_id = scope.library_id
  WHERE scope.task_id = NEW.task_id
    AND scope.active = 1
    AND location.asset_id = NEW.asset_id
    AND location.state = 'available'
    AND (
      trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') = ''
      OR trim(replace(COALESCE(location.relative_path, ''), char(92), '/'), '/') =
         trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/')
      OR substr(
           trim(replace(COALESCE(location.relative_path, ''), char(92), '/'), '/'),
           1,
           length(trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/')) + 1
         ) = trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') || '/'
    )
)
BEGIN
  SELECT RAISE(ABORT, 'task asset is outside active source scope');
END;

DROP TRIGGER IF EXISTS task_asset_membership_validate_update;
CREATE TRIGGER task_asset_membership_validate_update
BEFORE UPDATE OF task_id, asset_id, role ON task_assets
WHEN EXISTS (
  SELECT 1 FROM task_source_settings setting
  WHERE setting.task_id = NEW.task_id
)
AND NOT (
  NEW.role = 'digital_human'
  AND EXISTS (
    SELECT 1 FROM tasks task
    WHERE task.task_id = NEW.task_id
      AND task.task_type = 'digital_human'
      AND CASE
        WHEN json_valid(COALESCE(task.brief_json, '')) = 1
        THEN json_extract(task.brief_json, '$.digital_human_source.asset_id')
      END = NEW.asset_id
  )
)
AND NOT EXISTS (
  SELECT 1
  FROM task_source_scopes scope
  JOIN asset_locations location ON location.library_id = scope.library_id
  WHERE scope.task_id = NEW.task_id
    AND scope.active = 1
    AND location.asset_id = NEW.asset_id
    AND location.state = 'available'
    AND (
      trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') = ''
      OR trim(replace(COALESCE(location.relative_path, ''), char(92), '/'), '/') =
         trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/')
      OR substr(
           trim(replace(COALESCE(location.relative_path, ''), char(92), '/'), '/'),
           1,
           length(trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/')) + 1
         ) = trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') || '/'
    )
)
BEGIN
  SELECT RAISE(ABORT, 'task asset is outside active source scope');
END;

DROP TRIGGER IF EXISTS scene_card_membership_validate_insert;
CREATE TRIGGER scene_card_membership_validate_insert
BEFORE INSERT ON scene_cards
WHEN NEW.asset_id IS NOT NULL
AND EXISTS (
  SELECT 1 FROM task_source_settings setting
  WHERE setting.task_id = NEW.task_id
)
AND NOT EXISTS (
  SELECT 1
  FROM tasks task
  JOIN task_assets task_asset
    ON task_asset.task_id = task.task_id
   AND task_asset.asset_id = NEW.asset_id
   AND task_asset.role = 'digital_human'
  WHERE task.task_id = NEW.task_id
    AND task.task_type = 'digital_human'
    AND CASE
      WHEN json_valid(COALESCE(task.brief_json, '')) = 1
      THEN json_extract(task.brief_json, '$.digital_human_source.asset_id')
    END = NEW.asset_id
)
AND NOT EXISTS (
  SELECT 1
  FROM task_source_scopes scope
  JOIN asset_locations location ON location.library_id = scope.library_id
  WHERE scope.task_id = NEW.task_id
    AND scope.active = 1
    AND location.asset_id = NEW.asset_id
    AND location.state = 'available'
    AND (
      trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') = ''
      OR trim(replace(COALESCE(location.relative_path, ''), char(92), '/'), '/') =
         trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/')
      OR substr(
           trim(replace(COALESCE(location.relative_path, ''), char(92), '/'), '/'),
           1,
           length(trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/')) + 1
         ) = trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') || '/'
    )
)
BEGIN
  SELECT RAISE(ABORT, 'scene card asset is outside active source scope');
END;

DROP TRIGGER IF EXISTS scene_card_membership_validate_update;
CREATE TRIGGER scene_card_membership_validate_update
BEFORE UPDATE OF task_id, asset_id ON scene_cards
WHEN NEW.asset_id IS NOT NULL
AND EXISTS (
  SELECT 1 FROM task_source_settings setting
  WHERE setting.task_id = NEW.task_id
)
AND NOT EXISTS (
  SELECT 1
  FROM tasks task
  JOIN task_assets task_asset
    ON task_asset.task_id = task.task_id
   AND task_asset.asset_id = NEW.asset_id
   AND task_asset.role = 'digital_human'
  WHERE task.task_id = NEW.task_id
    AND task.task_type = 'digital_human'
    AND CASE
      WHEN json_valid(COALESCE(task.brief_json, '')) = 1
      THEN json_extract(task.brief_json, '$.digital_human_source.asset_id')
    END = NEW.asset_id
)
AND NOT EXISTS (
  SELECT 1
  FROM task_source_scopes scope
  JOIN asset_locations location ON location.library_id = scope.library_id
  WHERE scope.task_id = NEW.task_id
    AND scope.active = 1
    AND location.asset_id = NEW.asset_id
    AND location.state = 'available'
    AND (
      trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') = ''
      OR trim(replace(COALESCE(location.relative_path, ''), char(92), '/'), '/') =
         trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/')
      OR substr(
           trim(replace(COALESCE(location.relative_path, ''), char(92), '/'), '/'),
           1,
           length(trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/')) + 1
         ) = trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') || '/'
    )
)
BEGIN
  SELECT RAISE(ABORT, 'scene card asset is outside active source scope');
END;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(28, datetime('now'));
