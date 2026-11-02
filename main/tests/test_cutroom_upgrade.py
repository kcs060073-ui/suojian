import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from suo_jian.integrations.cutroom.installation import installation_info, require_existing_database, mcp_arguments, execution_root
from suo_jian.integrations.cutroom.execution_plan import candidate_render_path
from suo_jian.integrations.cutroom.client import call_local_adapter
from suo_jian.integrations.cutroom.unified import UnifiedExecution
from suo_jian.blueprint.templates import TemplateStore


class CutroomUpgradeTests(unittest.TestCase):
    def fixture(self, root, compiled=False, schema=39):
        (root / "media_catalog/migrations").mkdir(parents=True)
        (root / f"media_catalog/migrations/{schema:03}_test.sql").write_text("", encoding="utf-8")
        (root / "package.json").write_text(json.dumps({"version": "0.9.0-beta.8"}), encoding="utf-8")
        if compiled:
            (root / "release-manifest.json").write_text(json.dumps({"version": "0.9.0-beta.83", "distribution": "compiled-loaders-v1"}), encoding="utf-8")
        database = root / "runtime/workbench/catalog.sqlite"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE schema_migrations(version INTEGER)")
            db.execute("INSERT INTO schema_migrations VALUES (?)", (schema,))
            db.commit()
        return database

    def test_status_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertFalse(installation_info(root)["database_exists"])
            with self.assertRaisesRegex(ValueError, "任务库"):
                require_existing_database(root)
            self.assertFalse((root / "runtime").exists())

    def test_mismatched_schema_is_rejected_without_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); database = self.fixture(root, compiled=True)
            (root / "media_catalog/migrations/096_test.sql").write_text("", encoding="utf-8")
            before = database.read_bytes()
            with self.assertRaisesRegex(ValueError, "39.*96"):
                require_existing_database(root)
            self.assertEqual(database.read_bytes(), before)

    def test_new_mcp_binds_exact_database_and_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); self.fixture(root, compiled=True, schema=96)
            arguments = mcp_arguments(root, "http://127.0.0.1:8873")
            self.assertEqual(arguments[arguments.index("--db") + 1], str(root / "runtime/workbench/catalog.sqlite"))
            self.assertEqual(arguments[-1], "http://127.0.0.1:8873")
            with self.assertRaises(ValueError):
                mcp_arguments(root, "http://remote.example:8765")

    def test_legacy_mcp_does_not_receive_unsupported_flags(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); self.fixture(root)
            self.assertEqual(mcp_arguments(root)[1:], ["--client", "codex"])

    def test_compiled_backend_never_uses_old_confirmation_bypass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); self.fixture(root, compiled=True, schema=96)
            with patch("suo_jian.integrations.cutroom.client.subprocess.run") as invoke:
                with self.assertRaisesRegex(ValueError, "工作台"):
                    call_local_adapter(root, "lock_picture", {"owns_materials": True})
                invoke.assert_not_called()
                with self.assertRaisesRegex(ValueError, "工作台"):
                    call_local_adapter(root, "create", {})
                invoke.assert_not_called()

    def test_existing_execution_uses_original_installation_after_default_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); old = root / "old"; new = root / "new"; result = root / "result"; result.mkdir()
            task = "task_" + "a" * 32
            state = {"task_id": task, "preview_path": str(old / "out" / task / "candidate-v12/video-final.mp4")}
            (result / "execution.json").write_text(json.dumps(state), encoding="utf-8")
            runner = UnifiedExecution(new, result, TemplateStore(root))
            self.assertEqual(runner.root, old.resolve())
            runner.save(phase="test")
            self.assertEqual(json.loads((result / "execution.json").read_text())["cutroom_root"], str(old.resolve()))
            self.assertEqual(execution_root(new, result, {}), new.resolve())

    def test_matching_binding_preserves_incomplete_task_without_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); task = "task_" + "b" * 32
            (root / "cutroom_binding.json").write_text(json.dumps({"task_id": task, "cutroom_root": str(root / "old")}), encoding="utf-8")
            self.assertEqual(execution_root(root / "new", root, {"task_id": task}), (root / "old").resolve())
            self.assertEqual(execution_root(root / "new", root, {"task_id": "other"}), (root / "new").resolve())

    def test_preview_remains_readable_after_default_backend_changes(self):
        from flask import Flask
        from suo_jian.ui.unified_api import register_unified_routes
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); result = root / "results/aabbccdd"; result.mkdir(parents=True)
            task = "task_" + "c" * 32
            preview = root / "old/out" / task / "candidate-v12/video-final.mp4"
            preview.parent.mkdir(parents=True); preview.write_bytes(b"existing-preview")
            (result / "execution.json").write_text(json.dumps({"task_id": task, "preview_path": str(preview)}), encoding="utf-8")
            app = Flask(__name__)
            register_unified_routes(app, root, lambda job: ({}, None, result))
            with patch.dict(os.environ, {"SUOJIAN_CUTROOM_ROOT": str(root / "new")}):
                response = app.test_client().get("/api/unified/preview/aabbccdd")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b"existing-preview")
                response.close()

    def test_render_binding_ignores_file_mtime_and_never_substitutes_another_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); render = root / "runtime/workbench/tasks/task-a/render"
            for label in ["candidate-v2", "candidate-v12"]:
                (render / label).mkdir(parents=True)
                (render / label / "adrender.json").write_text("{}", encoding="utf-8")
            os.utime(render / "candidate-v2/adrender.json", (2000000000, 2000000000))
            rows = [{"version_no": "2", "version_label": "candidate-v2"}, {"version_no": "12", "version_label": "candidate-v12"}]
            self.assertEqual(candidate_render_path(root, "task-a", rows), (render / "candidate-v12/adrender.json").resolve())
            rows[-1]["version_label"] = "candidate-v13"
            self.assertIsNone(candidate_render_path(root, "task-a", rows))
            rows[-1]["version_label"] = "../escape"
            with self.assertRaises(ValueError):
                candidate_render_path(root, "task-a", rows)


if __name__ == "__main__":
    unittest.main()
