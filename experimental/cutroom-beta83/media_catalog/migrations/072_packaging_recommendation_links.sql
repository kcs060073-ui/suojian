CREATE TABLE IF NOT EXISTS packaging_recommendation_links (
 receipt_id TEXT PRIMARY KEY REFERENCES model_result_drafts(receipt_id),
 plan_id TEXT NOT NULL UNIQUE REFERENCES packaging_plans(plan_id),
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 method_version TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
