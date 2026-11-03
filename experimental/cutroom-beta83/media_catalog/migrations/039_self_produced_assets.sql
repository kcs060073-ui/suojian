-- 039: reusable self-produced authorization tied to the indexed fingerprint.

CREATE TABLE self_produced_asset_authorizations(
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id) ON DELETE CASCADE,
  quick_fingerprint TEXT NOT NULL,
  confirmed_by TEXT NOT NULL,
  confirmed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
