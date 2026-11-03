CREATE TABLE catalog_identity (
  id INTEGER PRIMARY KEY CHECK(id=1),
  catalog_id TEXT NOT NULL
);
INSERT INTO catalog_identity(id,catalog_id) VALUES(1,lower(hex(randomblob(16))));

CREATE TABLE catalog_asset_annotations (
  asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id) ON DELETE CASCADE,
  asset_fingerprint TEXT NOT NULL,
  fields_json TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE catalog_segment_marks ADD COLUMN tags TEXT NOT NULL DEFAULT '';
ALTER TABLE catalog_segment_marks ADD COLUMN quality_issue TEXT NOT NULL DEFAULT '';
ALTER TABLE catalog_segment_marks ADD COLUMN usable_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE catalog_segment_marks ADD COLUMN duplicate_note TEXT NOT NULL DEFAULT '';
ALTER TABLE catalog_segment_marks ADD COLUMN edited_fields_json TEXT;

CREATE TABLE catalog_exchange_exports (
  export_id TEXT PRIMARY KEY,
  catalog_id TEXT NOT NULL,
  exchange_version TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE catalog_exchange_import_previews (
  preview_id TEXT PRIMARY KEY,
  export_id TEXT NOT NULL REFERENCES catalog_exchange_exports(export_id),
  workbook_sha256 TEXT NOT NULL,
  parsed_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE catalog_exchange_import_receipts (
  request_key TEXT PRIMARY KEY,
  preview_id TEXT NOT NULL,
  workbook_sha256 TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE catalog_exchange_new_rows (
  export_id TEXT NOT NULL REFERENCES catalog_exchange_exports(export_id),
  row_fingerprint TEXT NOT NULL,
  segment_id TEXT NOT NULL REFERENCES catalog_segment_marks(segment_id),
  PRIMARY KEY(export_id,row_fingerprint)
);
