-- Earlier releases could leave several packages for one candidate marked current.
-- Keep the newest as current while retaining every old package and its files.
UPDATE publish_records
SET package_status='expired'
WHERE package_status='current'
  AND candidate_id IS NOT NULL
  AND rowid < (
    SELECT MAX(newer.rowid)
    FROM publish_records AS newer
    WHERE newer.task_id=publish_records.task_id
      AND newer.candidate_id=publish_records.candidate_id
      AND newer.package_status='current'
  );
