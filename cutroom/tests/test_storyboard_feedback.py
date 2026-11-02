import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.scene_cards import add_scene_card, get_scene_card
from media_catalog.storyboard_feedback import (
    apply_feedback_candidate,
    record_feedback,
    rerank_scene_candidates,
)
from media_catalog.task_sources import set_task_sources
from media_catalog.tasks import create_task


class StoryboardFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.primary = self.root / "primary"
        self.other = self.root / "other"
        self.primary.mkdir()
        self.other.mkdir()
        register_library(
            self.db, "primary", "本次园区", "local", str(self.primary),
            purpose="task_specific",
        )
        register_library(
            self.db, "other", "其他项目", "local", str(self.other),
            purpose="task_specific",
        )
        create_task(self.db, "task-1", "数字人任务", "digital_human")
        self._asset("night-current", "primary", "夜景/夜间园区.mp4", "available")
        self._asset("day-factory", "primary", "白天/白天厂房.mp4", "available")
        self._asset("offline-factory", "primary", "白天/离线厂房.mp4", "offline")
        self._asset("outside-factory", "other", "白天/外部厂房.mp4", "available")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_assets(task_id,asset_id,role)
                VALUES('task-1','night-current','broll')
                """
            )
        add_scene_card(
            self.db,
            "task-1",
            1,
            narrative_text="这里展示园区厂房和产业空间",
            narrative_start=0,
            narrative_end=5,
            asset_id="night-current",
            mix_form="broll全屏",
            enter_tc=0,
            exit_tc=5,
            source_type="系统推荐",
            system_suggestion="园区夜景",
        )
        self.card_id = "task-1#1"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _asset(self, asset_id, library_id, relative_path, state):
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO assets(
                  asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                ) VALUES(?, 'video', 100, ?, datetime('now'))
                """,
                (asset_id, "quick-" + asset_id),
            )
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id,library_id,relative_path,mtime_ns,file_identity,state
                ) VALUES(?,?,?,?,?,?)
                """,
                (asset_id, library_id, relative_path, 1, asset_id, state),
            )

    def test_reject_records_reason_without_changing_scene_card(self):
        before = get_scene_card(self.db, self.card_id)

        feedback = record_feedback(
            self.db,
            "task-1",
            self.card_id,
            "rejected",
            "不要夜景",
        )

        self.assertEqual(feedback["decision"], "rejected")
        self.assertEqual(feedback["instruction"], "不要夜景")
        self.assertEqual(
            get_scene_card(self.db, self.card_id)["asset_id"],
            before["asset_id"],
        )

    def test_rerank_uses_only_online_assets_inside_frozen_task_scope(self):
        result = rerank_scene_candidates(
            self.db,
            "task-1",
            self.card_id,
            "改用白天厂房",
        )

        self.assertEqual(
            [item["assetId"] for item in result["candidates"]],
            ["day-factory"],
        )
        self.assertRegex(result["candidates"][0]["reason"], "纠正要求|旁白")
        self.assertEqual(result["candidates"][0]["sourceDirectory"], "本次园区")
        self.assertEqual(result["decision"], "reranked")

    def test_apply_candidate_uses_rough_cut_history_and_records_application(self):
        reranked = rerank_scene_candidates(
            self.db,
            "task-1",
            self.card_id,
            "改用白天厂房",
        )

        applied = apply_feedback_candidate(
            self.db,
            "task-1",
            self.card_id,
            reranked["feedbackId"],
            "day-factory",
        )

        self.assertEqual(get_scene_card(self.db, self.card_id)["asset_id"], "day-factory")
        self.assertEqual(applied["feedback"]["decision"], "applied")
        edit = self.db.connection.execute(
            "SELECT status FROM rough_cut_edits WHERE task_id='task-1'"
        ).fetchone()
        self.assertEqual(edit["status"], "applied")


if __name__ == "__main__":
    unittest.main()
