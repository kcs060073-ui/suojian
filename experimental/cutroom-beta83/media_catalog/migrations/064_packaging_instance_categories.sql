-- Older editable v2 plans could contain task elements without a category item.
-- Backfill only drafts; confirmed plan rows and historical gate digests stay intact.
INSERT INTO packaging_plan_items(plan_id,slot,asset_ref)
SELECT p.plan_id, i.role, i.asset_ref
FROM packaging_plans p
JOIN packaging_plan_instances i ON i.plan_id=p.plan_id
WHERE p.schema_version=2 AND p.status='draft'
  AND i.instance_id=(
      SELECT first.instance_id FROM packaging_plan_instances first
      WHERE first.plan_id=i.plan_id AND first.role=i.role
      ORDER BY first.created_at,first.instance_id LIMIT 1
  )
  AND NOT EXISTS(
      SELECT 1 FROM packaging_plan_items item
      WHERE item.plan_id=p.plan_id AND item.slot=i.role
        AND item.is_backup=0
  );
