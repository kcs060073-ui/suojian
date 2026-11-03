-- One durable reservation per exact preview input; uncertain calls are never replayed.
CREATE TABLE voice_preview_requests (
    input_digest TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    profile_id TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','succeeded','rejected','unknown')),
    preview_url TEXT,
    output_sha256 TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
