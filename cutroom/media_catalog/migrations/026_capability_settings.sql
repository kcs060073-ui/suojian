-- 026: keep model routing in SQLite while secrets stay in the OS credential store.

CREATE TABLE capability_settings(
  capability TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  credential_ref TEXT,
  last_checked_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
