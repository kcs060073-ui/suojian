import copy
import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.task_sources import set_task_sources
from media_catalog.tasks import add_asset, create_task, set_status


def _finalize_module():
    try:
        return importlib.import_module("media_catalog.planning_finalize")
    except ModuleNotFoundError:
        raise AssertionError("media_catalog.planning_finalize must exist") from None


class DigitalPlanningFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.library = self.root / "materials"
        self.library.mkdir()
        register_library(
            self.db,
            "primary",
            "本次任务素材",
            "local",
            str(self.library),
            purpose="task_specific",
        )
        self._insert_asset("asset-digital", "video", "source.mp4")
        self._insert_asset("asset-bound", "image", "园区大门.png")
        self.script = "锁定原文"
        self.words = [
            {"w": "锁定", "start": 0.0, "end": 0.4},
            {"w": "原文", "start": 0.4, "end": 0.9},
        ]
        self.content = {
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
                """
                INSERT INTO assets(
                  asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                ) VALUES(?,?,1,?,datetime('now'))
                """,
                (asset_id, media_kind, "fp-" + asset_id),
            )
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id,library_id,relative_path,mtime_ns,file_identity
                ) VALUES(?,'primary',?,1,?)
                """,
                (asset_id, relative_path, asset_id),
            )

    def _create_ready_task(self, task_id, orchestration_mode):
        script_digest = hashlib.sha256(self.script.encode("utf-8")).hexdigest()
        task_root = (
            self.root / "runtime" / "workbench" / "tasks" / task_id
        )
        inputs = task_root / "inputs"
        artifacts = task_root / "artifacts"
        inputs.mkdir(parents=True)
        artifacts.mkdir()
        (inputs / "final-script.txt").write_text(self.script, encoding="utf-8")
        (artifacts / "transcript.words.json").write_text(
            json.dumps(self.words, ensure_ascii=False),
            encoding="utf-8",
        )
        create_task(
            self.db,
            task_id,
            "数字人任务",
            task_type="digital_human",
            brief={
                "subtype": "zhaoshang",
                "final_script": {
                    "relative_path": "inputs/final-script.txt",
                    "sha256": script_digest,
                },
            },
            orchestration_mode=orchestration_mode,
        )
        set_task_sources(
            self.db,
            task_id,
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        add_asset(self.db, task_id, "asset-digital", "digital_human")
        set_status(self.db, task_id, "brief_locked")
        binding = {
            "task_id": task_id,
            "script_sha256": script_digest,
            "source_snapshot_ids": [
                row[0]
                for row in self.db.connection.execute(
                    "SELECT snapshot_id FROM task_source_snapshots "
                    "WHERE task_id=? ORDER BY snapshot_id",
                    (task_id,),
                )
            ],
        }
        input_digest = hashlib.sha256(
            json.dumps(
                binding,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return task_root, binding, input_digest

    def _card_projection(self, task_id):
        return [
            tuple(row)
            for row in self.db.connection.execute(
                """
                SELECT idx,narrative_text,narrative_start,narrative_end,
                       asset_id,mix_form,audio_policy_draft,source_type,
                       system_suggestion,confirm_status
                FROM scene_cards WHERE task_id=? ORDER BY idx,card_id
                """,
                (task_id,),
            )
        ]

    def test_agent_and_standalone_inputs_produce_same_scene_cards(self):
        planning = _finalize_module()
        agent_root, agent_binding, agent_digest = self._create_ready_task(
            "agent-task",
            "agent",
        )
        standalone_root, standalone_binding, standalone_digest = (
            self._create_ready_task("standalone-task", "standalone")
        )

        agent_result = planning.finalize_digital_storyboard(
            self.db,
            task_id="agent-task",
            project_root=self.root,
            content=self.content,
            input_binding=agent_binding,
            input_digest=agent_digest,
            source="agent_artifact",
        )
        standalone_result = planning.finalize_digital_storyboard(
            self.db,
            task_id="standalone-task",
            project_root=self.root,
            content=self.content,
            input_binding=standalone_binding,
            input_digest=standalone_digest,
            source="standalone_model",
        )

        self.assertEqual(agent_result["scene_card_count"], 2)
        self.assertEqual(standalone_result["scene_card_count"], 2)
        self.assertEqual(
            self._card_projection("agent-task"),
            self._card_projection("standalone-task"),
        )
        metadata = json.loads(
            (
                agent_root
                / "artifacts"
                / "storyboard"
                / "recommendation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["source"], "agent_artifact")
        self.assertEqual(agent_result["source"], "agent_artifact")
        self.assertTrue((agent_root / "artifacts" / "storyboard" / "brief.json").is_file())
        self.assertTrue((standalone_root / "artifacts" / "storyboard" / "plan.json").is_file())

    def test_invalid_anchor_writes_neither_files_cards_nor_stage(self):
        planning = _finalize_module()
        task_root, binding, input_digest = self._create_ready_task(
            "agent-task",
            "agent",
        )
        content = copy.deepcopy(self.content)
        content["brief"]["broll"][0]["anchorText"] = "不在锁定文案中的文字"

        with self.assertRaisesRegex(RuntimeError, "STORYBOARD_OUTPUT_INVALID"):
            planning.finalize_digital_storyboard(
                self.db,
                task_id="agent-task",
                project_root=self.root,
                content=content,
                input_binding=binding,
                input_digest=input_digest,
                source="agent_artifact",
            )

        storyboard = task_root / "artifacts" / "storyboard"
        self.assertFalse((storyboard / "brief.json").exists())
        self.assertFalse((storyboard / "plan.json").exists())
        self.assertEqual(self._card_projection("agent-task"), [])
        stage = self.db.connection.execute(
            "SELECT status FROM task_stage_states "
            "WHERE task_id='agent-task' AND stage_key='ai_storyboard'"
        ).fetchone()
        self.assertTrue(stage is None or stage["status"] != "completed")

    def test_unknown_source_is_rejected_before_writes(self):
        planning = _finalize_module()
        task_root, binding, input_digest = self._create_ready_task(
            "agent-task",
            "agent",
        )
        with self.assertRaisesRegex(ValueError, "STORYBOARD_SOURCE_INVALID"):
            planning.finalize_digital_storyboard(
                self.db,
                task_id="agent-task",
                project_root=self.root,
                content=self.content,
                input_binding=binding,
                input_digest=input_digest,
                source="automatic_fallback",
            )
        self.assertFalse((task_root / "artifacts" / "storyboard").exists())


if __name__ == "__main__":
    unittest.main()
