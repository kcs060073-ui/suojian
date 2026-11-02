-- P3-M4 脚本草稿（视频脚本写作适配器；园区无文案入口优先，保持人工文案回退）
CREATE TABLE IF NOT EXISTS script_drafts (
  draft_id    TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL,
  route       TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'template',   -- template | llm
  status      TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft', 'confirmed', 'rejected')),
  outline     TEXT,                                -- JSON：分镜/要点
  script      TEXT NOT NULL,                        -- 草稿正文（需人工确认才生效）
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  confirmed_at TEXT
);
