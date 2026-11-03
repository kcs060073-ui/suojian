-- Candidate-bound, append-only release copy edits. Model suggestions remain separate drafts.
CREATE TABLE delivery_copy_drafts (
    draft_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('manual', 'accepted_suggestion')),
    source_ref TEXT,
    model_version TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(task_id, candidate_id, revision)
);
CREATE INDEX idx_delivery_copy_current
    ON delivery_copy_drafts(task_id, candidate_id, revision DESC);
