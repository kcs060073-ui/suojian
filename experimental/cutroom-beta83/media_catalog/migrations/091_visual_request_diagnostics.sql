-- Bounded provider shape/usage receipts; no response or reasoning bodies.
CREATE TABLE visual_request_diagnostics (
 job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
 item_key TEXT NOT NULL DEFAULT '',
 diagnostics_json TEXT NOT NULL,
 updated_at TEXT NOT NULL DEFAULT (datetime('now')),
 PRIMARY KEY(job_id,item_key)
);
