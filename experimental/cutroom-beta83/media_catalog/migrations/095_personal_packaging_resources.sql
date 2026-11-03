CREATE TABLE IF NOT EXISTS personal_packaging_resources (asset_ref TEXT PRIMARY KEY, category TEXT NOT NULL, item_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
