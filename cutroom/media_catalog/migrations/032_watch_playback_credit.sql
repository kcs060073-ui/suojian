-- Jitter is a bounded carry, never a fresh allowance on every progress request.
ALTER TABLE review_watch_sessions
  ADD COLUMN playback_credit_seconds REAL NOT NULL DEFAULT 0.5
  CHECK(playback_credit_seconds >= 0 AND playback_credit_seconds <= 0.5);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(32, datetime('now'));
