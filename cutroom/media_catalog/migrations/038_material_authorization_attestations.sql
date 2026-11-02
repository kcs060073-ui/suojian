-- 038: explicit, immutable authorization evidence for the assets in a picture lock.

CREATE TABLE material_authorization_attestations(
  attestation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  picture_lock_digest TEXT NOT NULL,
  asset_ids_json TEXT NOT NULL,
  confirmed_by TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_material_authorization_task_created
  ON material_authorization_attestations(task_id,created_at DESC);
