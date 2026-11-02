import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from media_catalog.agent_artifacts import list_agent_artifacts
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.task_sources import set_task_sources
from media_catalog.tasks import add_asset, create_task, set_status
from media_catalog.workflow import project_workflow


def _gateway_class():
    try:
        module = importlib.import_module("media_catalog.agent_gateway")
    except ModuleNotFoundError:
        raise AssertionError("media_catalog.agent_gateway must exist") from None
    return module.AgentGateway


class AgentGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.task_id = "agent-task"
        self.script = "锁定原文"
        self.script_digest = hashlib.sha256(
            self.script.encode("utf-8")
        ).hexdigest()
        task_root = (
            self.root
            / "runtime"
            / "workbench"
            / "tasks"
            / self.task_id
        )
        (task_root / "inputs").mkdir(parents=True)
        (task_root / "artifacts").mkdir()
        (task_root / "inputs" / "final-script.txt").write_text(
            self.script,
            encoding="utf-8",
        )
        (task_root / "artifacts" / "transcript.words.json").write_text(
            json.dumps(
                [
                    {"w": "锁定", "start": 0.0, "end": 0.4},
                    {"w": "原文", "start": 0.4, "end": 0.9},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        create_task(
            self.db,
            self.task_id,
            "Agent 数字人",
            task_type="digital_human",
            brief={
                "route": "digital_human",
                "subtype": "zhaoshang",
                "final_script": {
                    "relative_path": "inputs/final-script.txt",
                    "sha256": self.script_digest,
                },
            },
            orchestration_mode="agent",
        )
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
        self._insert_asset("asset-digital", "video", "source.mp4")
        self._insert_asset("asset-image", "image", "园区大门.png")
        set_task_sources(
            self.db,
            self.task_id,
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        add_asset(self.db, self.task_id, "asset-digital", "digital_human")
        set_status(self.db, self.task_id, "brief_locked")
        self.gateway = _gateway_class()(self.db, self.root)
        context = self.gateway.get_agent_context(self.task_id)
        self.assertTrue(context["ok"], context)
        self.input_digest = context["data"]["inputDigest"]
        self.valid_content = {
            "brief": {
                "keywords": ["锁定"],
                "punchLines": [],
                "title": {"text": "测试", "durationSec": 1},
                "broll": [
                    {
                        "anchorText": "原文",
                        "query": "园区大门",
                        "prompt": "园区大门缓慢推近",
                        "kind": "image",
                        "durationSec": 0.4,
                    }
                ],
            },
            "plan": {"speed": 1.25, "titles": [], "inserts": []},
        }

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _insert_asset(self, asset_id, media_kind, relative_path):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                "VALUES(?,?,1,?,datetime('now'))",
                (asset_id, media_kind, "fp-" + asset_id),
            )
            connection.execute(
                "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,file_identity) "
                "VALUES(?,'primary',?,1,?)",
                (asset_id, relative_path, asset_id),
            )

    def test_gateway_has_no_gate_approval_or_credential_methods(self):
        forbidden = {
            "lock_brief",
            "lock_picture",
            "confirm_packaging",
            "approve_review",
            "save_credentials",
        }
        self.assertTrue(forbidden.isdisjoint(dir(self.gateway)))

    def test_invalid_artifact_does_not_advance_or_fallback(self):
        before = project_workflow(self.db, self.task_id)["current_stage"]
        result = self.gateway.submit_planning_artifact(
            task_id=self.task_id,
            artifact_type="digital_storyboard",
            schema_version=1,
            input_digest="stale",
            content=self.valid_content,
            source_agent="codex",
            source_model_label="current-agent-model",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "AGENT_INPUT_STALE")
        self.assertEqual(list_agent_artifacts(self.db, self.task_id), [])
        self.assertEqual(
            project_workflow(self.db, self.task_id)["current_stage"],
            before,
        )

    def test_valid_digital_artifact_is_idempotent_and_finalizes_once(self):
        first = self.gateway.submit_planning_artifact(
            task_id=self.task_id,
            artifact_type="digital_storyboard",
            schema_version=1,
            input_digest=self.input_digest,
            content=self.valid_content,
            source_agent="codex",
            source_model_label="current-agent-model",
        )
        second = self.gateway.submit_planning_artifact(
            task_id=self.task_id,
            artifact_type="digital_storyboard",
            schema_version=1,
            input_digest=self.input_digest,
            content=self.valid_content,
            source_agent="codex",
            source_model_label="current-agent-model",
        )

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertTrue(first["data"]["created"])
        self.assertFalse(second["data"]["created"])
        self.assertEqual(
            first["data"]["artifact"]["artifact_id"],
            second["data"]["artifact"]["artifact_id"],
        )
        self.assertEqual(len(list_agent_artifacts(self.db, self.task_id)), 1)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM scene_cards WHERE task_id=?",
                (self.task_id,),
            ).fetchone()[0],
            2,
        )

    def test_content_and_action_allowlists_fail_closed(self):
        too_large = {"brief": {"broll": []}, "plan": {"note": "x" * 1_100_000}}
        large = self.gateway.submit_planning_artifact(
            task_id=self.task_id,
            artifact_type="digital_storyboard",
            schema_version=1,
            input_digest=self.input_digest,
            content=too_large,
            source_agent="codex",
            source_model_label=None,
        )
        self.assertEqual(large["error"]["code"], "AGENT_CONTENT_TOO_LARGE")

        out_of_scope = json.loads(json.dumps(self.valid_content, ensure_ascii=False))
        out_of_scope["plan"]["assetId"] = "not-in-task-scope"
        asset_result = self.gateway.submit_planning_artifact(
            task_id=self.task_id,
            artifact_type="digital_storyboard",
            schema_version=1,
            input_digest=self.input_digest,
            content=out_of_scope,
            source_agent="codex",
            source_model_label=None,
        )
        self.assertEqual(asset_result["error"]["code"], "AGENT_ASSET_OUT_OF_SCOPE")

        arbitrary = self.gateway.start_task_action(
            task_id=self.task_id,
            action="run_command",
        )
        self.assertEqual(arbitrary["error"]["code"], "AGENT_ACTION_NOT_ALLOWED")
        paid = self.gateway.start_task_action(
            task_id=self.task_id,
            action="recommend_storyboard",
            confirm_cost=False,
        )
        self.assertEqual(paid["error"]["code"], "PAID_CONFIRMATION_REQUIRED")

    def test_standalone_task_and_wrong_artifact_type_are_rejected(self):
        create_task(self.db, "standalone-task", "独立任务")
        standalone = self.gateway.get_agent_context("standalone-task")
        self.assertEqual(standalone["error"]["code"], "AGENT_MODE_REQUIRED")
        wrong = self.gateway.submit_planning_artifact(
            task_id=self.task_id,
            artifact_type="shell_command",
            schema_version=1,
            input_digest=self.input_digest,
            content={"command": "whoami"},
            source_agent="codex",
            source_model_label=None,
        )
        self.assertEqual(wrong["error"]["code"], "AGENT_ARTIFACT_TYPE_INVALID")


if __name__ == "__main__":
    unittest.main()
