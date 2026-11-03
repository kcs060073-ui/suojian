-- Local measurements and direct duplicate pairs; human segment marks remain separate.
CREATE TABLE quality_evidence_runs (
  evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  asset_fingerprint TEXT NOT NULL,
  method_version TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(asset_id,asset_fingerprint,method_version)
);
CREATE TABLE duplicate_evidence (
  evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
  pair_key TEXT NOT NULL UNIQUE,
  left_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  right_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  left_fingerprint TEXT NOT NULL,
  right_fingerprint TEXT NOT NULL,
  left_start_us INTEGER NOT NULL,
  left_end_us INTEGER NOT NULL,
  right_start_us INTEGER NOT NULL,
  right_end_us INTEGER NOT NULL,
  result_json TEXT NOT NULL,
  decision TEXT NOT NULL DEFAULT 'unknown' CHECK(decision IN ('unknown','same','different')),
  reason TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL CHECK(revision>0),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX duplicate_evidence_assets ON duplicate_evidence(left_asset_id,right_asset_id);
ALTER TABLE scene_detection_items ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE scene_detection_items ADD COLUMN quality_error TEXT;
