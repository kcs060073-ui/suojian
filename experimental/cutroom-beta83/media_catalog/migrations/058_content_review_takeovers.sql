-- Explicit, candidate-bound acknowledgement of incomplete advisory checking.
CREATE TABLE content_review_takeovers (
    takeover_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES candidate_versions(candidate_id),
    run_id TEXT NOT NULL REFERENCES content_review_runs(run_id),
    qa_snapshot_id TEXT NOT NULL,
    qa_report_id TEXT NOT NULL,
    watch_session_id TEXT NOT NULL REFERENCES review_watch_sessions(session_id),
    review_status TEXT NOT NULL CHECK(review_status IN ('partial','failed')),
    acknowledged_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(task_id,candidate_id,run_id)
);
CREATE INDEX idx_content_review_takeover_candidate
    ON content_review_takeovers(task_id,candidate_id,run_id);
