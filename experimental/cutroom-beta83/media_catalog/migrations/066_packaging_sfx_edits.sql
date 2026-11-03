-- Optional audio edits. NULL preserves the historical source-zero and short fade-out recipe.
-- Confirmed rows are not rewritten so their original gate digest remains stable.
ALTER TABLE packaging_plan_instances ADD COLUMN source_start_seconds REAL;
ALTER TABLE packaging_plan_instances ADD COLUMN fade_in_seconds REAL;
ALTER TABLE packaging_plan_instances ADD COLUMN fade_out_seconds REAL;
