ALTER TABLE packaging_plans
  ADD COLUMN origin_type TEXT NOT NULL DEFAULT 'custom'
  CHECK(origin_type IN('custom','system_recommendation'));

UPDATE packaging_plans
SET origin_type='system_recommendation'
WHERE status='draft'
  AND plan_name IN(
    '方案A · 推荐主线',
    '方案B · 备选情绪+贴纸',
    '方案C · 备选动态+花字'
  );

CREATE INDEX idx_packaging_plans_origin
  ON packaging_plans(task_id, origin_type, created_at);
