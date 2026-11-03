-- Frozen Step 05: task-local subtitle emphasis and BGM volume.
-- NULL gain preserves the registered track's calibrated default for existing plans.
ALTER TABLE packaging_plans ADD COLUMN subtitle_highlight INTEGER NOT NULL DEFAULT 1
  CHECK(subtitle_highlight IN (0, 1));
ALTER TABLE packaging_plans ADD COLUMN bgm_gain REAL
  CHECK(bgm_gain IS NULL OR (bgm_gain >= 0 AND bgm_gain <= 0.5));
ALTER TABLE packaging_plans ADD COLUMN settings_revision INTEGER NOT NULL DEFAULT 0;
-- Historical v2 confirmed digests and render snapshots did not contain settings.
ALTER TABLE packaging_plans ADD COLUMN settings_schema_version INTEGER NOT NULL DEFAULT 0;
