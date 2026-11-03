-- Model-assisted content checking is advisory evidence, separate from technical QA.
CREATE TABLE content_review_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES candidate_versions(candidate_id),
    qa_snapshot_id TEXT NOT NULL,
    qa_report_id TEXT NOT NULL,
    video_sha256 TEXT NOT NULL,
    duration_seconds REAL NOT NULL CHECK(duration_seconds > 0),
    executor TEXT NOT NULL CHECK(executor IN ('vision_service','agent')),
    prompt_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed')),
    coverage_refs_json TEXT NOT NULL DEFAULT '[]',
    uncovered_json TEXT NOT NULL DEFAULT '[]',
    issues_json TEXT NOT NULL DEFAULT '[]',
    unknowns_json TEXT NOT NULL DEFAULT '[]',
    conclusion TEXT CHECK(conclusion IN ('issues_found','no_issue_in_coverage','insufficient_evidence')),
    error_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_content_review_latest
    ON content_review_runs(task_id, candidate_id, created_at DESC);
