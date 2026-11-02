import importlib
import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.tasks import create_task


def _artifact_module():
    try:
        return importlib.import_module("media_catalog.agent_artifacts")
    except ModuleNotFoundError:
        raise AssertionError("media_catalog.agent_artifacts must exist") from None


class AgentArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase.open(Path(self.tmp.name) / "catalog.sqlite")
        self.db.migrate()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_task_mode_defaults_to_standalone_and_accepts_explicit_agent(self):
        standalone = create_task(self.db, "standalone-task", "独立任务")
        try:
            agent = create_task(
                self.db,
                "agent-task",
                "Agent 任务",
                orchestration_mode="agent",
            )
        except TypeError:
            self.fail("create_task must accept orchestration_mode")

        self.assertEqual(standalone["orchestration_mode"], "standalone")
        self.assertEqual(agent["orchestration_mode"], "agent")
        with self.assertRaisesRegex(ValueError, "ORCHESTRATION_MODE_INVALID"):
            create_task(
                self.db,
                "invalid-task",
                "非法任务",
                orchestration_mode="automatic",
            )

    def test_same_content_and_input_digest_is_idempotent(self):
        artifacts = _artifact_module()
        create_task(
            self.db,
            "agent-task",
            "Agent 任务",
            orchestration_mode="agent",
        )
        first, created = artifacts.save_agent_artifact(
            self.db,
            task_id="agent-task",
            artifact_type="digital_storyboard",
            schema_version=1,
            content={"brief": {"broll": []}, "plan": {}},
            input_digest="input-1",
            source_agent="codex",
            source_model_label="current-agent-model",
        )
        second, created_again = artifacts.save_agent_artifact(
            self.db,
            task_id="agent-task",
            artifact_type="digital_storyboard",
            schema_version=1,
            content={"plan": {}, "brief": {"broll": []}},
            input_digest="input-1",
            source_agent="codex",
            source_model_label="current-agent-model",
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["artifact_id"], second["artifact_id"])
        self.assertEqual(first["content"], {"brief": {"broll": []}, "plan": {}})
        self.assertNotIn("content_json", first)

    def test_changed_content_creates_a_second_immutable_version(self):
        artifacts = _artifact_module()
        create_task(
            self.db,
            "agent-task",
            "Agent 任务",
            orchestration_mode="agent",
        )
        first, _ = artifacts.save_agent_artifact(
            self.db,
            task_id="agent-task",
            artifact_type="digital_storyboard",
            schema_version=1,
            content={"brief": {"broll": []}, "plan": {"version": 1}},
            input_digest="input-1",
            source_agent="codex",
            source_model_label=None,
        )
        second, _ = artifacts.save_agent_artifact(
            self.db,
            task_id="agent-task",
            artifact_type="digital_storyboard",
            schema_version=1,
            content={"brief": {"broll": []}, "plan": {"version": 2}},
            input_digest="input-1",
            source_agent="codex",
            source_model_label=None,
        )

        rows = artifacts.list_agent_artifacts(
            self.db,
            "agent-task",
            "digital_storyboard",
        )
        self.assertEqual([row["artifact_id"] for row in rows], [first["artifact_id"], second["artifact_id"]])
        self.assertNotEqual(first["content_digest"], second["content_digest"])

    def test_store_rejects_standalone_tasks_and_unknown_artifact_types(self):
        artifacts = _artifact_module()
        create_task(self.db, "standalone-task", "独立任务")
        with self.assertRaisesRegex(ValueError, "AGENT_MODE_REQUIRED"):
            artifacts.save_agent_artifact(
                self.db,
                task_id="standalone-task",
                artifact_type="digital_storyboard",
                schema_version=1,
                content={"brief": {"broll": []}, "plan": {}},
                input_digest="input-1",
                source_agent="codex",
                source_model_label=None,
            )

        create_task(
            self.db,
            "agent-task",
            "Agent 任务",
            orchestration_mode="agent",
        )
        with self.assertRaisesRegex(ValueError, "AGENT_ARTIFACT_TYPE_INVALID"):
            artifacts.save_agent_artifact(
                self.db,
                task_id="agent-task",
                artifact_type="shell_command",
                schema_version=1,
                content={"command": "whoami"},
                input_digest="input-1",
                source_agent="codex",
                source_model_label=None,
            )


if __name__ == "__main__":
    unittest.main()
