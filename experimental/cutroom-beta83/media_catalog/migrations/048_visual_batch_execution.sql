CREATE TABLE visual_batch_items (
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  item_index INTEGER NOT NULL,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  asset_fingerprint TEXT NOT NULL,
  source_start_us INTEGER NOT NULL,
  source_end_us INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed','outcome_unknown')),
  observation_id INTEGER REFERENCES visual_observations(observation_id),
  input_digest TEXT,
  error_code TEXT,
  provider_dispatched INTEGER NOT NULL DEFAULT 0 CHECK(provider_dispatched IN (0,1)),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(job_id,item_index)
);
CREATE INDEX visual_batch_items_asset_idx ON visual_batch_items(asset_id,status);
