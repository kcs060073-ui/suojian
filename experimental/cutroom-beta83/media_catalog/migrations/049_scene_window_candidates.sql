-- Scene suggestions are versioned facts about one source fingerprint.
-- They never replace human marks or the legacy P1 segments table.
CREATE TABLE scene_candidate_sets (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id) ON DELETE CASCADE,
  asset_fingerprint TEXT NOT NULL,
  method_version TEXT NOT NULL,
  duration_us INTEGER NOT NULL CHECK(duration_us > 0),
  source_library_id TEXT NOT NULL,
  source_relative_path TEXT NOT NULL,
  detected_cut_count INTEGER NOT NULL CHECK(detected_cut_count >= 0),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE scene_candidate_windows (
  asset_id TEXT NOT NULL REFERENCES scene_candidate_sets(asset_id) ON DELETE CASCADE,
  item_index INTEGER NOT NULL,
  source_start_us INTEGER NOT NULL CHECK(source_start_us >= 0),
  source_end_us INTEGER NOT NULL CHECK(source_end_us > source_start_us),
  origin TEXT NOT NULL CHECK(origin IN ('detected_cut','bounded_fallback')),
  PRIMARY KEY(asset_id,item_index)
);
CREATE INDEX scene_candidate_windows_range_idx
  ON scene_candidate_windows(asset_id,source_start_us,source_end_us);
