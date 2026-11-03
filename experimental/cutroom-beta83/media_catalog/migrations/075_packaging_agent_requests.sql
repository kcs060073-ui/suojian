CREATE TABLE packaging_agent_requests (
 request_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 base_plan_id TEXT REFERENCES packaging_plans(plan_id),
 style TEXT NOT NULL CHECK(style IN ('business','focus')),
 input_digest TEXT NOT NULL,
 prompt_version TEXT NOT NULL,
 prompt_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('waiting_agent','claimed','completed','partial','needs_input','stale','cancelled')),
 claimed_by TEXT,
 expires_at REAL NOT NULL,
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_packaging_agent_task ON packaging_agent_requests(task_id);
