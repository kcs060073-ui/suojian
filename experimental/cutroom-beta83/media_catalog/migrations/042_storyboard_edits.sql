-- Reversible edits to the same scene_cards ledger used by rough cut and render.
CREATE TABLE storyboard_edits (
  edit_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('applied','reverted')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_storyboard_edits_task ON storyboard_edits(task_id, status, created_at);
