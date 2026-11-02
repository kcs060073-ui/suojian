"""P3-M3 语义/内容匹配测试：分词、打分、排序、DB 集成。"""

import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.match import (
    candidate_text_for_asset,
    match_for_task,
    rank,
    score,
    tokenize,
)


class TokenizeTest(unittest.TestCase):
    def test_english_digit_words_kept(self):
        tokens = tokenize("xjiang 4000P gdp")
        self.assertIn("xjiang", tokens)
        self.assertIn("4000p", tokens)

    def test_chinese_bigrams(self):
        tokens = tokenize("新疆数据中心")
        self.assertIn("新疆", tokens)
        self.assertIn("数据", tokens)
        self.assertIn("中心", tokens)
        # 停用二元组被剔除
        self.assertNotIn("我们", tokenize("我们园区"))

    def test_score_full_hit(self):
        q = tokenize("新疆 数据中心")
        doc = tokenize("新疆数据中心")
        self.assertAlmostEqual(score(q, doc), 1.0, places=3)

    def test_score_partial_hit(self):
        q = tokenize("绿色电力")
        doc = tokenize("数据中心")
        self.assertEqual(score(q, doc), 0.0)


class RankTest(unittest.TestCase):
    def test_rank_orders_by_relevance(self):
        script = "新疆 数据中心 绿色电力"
        candidates = [
            {"id": "c1", "text": "新疆数据中心 服务器 机房"},
            {"id": "c2", "text": "路边小吃 街景"},
            {"id": "c3", "text": "新疆 绿色电力 风电"},
        ]
        ranked = rank(script, candidates)
        self.assertEqual(ranked[0]["id"], "c1")
        self.assertEqual(ranked[2]["id"], "c2")
        self.assertGreaterEqual(ranked[0]["score"], ranked[1]["score"])
        self.assertGreaterEqual(ranked[1]["score"], ranked[2]["score"])


class DbIntegrationTest(unittest.TestCase):
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
            for aid, rel in (
                ("asset-data", "inserts/新疆数据中心.mp4"),
                ("asset-wind", "inserts/新疆风力发电绿电.mp4"),
                ("asset-dh", "digital_human/主播.mp4"),
            ):
                conn.execute(
                    "INSERT INTO assets(asset_id, media_kind, size_bytes, quick_fingerprint, created_at) "
                    "VALUES(?, 'video', 10, ?, datetime('now'))",
                    (aid, f"fp-{aid}"),
                )
                conn.execute(
                    "INSERT INTO asset_locations(asset_id, library_id, relative_path, mtime_ns, state) "
                    "VALUES(?, 'lib', ?, 1, 'available')",
                    (aid, rel),
                )
            conn.execute(
                "INSERT INTO tasks(task_id, title, task_type, status) VALUES('task-1','t','数字人混剪','draft')"
            )
            for aid in ("asset-data", "asset-wind", "asset-dh"):
                conn.execute(
                    "INSERT INTO task_assets(task_id, asset_id, role) VALUES('task-1', ?, 'broll')",
                    (aid,),
                )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_candidate_text_uses_filename(self):
        text = candidate_text_for_asset(self.db, "asset-data")
        self.assertIn("新疆", text)
        self.assertIn("数据中心", text)

    def test_match_for_task_ranks_relevant_first(self):
        result = match_for_task(self.db, "task-1", "新疆 数据中心 机房")
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["id"], "asset-data")
        self.assertEqual(result[2]["id"], "asset-dh")
        for item in result:
            self.assertIn("score", item)


if __name__ == "__main__":
    unittest.main()
