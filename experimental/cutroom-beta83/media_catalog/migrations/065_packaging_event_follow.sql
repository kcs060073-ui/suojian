-- Keep timing intent independent of origin: changing gain or the asset is a
-- manual edit, but a model sound may still follow its verified visible event.
ALTER TABLE packaging_plan_instances
ADD COLUMN follow_event INTEGER NOT NULL DEFAULT 0 CHECK(follow_event IN (0,1));

-- Only mutable drafts are backfilled. Confirmed history and gate digests stay fixed.
UPDATE packaging_plan_instances AS sound
SET follow_event=1
WHERE sound.role='sfx' AND sound.origin='recommendation'
  AND sound.anchor_kind='instance'
  AND EXISTS(
    SELECT 1 FROM packaging_plan_instances title
    JOIN packaging_plans plan ON plan.plan_id=title.plan_id
    WHERE title.instance_id=sound.anchor_ref AND title.plan_id=sound.plan_id
      AND title.role='title_card' AND plan.status='draft'
  );
