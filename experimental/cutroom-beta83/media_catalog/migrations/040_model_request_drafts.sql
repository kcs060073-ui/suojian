-- Typed model requests and immutable suggestion receipts; no approval side effects.
CREATE TABLE model_contract_requests (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    base_revision INTEGER NOT NULL CHECK(base_revision >= 0),
    input_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    registration_digest TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_model_requests_task ON model_contract_requests(task_id, created_at);
CREATE TABLE model_result_drafts (
    request_id TEXT PRIMARY KEY REFERENCES model_contract_requests(request_id) ON DELETE CASCADE,
    receipt_id TEXT NOT NULL UNIQUE,
    response_json TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draft', 'stale')),
    validation_code TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
