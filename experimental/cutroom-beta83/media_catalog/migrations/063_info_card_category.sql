-- Existing editable v2 plans may contain info cards without a category item.
-- Confirmed plan digests remain untouched; clones acquire the new category.
INSERT INTO packaging_plan_items(plan_id,slot,asset_ref)
SELECT p.plan_id, 'info_card',
       (SELECT i.asset_ref FROM packaging_plan_instances i
        WHERE i.plan_id=p.plan_id AND i.role='info_card'
        ORDER BY i.created_at,i.instance_id LIMIT 1)
FROM packaging_plans p
WHERE p.schema_version=2 AND p.status='draft'
  AND EXISTS(SELECT 1 FROM packaging_plan_instances i
             WHERE i.plan_id=p.plan_id AND i.role='info_card')
  AND NOT EXISTS(SELECT 1 FROM packaging_plan_items item
                 WHERE item.plan_id=p.plan_id AND item.slot='info_card'
                   AND item.is_backup=0);
