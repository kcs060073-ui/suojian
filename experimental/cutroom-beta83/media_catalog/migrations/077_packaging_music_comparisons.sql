CREATE TABLE packaging_music_comparisons (
 request_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
 base_plan_id TEXT NOT NULL REFERENCES packaging_plans(plan_id) ON DELETE CASCADE,
 context_digest TEXT NOT NULL,
 asset_ref TEXT,
 plan_id TEXT NOT NULL REFERENCES packaging_plans(plan_id) ON DELETE CASCADE,
 created_at TEXT NOT NULL DEFAULT(datetime('now'))
);
