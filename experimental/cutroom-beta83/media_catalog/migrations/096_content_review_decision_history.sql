-- Preserve previous human explanations when an unapproved candidate is rechecked.
CREATE TABLE content_review_decision_history (
 decision_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
 candidate_id TEXT NOT NULL REFERENCES candidate_versions(candidate_id),
 run_id TEXT NOT NULL REFERENCES content_review_runs(run_id),
 issue_index INTEGER NOT NULL,
 disposition TEXT NOT NULL,
 reason TEXT NOT NULL,
 reviewed_by TEXT NOT NULL,
 created_at TEXT NOT NULL,
 replaced_by TEXT NOT NULL,
 revised_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_content_review_decision_history_run ON content_review_decision_history(run_id,issue_index);
