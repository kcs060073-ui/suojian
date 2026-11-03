-- Human explanations for retaining a candidate after reviewing advisory issues.
CREATE TABLE content_review_issue_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES candidate_versions(candidate_id),
    run_id TEXT NOT NULL REFERENCES content_review_runs(run_id),
    issue_index INTEGER NOT NULL CHECK(issue_index >= 0),
    disposition TEXT NOT NULL CHECK(disposition = 'retain'),
    reason TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(run_id, issue_index)
);
CREATE INDEX idx_content_review_issue_decisions_run
    ON content_review_issue_decisions(task_id, candidate_id, run_id);
