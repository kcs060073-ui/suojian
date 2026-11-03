-- 036: append-only scene recommendation feedback for the two active routes.

CREATE TABLE storyboard_feedback(
  feedback_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  card_id TEXT NOT NULL,
  decision TEXT NOT NULL
    CHECK(decision IN ('rejected','correction','reranked','applied')),
  instruction TEXT,
  original_asset_id TEXT,
  selected_asset_id TEXT,
  candidates_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  applied_at TEXT,
  FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(card_id) REFERENCES scene_cards(card_id) ON DELETE CASCADE,
  FOREIGN KEY(original_asset_id) REFERENCES assets(asset_id) ON DELETE SET NULL,
  FOREIGN KEY(selected_asset_id) REFERENCES assets(asset_id) ON DELETE SET NULL
);

CREATE INDEX idx_storyboard_feedback_task_card
  ON storyboard_feedback(task_id,card_id,created_at,feedback_id);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES(36, datetime('now'));
