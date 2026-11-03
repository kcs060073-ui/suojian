-- 027: multiple named model profiles with provider accounts and frozen task bindings.

CREATE TABLE model_provider_accounts(
  account_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  provider TEXT NOT NULL,
  base_url TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(provider, name)
);

CREATE TABLE model_profiles(
  profile_id TEXT PRIMARY KEY,
  capability TEXT NOT NULL,
  display_name TEXT NOT NULL,
  account_id TEXT NOT NULL REFERENCES model_provider_accounts(account_id)
    ON DELETE RESTRICT,
  model TEXT NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}',
  is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  availability_status TEXT NOT NULL DEFAULT 'untested'
    CHECK(availability_status IN ('untested', 'available', 'unavailable')),
  last_checked_at TEXT,
  last_error_code TEXT,
  last_error_message TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(capability, display_name)
);
CREATE UNIQUE INDEX idx_model_profiles_one_default
  ON model_profiles(capability) WHERE is_default = 1;
CREATE INDEX idx_model_profiles_capability
  ON model_profiles(capability, enabled, display_name);

CREATE TABLE task_model_bindings(
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  capability TEXT NOT NULL,
  profile_id TEXT NOT NULL REFERENCES model_profiles(profile_id) ON DELETE RESTRICT,
  snapshot_json TEXT NOT NULL,
  snapshot_digest TEXT NOT NULL,
  bound_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(task_id, capability)
);
CREATE INDEX idx_task_model_bindings_profile
  ON task_model_bindings(profile_id, task_id);
