-- Stable human decisions on usable source windows. Model observations remain append-only.
CREATE TABLE catalog_segment_marks (
  segment_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  base_start_us INTEGER NOT NULL,
  base_end_us INTEGER NOT NULL,
  asset_fingerprint TEXT NOT NULL,
  source_start_us INTEGER NOT NULL,
  source_end_us INTEGER NOT NULL,
  title TEXT,
  description TEXT,
  camera_motion TEXT NOT NULL,
  motion_kind TEXT,
  subject_motion TEXT,
  usage_status TEXT NOT NULL CHECK(usage_status IN ('可用','待核验','不建议使用')),
  note TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK(source_start_us >= 0 AND source_end_us >= source_start_us)
);
CREATE INDEX idx_catalog_segment_marks_asset ON catalog_segment_marks(asset_id,asset_fingerprint);
