"""P3-M1 规则版本化测试：版本读写、bump 递增、任务锁定、新任务默认最新。"""

import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.rules import (
    DEFAULT_PAYLOAD,
    add_version,
    current_version,
    ensure_seed,
    get_payload,
    list_versions,
    rules_for_task,
    show_version,
)
from media_catalog.tasks import create_task, get_task


class RulesVersioningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_seed_creates_v1_baseline(self):
        ensure_seed(self.db)
        self.assertEqual(current_version(self.db), 1)
        payload = get_payload(self.db, 1)
        self.assertEqual(payload, DEFAULT_PAYLOAD)
        # 幂等：重复 ensure_seed 不重建
        ensure_seed(self.db)
        self.assertEqual(current_version(self.db), 1)

    def test_add_version_increments(self):
        ensure_seed(self.db)
        v2 = add_version(self.db, {"qa": {"black": {"block_above": 0.5}}}, notes="调严")
        self.assertEqual(v2, 2)
        self.assertEqual(current_version(self.db), 2)
        v3 = add_version(self.db, {"qa": {}})
        self.assertEqual(v3, 3)
        self.assertEqual(current_version(self.db), 3)

    def test_version_payload_is_isolated(self):
        ensure_seed(self.db)
        add_version(self.db, {"qa": {"black": {"block_above": 0.5}}})
        v1 = get_payload(self.db, 1)
        v2 = get_payload(self.db, 2)
        self.assertEqual(v1["qa"]["black"]["block_above"], 1.0)
        self.assertEqual(v2["qa"]["black"]["block_above"], 0.5)
        # 不存在版本回退默认
        self.assertEqual(get_payload(self.db, 99), DEFAULT_PAYLOAD)

    def test_rules_for_task_locks_version(self):
        ensure_seed(self.db)
        create_task(self.db, "task-a", "旧任务")
        self.assertEqual(get_task(self.db, "task-a")["rule_version"], 1)
        add_version(self.db, {"qa": {}})  # bump 到 v2
        # 旧任务锁定 v1
        payload, version = rules_for_task(self.db, "task-a")
        self.assertEqual(version, 1)
        self.assertEqual(payload["qa"]["black"]["block_above"], 1.0)
        # 新任务默认最新 v2
        create_task(self.db, "task-b", "新任务")
        self.assertEqual(get_task(self.db, "task-b")["rule_version"], 2)
        payload_b, version_b = rules_for_task(self.db, "task-b")
        self.assertEqual(version_b, 2)

    def test_rules_for_task_unlocked_falls_back_to_current(self):
        ensure_seed(self.db)
        add_version(self.db, {"qa": {}})
        # 手工插入无锁定的任务（rule_version NULL）
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id, title, task_type, status) "
                "VALUES('task-x', '无锁定', '数字人混剪', 'draft')"
            )
        payload, version = rules_for_task(self.db, "task-x")
        self.assertEqual(version, 2)

    def test_list_and_show_versions(self):
        ensure_seed(self.db)
        add_version(self.db, {"qa": {"x": 1}}, notes="第二版")
        versions = list_versions(self.db)
        self.assertEqual([v["version"] for v in versions], [1, 2])
        shown = show_version(self.db, 2)
        self.assertIsNotNone(shown)
        self.assertEqual(shown["notes"], "第二版")
        self.assertIn("payload", shown)
        self.assertIsNone(show_version(self.db, 99))


if __name__ == "__main__":
    unittest.main()
