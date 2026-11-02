"""P3-M4 脚本写作适配器测试：模板草稿、落库、人工确认/驳回回退门禁。"""

import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.script_adapter import (
    confirm_draft,
    draft_script,
    get_draft,
    list_drafts,
    reject_draft,
    template_draft,
)


class TemplateDraftTest(unittest.TestCase):
    def test_park_route_script_shape(self):
        out = template_draft("园区宣传", ["public/昌吉信息产业园.mp4", "public/厂房空镜.mp4"])
        script = out["script"]
        outline = out["outline"]
        self.assertTrue(script.startswith("走进这片园区"))
        self.assertTrue(script.endswith("。"))
        self.assertEqual(len(outline["points"]), 2)
        self.assertIn("昌吉", outline["features"][0])

    def test_route_variants(self):
        for route in ("数字人混剪", "园区宣传", "信息流广告"):
            out = template_draft(route, ["public/新疆数据中心.mp4"])
            self.assertIn("。", out["script"])

    def test_no_features_fallback(self):
        # 短文本(<2)且被排除的 token 不产生特征 → 回退占位卖点
        out = template_draft("园区宣传", ["a", "b", "public", "seed"])
        self.assertIn("园区核心优势", out["outline"]["features"])

    def test_unknown_route_falls_back_to_ad_style(self):
        # template_draft 对未知 route 落到信息流分支（不抛）；route 强校验在 draft_script
        out = template_draft("未知路线", ["新疆数据中心.mp4"])
        self.assertIn("一条视频", out["outline"]["opening"])


class DraftLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO libraries(library_id, name, kind, mount_path, online, created_at, updated_at) "
                "VALUES('lib', 'lib', 'local', ?, 1, datetime('now'), datetime('now'))",
                (str(self.root),),
            )
            conn.execute(
                "INSERT INTO assets(asset_id, media_kind, size_bytes, quick_fingerprint, created_at) "
                "VALUES('asset-1', 'video', 10, 'fp', datetime('now'))"
            )
            conn.execute(
                "INSERT INTO asset_locations(asset_id, library_id, relative_path, mtime_ns, state) "
                "VALUES('asset-1', 'lib', 'library/昌吉产业园.mp4', 1, 'available')"
            )
            conn.execute(
                "INSERT INTO tasks(task_id, title, task_type, status) VALUES('task-1','t','园区宣传','draft')"
            )
            conn.execute(
                "INSERT INTO task_assets(task_id, asset_id, role) VALUES('task-1','asset-1','broll')"
            )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_draft_persisted_as_draft(self):
        draft = draft_script(self.db, "task-1", "园区宣传")
        self.assertTrue(draft["draft_id"].startswith("sd-"))
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["source"], "template")
        stored = get_draft(self.db, draft["draft_id"])
        self.assertEqual(stored["task_id"], "task-1")
        self.assertEqual(stored["route"], "园区宣传")
        self.assertIn("script", stored)
        self.assertIn("outline", stored)
        self.assertIsInstance(stored["outline"], dict)

    def test_confirm_gate_applies(self):
        draft = draft_script(self.db, "task-1", "园区宣传")
        confirmed = confirm_draft(self.db, draft["draft_id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertIsNotNone(confirmed.get("confirmed_at"))

    def test_reject_rollback_to_manual(self):
        draft = draft_script(self.db, "task-1", "园区宣传")
        rejected = reject_draft(self.db, draft["draft_id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(list_drafts(self.db, task_id="task-1")[0]["status"], "rejected")

    def test_custom_generator_sourced_as_llm(self):
        def fake_generator(route, texts):
            return {"outline": {"opening": "自定", "points": ["x"], "cta": "y", "features": []},
                    "script": "自定脚本。"}
        draft = draft_script(self.db, "task-1", "园区宣传", generator=fake_generator)
        self.assertEqual(draft["source"], "llm")
        self.assertIn("自定脚本", draft["script"])

    def test_invalid_route_raises(self):
        with self.assertRaises(ValueError):
            draft_script(self.db, "task-1", "非法路线")


if __name__ == "__main__":
    unittest.main()
