CREATE TABLE IF NOT EXISTS libraries (
  library_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('local', 'nas')),
  mount_path TEXT NOT NULL,
  online INTEGER NOT NULL DEFAULT 1 CHECK(online IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  media_kind TEXT NOT NULL CHECK(media_kind IN ('video', 'image', 'audio', 'other')),
  size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
  quick_fingerprint TEXT NOT NULL,
  full_sha256 TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_quick_fingerprint
  ON assets(size_bytes, quick_fingerprint);

CREATE TABLE IF NOT EXISTS asset_locations (
  location_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  library_id TEXT NOT NULL REFERENCES libraries(library_id),
  relative_path TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL,
  file_identity TEXT,
  state TEXT NOT NULL DEFAULT 'available'
    CHECK(state IN ('available', 'missing', 'offline')),
  UNIQUE(library_id, relative_path)
);

CREATE INDEX IF NOT EXISTS idx_asset_locations_identity
  ON asset_locations(library_id, file_identity);

CREATE TABLE IF NOT EXISTS analysis_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  stage TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK(status IN ('pending', 'running', 'completed', 'failed', 'interrupted', 'stale')),
  tool_version TEXT,
  result_json TEXT,
  error_summary TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(asset_id, stage)
);

CREATE TABLE IF NOT EXISTS duplicate_candidates (
  left_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  right_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'suspected',
  checked_sha256_at TEXT,
  PRIMARY KEY(left_asset_id, right_asset_id),
  CHECK(left_asset_id < right_asset_id)
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,
  library_id TEXT REFERENCES libraries(library_id),
  status TEXT NOT NULL
    CHECK(status IN ('pending', 'running', 'completed', 'failed', 'interrupted', 'stale')),
  cursor TEXT,
  heartbeat_at TEXT,
  error_summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
