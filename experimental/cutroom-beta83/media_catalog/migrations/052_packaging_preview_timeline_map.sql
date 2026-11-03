-- Frozen PK-11: persist the immutable short-preview to full-timeline map.
ALTER TABLE packaging_preview_artifacts
  ADD COLUMN timeline_map_json TEXT NOT NULL DEFAULT '[]';
