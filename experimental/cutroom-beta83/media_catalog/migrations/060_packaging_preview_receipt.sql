-- Frozen RenderRecipe preview provenance, nullable for existing digital previews.
ALTER TABLE packaging_preview_artifacts
  ADD COLUMN preview_receipt_json TEXT;
