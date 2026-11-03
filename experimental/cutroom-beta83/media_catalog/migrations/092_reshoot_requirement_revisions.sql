-- Explicit user revisions append new requirements and preserve old reports.
CREATE TABLE reshoot_requirement_revisions (
 revision_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
 task_revision TEXT NOT NULL,
 source_report_id TEXT NOT NULL REFERENCES reshoot_reports(report_id),
 source_inputs_digest TEXT NOT NULL,
 old_gap_id TEXT NOT NULL REFERENCES reshoot_requirements(gap_id),
 new_gap_id TEXT NOT NULL UNIQUE REFERENCES reshoot_requirements(gap_id),
 old_query TEXT NOT NULL,
 new_query TEXT NOT NULL,
 reason TEXT NOT NULL,
 result_report_id TEXT REFERENCES reshoot_reports(report_id),
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 UNIQUE(task_id,old_gap_id)
);
CREATE INDEX idx_reshoot_revision_task ON reshoot_requirement_revisions(task_id,new_gap_id);