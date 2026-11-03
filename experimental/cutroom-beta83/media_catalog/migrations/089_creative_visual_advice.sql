CREATE TABLE creative_visual_advice_results (
  job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
  state TEXT NOT NULL CHECK(state IN ('dispatched','saved')),
  result_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
