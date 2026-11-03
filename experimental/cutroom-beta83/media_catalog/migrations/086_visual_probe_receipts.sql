-- Keep immutable operation context even if a model profile is later removed.
-- No provider response text, request headers, prompts or media are stored here.
CREATE TABLE visual_profile_probe_runs (
 operation_id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL,
 config_digest TEXT NOT NULL,
 protocol_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed','unknown','stale')),
 started_at TEXT NOT NULL DEFAULT (datetime('now')),
 finished_at TEXT,
 error_code TEXT,
 diagnostic_json TEXT
);
CREATE INDEX visual_probe_profile_history ON visual_profile_probe_runs(profile_id);
