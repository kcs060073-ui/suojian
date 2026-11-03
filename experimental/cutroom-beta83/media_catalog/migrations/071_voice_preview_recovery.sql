-- Expired reservations require explicit user reconciliation before another paid call.
ALTER TABLE voice_preview_requests ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0;
CREATE TABLE voice_preview_attempt_history (
    request_id TEXT PRIMARY KEY,
    input_digest TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    state TEXT NOT NULL,
    preview_url TEXT,
    output_sha256 TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    replaced_by TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL CHECK(reason IN ('user_confirmed_retry','definite_rejection_retry')),
    archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);
