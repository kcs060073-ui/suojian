import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.task_sources import set_task_sources
from media_catalog.tasks import create_task, set_status


def _context_module():
    try:
        return importlib.import_module("media_catalog.agent_context")
    except ModuleNotFoundError:
        raise AssertionError("media_catalog.agent_context must exist") from None


class AgentContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.task_id = "agent-task"
        self.script = "锁定文案"
        self.script_path = (
            self.root
            / "runtime"
            / "workbench"
            / "tasks"
            / self.task_id
            / "inputs"
            / "final-script.txt"
        )
        self.script_path.parent.mkdir(parents=True)
        self.script_path.write_text(self.script, encoding="utf-8")
        self._create_agent_task(self.task_id, self.script)

        self.library = self.root / "materials"
        self.library.mkdir()
        register_library(
            self.db,
            "primary",
            "任务素材",
            "local",
            str(self.library),
            purpose="task_specific",
        )
        self.asset_id = "asset-in-scope"
        self._insert_asset(self.asset_id, "primary", "park/shot.mp4")
        set_task_sources(
            self.db,
            self.task_id,
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        set_status(self.db, self.task_id, "brief_locked")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _create_agent_task(self, task_id, script):
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        return create_task(
            self.db,
            task_id,
            "Agent 视频任务",
            task_type="digital_human",
            brief={
                "route": "digital_human",
                "final_script": {
                    "relative_path": "inputs/final-script.txt",
                    "sha256": digest,
                },
            },
            orchestration_mode="agent",
        )

    def _insert_asset(self, asset_id, library_id, relative_path):
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO assets(
                  asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                ) VALUES(?, 'video', 12, ?, datetime('now'))
                """,
                (asset_id, asset_id),
            )
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns, file_identity
                ) VALUES(?, ?, ?, 1, ?)
                """,
                (asset_id, library_id, relative_path, asset_id),
            )

    def test_context_contains_locked_script_scoped_assets_and_stable_digest(self):
        context_module = _context_module()
        context = context_module.build_agent_context(
            self.db,
            self.task_id,
            self.root,
        )

        self.assertEqual(context["task"]["orchestrationMode"], "agent")
        self.assertEqual(context["script"]["text"], self.script)
        self.assertEqual(context["materials"][0]["assetId"], self.asset_id)
        self.assertEqual(
            context["inputDigest"],
            context_module.build_agent_context(
                self.db,
                self.task_id,
                self.root,
            )["inputDigest"],
        )
        self.assertEqual(
            context["inputDigest"],
            context_module.current_agent_input_digest(
                self.db,
                self.task_id,
                self.root,
            ),
        )

    def test_context_rejects_unlocked_or_standalone_tasks_and_has_no_secrets(self):
        context_module = _context_module()
        create_task(self.db, "standalone-task", "独立任务")
        with self.assertRaisesRegex(RuntimeError, "AGENT_MODE_REQUIRED"):
            context_module.build_agent_context(
                self.db,
                "standalone-task",
                self.root,
            )

        unlocked_id = "unlocked-agent"
        unlocked_script = "尚未锁定"
        unlocked_path = (
            self.root
            / "runtime"
            / "workbench"
            / "tasks"
            / unlocked_id
            / "inputs"
            / "final-script.txt"
        )
        unlocked_path.parent.mkdir(parents=True)
        unlocked_path.write_text(unlocked_script, encoding="utf-8")
        self._create_agent_task(unlocked_id, unlocked_script)
        with self.assertRaisesRegex(RuntimeError, "AGENT_BRIEF_LOCK_REQUIRED"):
            context_module.build_agent_context(
                self.db,
                unlocked_id,
                self.root,
            )

        serialized = json.dumps(
            context_module.build_agent_context(
                self.db,
                self.task_id,
                self.root,
            ),
            ensure_ascii=False,
        )
        self.assertNotRegex(
            serialized.lower(),
            r"api[_-]?key|token|credential|cookie|startup",
        )
        self.assertNotIn(str(self.library), serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_context_digest_changes_with_relocked_script_and_excludes_out_of_scope_assets(self):
        context_module = _context_module()
        first = context_module.build_agent_context(
            self.db,
            self.task_id,
            self.root,
        )

        other_library = self.root / "other-materials"
        other_library.mkdir()
        register_library(
            self.db,
            "other",
            "其他项目素材",
            "local",
            str(other_library),
            purpose="task_specific",
        )
        self._insert_asset("asset-out-of-scope", "other", "secret.mp4")

        changed_script = "重新锁定的文案"
        self.script_path.write_text(changed_script, encoding="utf-8")
        self._create_agent_task(self.task_id, changed_script)
        set_status(self.db, self.task_id, "brief_locked")
        second = context_module.build_agent_context(
            self.db,
            self.task_id,
            self.root,
        )

        self.assertNotEqual(first["inputDigest"], second["inputDigest"])
        self.assertEqual(
            [item["assetId"] for item in second["materials"]],
            [self.asset_id],
        )

    def test_context_requires_material_sync_and_rejects_script_path_escape(self):
        context_module = _context_module()
        task_id = "no-materials"
        script = "只有文案"
        script_path = (
            self.root
            / "runtime"
            / "workbench"
            / "tasks"
            / task_id
            / "inputs"
            / "final-script.txt"
        )
        script_path.parent.mkdir(parents=True)
        script_path.write_text(script, encoding="utf-8")
        self._create_agent_task(task_id, script)
        set_status(self.db, task_id, "brief_locked")
        with self.assertRaisesRegex(RuntimeError, "AGENT_MATERIAL_SYNC_REQUIRED"):
            context_module.build_agent_context(self.db, task_id, self.root)

        escaped = self.root / "outside.txt"
        escaped.write_text("不允许读取", encoding="utf-8")
        digest = hashlib.sha256("不允许读取".encode("utf-8")).hexdigest()
        create_task(
            self.db,
            self.task_id,
            "Agent 视频任务",
            task_type="digital_human",
            brief={
                "route": "digital_human",
                "final_script": {
                    "relative_path": "../../../../../outside.txt",
                    "sha256": digest,
                },
            },
            orchestration_mode="agent",
        )
        set_status(self.db, self.task_id, "brief_locked")
        with self.assertRaisesRegex(RuntimeError, "AGENT_SCRIPT_PATH_INVALID"):
            context_module.build_agent_context(
                self.db,
                self.task_id,
                self.root,
            )

    def test_context_reports_missing_locked_script_with_stable_error_code(self):
        context_module = _context_module()
        self.script_path.unlink()

        with self.assertRaisesRegex(RuntimeError, "AGENT_SCRIPT_REQUIRED"):
            context_module.build_agent_context(
                self.db,
                self.task_id,
                self.root,
            )


if __name__ == "__main__":
    unittest.main()
