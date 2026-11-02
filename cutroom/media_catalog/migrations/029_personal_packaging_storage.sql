CREATE TABLE IF NOT EXISTS storage_settings (
  setting_key TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);