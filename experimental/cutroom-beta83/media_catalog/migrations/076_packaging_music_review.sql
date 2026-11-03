ALTER TABLE packaging_preview_artifacts ADD COLUMN audio_advice_json TEXT;
CREATE TABLE packaging_music_feedback(
  request_id TEXT PRIMARY KEY,
  preview_id TEXT NOT NULL REFERENCES packaging_preview_artifacts(preview_id) ON DELETE CASCADE,
  rating TEXT NOT NULL CHECK(rating IN('too_weak','audible_raise','balanced','too_loud','style_mismatch')),
  created_at TEXT NOT NULL DEFAULT(datetime('now'))
);
CREATE INDEX idx_music_feedback_preview ON packaging_music_feedback(preview_id,created_at);
