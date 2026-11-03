-- Separate locked-script segments from shots. Existing asset segment_id remains an
-- asset source window and must never be treated as a script segment identifier.
CREATE TABLE script_segments (
  segment_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  script_revision TEXT NOT NULL,
  idx INTEGER NOT NULL CHECK(idx >= 0),
  text TEXT NOT NULL CHECK(length(trim(text)) > 0),
  output_start_us INTEGER NOT NULL CHECK(output_start_us >= 0),
  output_end_us INTEGER NOT NULL CHECK(output_end_us > output_start_us),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_script_segments_task ON script_segments(task_id, script_revision, idx);
CREATE UNIQUE INDEX idx_script_segments_current_identity
ON script_segments(task_id, script_revision, idx)
WHERE script_revision <> 'legacy';

ALTER TABLE scene_cards ADD COLUMN script_segment_id TEXT REFERENCES script_segments(segment_id);
ALTER TABLE scene_cards ADD COLUMN shot_revision INTEGER NOT NULL DEFAULT 1 CHECK(shot_revision > 0);
CREATE INDEX idx_scene_cards_script_segment ON scene_cards(script_segment_id, idx);

-- Legacy cards have no explicit segment relationship. Backfill one segment per
-- complete card; do not infer that similar text or times represent one segment.
-- Incomplete cards remain unbound and require explicit review before editing.
INSERT INTO script_segments(segment_id,task_id,script_revision,idx,text,output_start_us,output_end_us)
SELECT card_id || ':script',task_id,'legacy',idx,narrative_text,
       CAST(ROUND(narrative_start * 1000000) AS INTEGER),
       CAST(ROUND(narrative_end * 1000000) AS INTEGER)
FROM scene_cards
WHERE narrative_text IS NOT NULL AND length(trim(narrative_text)) > 0
  AND narrative_start IS NOT NULL AND narrative_end IS NOT NULL
  AND idx >= 0 AND narrative_start >= 0 AND narrative_end > narrative_start
  AND ROUND(narrative_end * 1000000) > ROUND(narrative_start * 1000000);

UPDATE scene_cards SET script_segment_id=card_id || ':script'
WHERE EXISTS(SELECT 1 FROM script_segments WHERE segment_id=scene_cards.card_id || ':script');

CREATE TRIGGER scene_card_script_segment_insert
BEFORE INSERT ON scene_cards
WHEN NEW.script_segment_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM script_segments
  WHERE segment_id=NEW.script_segment_id AND task_id=NEW.task_id
)
BEGIN SELECT RAISE(ABORT, 'script segment task mismatch'); END;

CREATE TRIGGER scene_card_script_segment_update
BEFORE UPDATE OF script_segment_id, task_id ON scene_cards
WHEN NEW.script_segment_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM script_segments
  WHERE segment_id=NEW.script_segment_id AND task_id=NEW.task_id
)
BEGIN SELECT RAISE(ABORT, 'script segment task mismatch'); END;
