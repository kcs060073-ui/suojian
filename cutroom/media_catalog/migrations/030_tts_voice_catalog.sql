-- 030: cached selectable TTS voices and refresh state per provider resource.

CREATE TABLE tts_voice_catalog(
  account_id TEXT NOT NULL REFERENCES model_provider_accounts(account_id)
    ON DELETE CASCADE,
  resource_id TEXT NOT NULL,
  voice_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  gender TEXT,
  languages_json TEXT NOT NULL DEFAULT '[]',
  styles_json TEXT NOT NULL DEFAULT '[]',
  emotions_json TEXT NOT NULL DEFAULT '[]',
  preview_url TEXT,
  short_preview_url TEXT,
  source TEXT NOT NULL
    CHECK(source IN ('provider_catalog', 'legacy_saved')),
  selectable INTEGER NOT NULL DEFAULT 1 CHECK(selectable IN (0, 1)),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  last_seen_at TEXT NOT NULL,
  last_tested_at TEXT,
  test_status TEXT NOT NULL DEFAULT 'untested'
    CHECK(test_status IN ('untested', 'available', 'unavailable')),
  last_test_error_code TEXT,
  last_test_error_message TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(account_id, resource_id, voice_id)
);
CREATE INDEX idx_tts_voice_catalog_selectable
  ON tts_voice_catalog(account_id, resource_id, selectable, display_name);

CREATE TABLE tts_voice_catalog_state(
  account_id TEXT NOT NULL REFERENCES model_provider_accounts(account_id)
    ON DELETE CASCADE,
  resource_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'never'
    CHECK(status IN ('never', 'success', 'failed')),
  last_attempt_at TEXT,
  last_success_at TEXT,
  last_error_code TEXT,
  last_error_message TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(account_id, resource_id)
);
