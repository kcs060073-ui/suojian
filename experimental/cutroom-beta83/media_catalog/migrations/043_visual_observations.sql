-- Append-only, source-window visual evidence. Rescanning segments does not erase reviews.
CREATE TABLE visual_observations (
  observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  source_segment_id TEXT,
  source_start_us INTEGER NOT NULL,
  source_end_us INTEGER NOT NULL,
  asset_fingerprint TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('model','human')),
  method TEXT NOT NULL,
  method_version TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  title TEXT,
  description TEXT,
  camera_motion TEXT NOT NULL,
  motion_kind TEXT,
  subject_motion TEXT,
  entry_motion TEXT,
  exit_motion TEXT,
  uncertainty_reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK(source_start_us >= 0 AND source_end_us >= source_start_us)
);
CREATE INDEX idx_visual_observations_window
  ON visual_observations(asset_id, source_start_us, source_end_us, observation_id);
