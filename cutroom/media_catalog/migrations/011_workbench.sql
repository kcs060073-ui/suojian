-- 011: durable colleague workbench state.
-- Adds task source boundaries and snapshots, the twelve-stage state machine,
-- review progress, model capability settings, and observable/cancellable jobs.

CREATE TABLE IF NOT EXISTS task_source_scopes(
  scope_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  library_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('primary', 'supplementary')),
  relative_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(scope_id, task_id),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(library_id) REFERENCES libraries(library_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_source_scopes_one_primary
  ON task_source_scopes(task_id)
  WHERE role = 'primary';
CREATE INDEX IF NOT EXISTS idx_task_source_scopes_task
  ON task_source_scopes(task_id, role);

CREATE TABLE IF NOT EXISTS task_source_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  asset_count INTEGER NOT NULL DEFAULT 0 CHECK(asset_count >= 0),
  total_size_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_size_bytes >= 0),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(scope_id, task_id)
    REFERENCES task_source_scopes(scope_id, task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_source_snapshots_task
  ON task_source_snapshots(task_id, created_at);

CREATE TABLE IF NOT EXISTS task_stage_states(
  task_id TEXT NOT NULL,
  stage_key TEXT NOT NULL CHECK(stage_key IN (
    'create_preflight',
    'brief_lock',
    'material_sync',
    'ai_storyboard',
    'rough_cut',
    'picture_order',
    'picture_lock',
    'packaging_confirm',
    'fine_render',
    'auto_qa',
    'full_review',
    'publish_package'
  )),
  status TEXT NOT NULL DEFAULT 'not_started' CHECK(status IN (
    'not_started', 'ready', 'in_progress', 'completed', 'invalidated', 'blocked'
  )),
  detail_json TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(task_id, stage_key),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_stage_states_status
  ON task_stage_states(status, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_render_snapshots_snapshot_task
  ON render_snapshots(snapshot_id, task_id);

CREATE TABLE IF NOT EXISTS review_watch_progress(
  task_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  watched_seconds REAL NOT NULL DEFAULT 0 CHECK(watched_seconds >= 0),
  duration_seconds REAL NOT NULL DEFAULT 0 CHECK(duration_seconds >= 0),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(task_id, snapshot_id),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(snapshot_id, task_id)
    REFERENCES render_snapshots(snapshot_id, task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_provider_settings(
  capability TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE jobs ADD COLUMN task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE;
ALTER TABLE jobs ADD COLUMN progress_current INTEGER NOT NULL DEFAULT 0
  CHECK(progress_current >= 0);
ALTER TABLE jobs ADD COLUMN progress_total INTEGER NOT NULL DEFAULT 0
  CHECK(progress_total >= 0);
ALTER TABLE jobs ADD COLUMN phase_label TEXT;
ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0
  CHECK(cancel_requested IN (0, 1));
ALTER TABLE jobs ADD COLUMN cancel_reason TEXT;
ALTER TABLE jobs ADD COLUMN started_at TEXT;
ALTER TABLE jobs ADD COLUMN finished_at TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_task_created
  ON jobs(task_id, created_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(11, datetime('now'));
