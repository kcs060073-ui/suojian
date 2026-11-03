-- Library-level visual service is verified for three distinct abilities.
CREATE TABLE visual_profile_checks (
  profile_id TEXT NOT NULL REFERENCES model_profiles(profile_id) ON DELETE CASCADE,
  check_kind TEXT NOT NULL CHECK(check_kind IN ('image','frame_sequence','structured_output')),
  status TEXT NOT NULL CHECK(status IN ('available','unavailable')),
  config_digest TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  error_code TEXT,
  error_message TEXT,
  PRIMARY KEY(profile_id, check_kind)
);
