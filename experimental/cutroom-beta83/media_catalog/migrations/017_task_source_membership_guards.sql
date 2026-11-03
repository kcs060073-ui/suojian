-- 017: keep task material membership and active source purposes valid in SQLite.

DROP TRIGGER IF EXISTS task_asset_membership_validate_insert;
CREATE TRIGGER task_asset_membership_validate_insert
BEFORE INSERT ON task_assets
WHEN EXISTS (
  SELECT 1 FROM task_source_settings setting
  WHERE setting.task_id = NEW.task_id
)
AND NOT EXISTS (
  SELECT 1
  FROM task_source_scopes scope
  JOIN asset_locations location
    ON location.library_id = scope.library_id
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
BEFORE UPDATE OF task_id, asset_id ON task_assets
WHEN EXISTS (
  SELECT 1 FROM task_source_settings setting
  WHERE setting.task_id = NEW.task_id
)
AND NOT EXISTS (
  SELECT 1
  FROM task_source_scopes scope
  JOIN asset_locations location
    ON location.library_id = scope.library_id
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
  FROM task_source_scopes scope
  JOIN asset_locations location
    ON location.library_id = scope.library_id
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
  FROM task_source_scopes scope
  JOIN asset_locations location
    ON location.library_id = scope.library_id
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

DROP TRIGGER IF EXISTS library_default_generic_validate_update;
CREATE TRIGGER library_default_generic_validate_update
BEFORE UPDATE OF purpose, is_default_generic ON libraries
WHEN NEW.purpose IS NULL
  OR NEW.purpose NOT IN ('task_specific', 'generic', 'packaging')
  OR NEW.is_default_generic IS NULL
  OR NEW.is_default_generic NOT IN (0, 1)
  OR (NEW.is_default_generic = 1 AND NEW.purpose != 'generic')
BEGIN
  SELECT RAISE(ABORT, 'default generic library requires generic purpose');
END;

DROP TRIGGER IF EXISTS library_active_scope_purpose_validate_update;
CREATE TRIGGER library_active_scope_purpose_validate_update
BEFORE UPDATE OF purpose, is_default_generic ON libraries
WHEN EXISTS (
  SELECT 1
  FROM task_source_scopes scope
  WHERE scope.library_id = NEW.library_id
    AND scope.active = 1
    AND (
      (
        COALESCE(
          scope.source_mode,
          CASE scope.role WHEN 'primary' THEN 'primary' ELSE 'selected_generic' END
        ) = 'primary'
        AND NEW.purpose != 'task_specific'
      )
      OR (
        COALESCE(
          scope.source_mode,
          CASE scope.role WHEN 'primary' THEN 'primary' ELSE 'selected_generic' END
        ) IN ('selected_generic', 'default_generic')
        AND NEW.purpose != 'generic'
      )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'library purpose conflicts with active task source scope');
END;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(17, datetime('now'));
