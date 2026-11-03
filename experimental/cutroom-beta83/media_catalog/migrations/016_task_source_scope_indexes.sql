-- 016: allow active task source scopes from distinct library subdirectories.

DROP INDEX IF EXISTS idx_task_source_scopes_one_active_library;
CREATE UNIQUE INDEX idx_task_source_scopes_one_active_library
  ON task_source_scopes(task_id, library_id, relative_path)
  WHERE active = 1;
