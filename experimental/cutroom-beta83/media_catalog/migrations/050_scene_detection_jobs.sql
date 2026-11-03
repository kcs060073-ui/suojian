CREATE TABLE scene_detection_items (
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  item_index INTEGER NOT NULL,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  asset_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('succeeded','failed')),
  window_count INTEGER NOT NULL DEFAULT 0 CHECK(window_count >= 0),
  error_code TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(job_id,item_index)
);
