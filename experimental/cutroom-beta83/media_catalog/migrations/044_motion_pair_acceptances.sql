-- User acceptance applies to one exact adjacent pair and source/analysis revisions.
CREATE TABLE motion_pair_acceptances (
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  pair_key TEXT NOT NULL,
  left_shot_id TEXT NOT NULL,
  right_shot_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  pair_state_json TEXT NOT NULL,
  accepted_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(task_id, pair_key)
);
