CREATE TABLE edit_proposals (
 proposal_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
 request_id TEXT NOT NULL,
 request_digest TEXT NOT NULL,
 task_revision TEXT NOT NULL,
 inputs_digest TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','needs_clarification','blocked','applied','cancelled','reverted')),
 result_json TEXT NOT NULL,
 receipt_json TEXT,
 after_inputs_digest TEXT,
 expires_at INTEGER NOT NULL,
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 updated_at TEXT NOT NULL DEFAULT (datetime('now')),
 UNIQUE(task_id,request_id)
);
CREATE TABLE edit_proposal_receipts (
 task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
 request_id TEXT NOT NULL,
 proposal_id TEXT NOT NULL REFERENCES edit_proposals(proposal_id),
 action TEXT NOT NULL,
 request_digest TEXT NOT NULL,
 result_json TEXT NOT NULL,
 PRIMARY KEY(task_id,request_id)
);
