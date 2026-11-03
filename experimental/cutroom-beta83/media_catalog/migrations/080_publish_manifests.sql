-- Preserve the exact committed delivery inventory independently of local files.
-- Existing packages are not silently trusted/backfilled from mutable disk manifests.
CREATE TABLE publish_manifests (
    record_id TEXT PRIMARY KEY REFERENCES publish_records(record_id) ON DELETE CASCADE,
    manifest_json TEXT NOT NULL,
    manifest_signature_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
