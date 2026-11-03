-- Keep task-local draft elements when a category is switched off.
-- Historical schema-v1 plans remain enabled and retain their gate digests.
ALTER TABLE packaging_plan_items
ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1));
