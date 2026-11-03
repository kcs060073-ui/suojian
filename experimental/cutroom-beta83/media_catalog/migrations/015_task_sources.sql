-- 015: explicit library purpose and durable task material scope history.

ALTER TABLE libraries ADD COLUMN purpose TEXT NOT NULL DEFAULT 'task_specific'
  CHECK(purpose IN ('task_specific', 'generic', 'packaging'));
ALTER TABLE libraries ADD COLUMN is_default_generic INTEGER NOT NULL DEFAULT 0
  CHECK(
    is_default_generic IN (0, 1)
    AND (is_default_generic = 0 OR purpose = 'generic')
  );

ALTER TABLE task_source_scopes ADD COLUMN source_mode TEXT
  CHECK(
    source_mode IS NULL
    OR source_mode IN ('primary', 'selected_generic', 'default_generic')
  );
ALTER TABLE task_source_scopes ADD COLUMN active INTEGER NOT NULL DEFAULT 1
  CHECK(active IN (0, 1));
ALTER TABLE task_source_scopes ADD COLUMN priority INTEGER NOT NULL DEFAULT 0
  CHECK(priority >= 0);
ALTER TABLE task_source_scopes ADD COLUMN retired_at TEXT;

UPDATE task_source_scopes
SET source_mode = CASE role
  WHEN 'primary' THEN 'primary'
  ELSE 'selected_generic'
END
WHERE source_mode IS NULL;

DROP INDEX IF EXISTS idx_task_source_scopes_one_primary;
CREATE UNIQUE INDEX idx_task_source_scopes_one_active_primary
  ON task_source_scopes(task_id)
  WHERE active = 1 AND source_mode = 'primary';
CREATE UNIQUE INDEX idx_task_source_scopes_one_active_library
  ON task_source_scopes(task_id, library_id, relative_path)
  WHERE active = 1;
CREATE INDEX idx_task_source_scopes_active_priority
  ON task_source_scopes(task_id, active, priority, scope_id);

CREATE TRIGGER task_source_scope_fill_mode
AFTER INSERT ON task_source_scopes
WHEN NEW.source_mode IS NULL
BEGIN
  UPDATE task_source_scopes
  SET source_mode = CASE NEW.role
    WHEN 'primary' THEN 'primary'
    ELSE 'selected_generic'
  END
  WHERE scope_id = NEW.scope_id;
END;

CREATE TRIGGER task_source_scope_validate_insert
BEFORE INSERT ON task_source_scopes
WHEN NEW.source_mode IS NOT NULL
  AND (
    (NEW.role = 'primary' AND NEW.source_mode != 'primary')
    OR
    (NEW.role = 'supplementary' AND NEW.source_mode = 'primary')
  )
BEGIN
  SELECT RAISE(ABORT, 'task source role and mode mismatch');
END;

CREATE TRIGGER task_source_scope_validate_update
BEFORE UPDATE OF role, source_mode ON task_source_scopes
WHEN NEW.source_mode IS NULL
  OR (
    (NEW.role = 'primary' AND NEW.source_mode != 'primary')
    OR
    (NEW.role = 'supplementary' AND NEW.source_mode = 'primary')
  )
BEGIN
  SELECT RAISE(ABORT, 'task source role and mode mismatch');
END;

CREATE TABLE task_source_settings(
  task_id TEXT PRIMARY KEY,
  include_default_generic INTEGER NOT NULL DEFAULT 0
    CHECK(include_default_generic IN (0, 1)),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
INSERT INTO task_source_settings(
  task_id,
  include_default_generic,
  revision,
  created_at,
  updated_at
)
SELECT
  task_id,
  0,
  1,
  MIN(created_at),
  MAX(updated_at)
FROM task_source_scopes
GROUP BY task_id;

CREATE TRIGGER task_source_scope_validate_library_insert
BEFORE INSERT ON task_source_scopes
WHEN (
  COALESCE(
    NEW.source_mode,
    CASE NEW.role WHEN 'primary' THEN 'primary' ELSE 'selected_generic' END
  ) = 'primary'
  AND (
    SELECT purpose FROM libraries WHERE library_id = NEW.library_id
  ) != 'task_specific'
)
OR (
  COALESCE(
    NEW.source_mode,
    CASE NEW.role WHEN 'primary' THEN 'primary' ELSE 'selected_generic' END
  ) IN ('selected_generic', 'default_generic')
  AND (
    SELECT purpose FROM libraries WHERE library_id = NEW.library_id
  ) != 'generic'
)
BEGIN
  SELECT RAISE(ABORT, 'task source library purpose mismatch');
END;

CREATE TRIGGER task_source_scope_validate_library_update
BEFORE UPDATE OF library_id, role, source_mode ON task_source_scopes
WHEN (
  COALESCE(
    NEW.source_mode,
    CASE NEW.role WHEN 'primary' THEN 'primary' ELSE 'selected_generic' END
  ) = 'primary'
  AND (
    SELECT purpose FROM libraries WHERE library_id = NEW.library_id
  ) != 'task_specific'
)
OR (
  COALESCE(
    NEW.source_mode,
    CASE NEW.role WHEN 'primary' THEN 'primary' ELSE 'selected_generic' END
  ) IN ('selected_generic', 'default_generic')
  AND (
    SELECT purpose FROM libraries WHERE library_id = NEW.library_id
  ) != 'generic'
)
BEGIN
  SELECT RAISE(ABORT, 'task source library purpose mismatch');
END;

ALTER TABLE task_source_snapshots RENAME TO task_source_snapshots_011;
CREATE TABLE task_source_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  asset_count INTEGER NOT NULL DEFAULT 0 CHECK(asset_count >= 0),
  total_size_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_size_bytes >= 0),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(scope_id, task_id)
    REFERENCES task_source_scopes(scope_id, task_id) ON DELETE RESTRICT
);
INSERT INTO task_source_snapshots(
  snapshot_id,
  task_id,
  scope_id,
  summary_json,
  asset_count,
  total_size_bytes,
  created_at
)
SELECT
  snapshot_id,
  task_id,
  scope_id,
  summary_json,
  asset_count,
  total_size_bytes,
  created_at
FROM task_source_snapshots_011;
DROP TABLE task_source_snapshots_011;
CREATE INDEX idx_task_source_snapshots_task
  ON task_source_snapshots(task_id, created_at);
CREATE INDEX idx_task_source_snapshots_scope
  ON task_source_snapshots(scope_id, created_at);
