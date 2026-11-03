-- Invalidate immutable render identities, including candidates attached after QA.
CREATE TABLE render_invalidations(
  snapshot_id TEXT PRIMARY KEY REFERENCES render_snapshots(snapshot_id) ON DELETE CASCADE,
  stage_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE VIEW candidate_invalidations AS
SELECT c.candidate_id,i.stage_key,i.reason,i.created_at
FROM candidate_versions c JOIN render_invalidations i ON i.snapshot_id=c.mix_snapshot_id;
CREATE TABLE task_render_epochs(
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch>=0)
);

-- Backfill render identities before legacy candidate recovery runs.
INSERT INTO render_invalidations(snapshot_id,stage_key,reason)
SELECT r.snapshot_id,'fine_render','既有流程已要求重新渲染或质检'
FROM render_snapshots r WHERE r.stage='mix'
AND EXISTS(SELECT 1 FROM task_stage_states s WHERE s.task_id=r.task_id
           AND s.stage_key IN ('fine_render','auto_qa') AND s.status='invalidated');
INSERT INTO task_render_epochs(task_id,epoch)
SELECT DISTINCT r.task_id,1 FROM render_snapshots r JOIN render_invalidations i ON i.snapshot_id=r.snapshot_id;
UPDATE publish_records SET package_status='expired'
WHERE package_status='current' AND candidate_id IN(SELECT candidate_id FROM candidate_invalidations);
