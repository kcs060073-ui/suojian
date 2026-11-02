import concurrent.futures
import io
import json
import sqlite3
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog import packaging_plan
from media_catalog.packaging_plan import add_item, confirm_plan, get_plan
from media_catalog.scene_cards import set_picture_lock
from media_catalog.tasks import create_task, set_status
from media_catalog.workflow import complete_stage, seed_stage_states


class PackagingCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.manifest_dir = self.root / "assets" / "manifests"
        self.manifest_dir.mkdir(parents=True)
        (self.root / "assets" / "pack").mkdir(parents=True)
        (self.root / "assets" / "fonts").mkdir(parents=True)
        audio = io.BytesIO()
        with wave.open(audio, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(b"\0\0" * 800)
        for name in ("bgm-a.mp3", "bgm-b.mp3"):
            (self.root / "assets" / "pack" / name).write_bytes(audio.getvalue())
        for name in ("brand.png", "sticker.png"):
            (self.root / "assets" / "pack" / name).write_bytes(name.encode())
        (self.root / "assets" / "pack" / "broken.mp3").write_bytes(b"placeholder")
        (self.root / "assets" / "fonts" / "sub.ttf").write_bytes(b"font")
        manifests = {
            "subtitle-presets.json": [
                {"id": "sub-a", "font": "sub.ttf", "license": "commercial-approved",
                 "default_for": ["数字人混剪"]},
                {"id": "sub-b", "font": "sub.ttf", "license": "commercial-approved",
                 "default_for": ["数字人混剪"]},
                {"id": "sub-shared", "font": "sub.ttf",
                 "license": "commercial-approved"},
            ],
            "bgm.json": [
                {"id": "bgm-a", "file": "assets/pack/bgm-a.mp3",
                 "license": "commercial-approved", "default_for": ["数字人混剪"]},
                {"id": "bgm-b", "file": "assets/pack/bgm-b.mp3",
                 "license": "commercial-approved", "default_for": ["数字人混剪"]},
                {"id": "bgm-park", "file": "assets/pack/bgm-b.mp3",
                 "license": "commercial-approved", "default_for": ["园区宣传"]},
                {"id": "bgm-unknown", "file": "assets/pack/bgm-b.mp3",
                 "license": "unknown", "default_for": ["数字人混剪"]},
                {"id": "bgm-missing", "file": "assets/pack/missing.mp3",
                 "license": "commercial-approved", "default_for": ["数字人混剪"]},
                {"id": "bgm-broken", "file": "assets/pack/broken.mp3",
                 "license": "commercial-approved", "default_for": ["数字人混剪"]},
                {"id": "bgm-shared", "file": "assets/pack/bgm-b.mp3",
                 "license": "commercial-approved"},
            ],
            "visual-cards.json": [
                {"id": "brand-a", "file": "assets/pack/brand.png", "kind": "watermark",
                 "license": "commercial-approved", "default_for": ["数字人混剪"]},
            ],
            "stickers.json": [
                {"id": "sticker-a", "file": "assets/pack/sticker.png",
                 "license": "commercial-approved", "default_for": ["数字人混剪"]},
            ],
            "voice-profiles.json": [],
            "sfx.json": [],
            "fancy-text-presets.json": [],
        }
        for filename, items in manifests.items():
            (self.manifest_dir / filename).write_text(
                json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8"
            )
        self.manifest_patch = patch.object(packaging_plan, "MANIFEST_DIR", self.manifest_dir)
        self.manifest_patch.start()
        self._create_task("task-1", lock=True)

    def tearDown(self):
        self.manifest_patch.stop()
        self.db.close()
        self.temp_dir.cleanup()

    def _create_task(self, task_id, *, lock=False, task_type="数字人混剪", subtype=None):
        brief = {"text": "文案"}
        if subtype is not None:
            brief["subtype"] = subtype
        create_task(self.db, task_id, task_id, task_type=task_type, brief=brief)
        set_status(self.db, task_id, "brief_locked")
        if lock:
            set_picture_lock(self.db, task_id)

    def _complete_draft(self, task_id="task-1"):
        plan_id = packaging_plan.create_blank_plan(self.db, task_id, "数字人混剪", "本片方案")
        packaging_plan.replace_item(self.db, plan_id, "subtitle", "sub-a")
        packaging_plan.replace_item(self.db, plan_id, "bgm", "bgm-a")
        return plan_id

    def test_task6_api_surface_exists(self):
        names = (
            "create_blank_plan", "clone_plan", "replace_item",
            "disable_optional_slot", "save_personal_template",
        )
        self.assertEqual([], [name for name in names if not callable(getattr(packaging_plan, name, None))])

    def test_recommendations_and_whole_plan_clone_remain_drafts(self):
        proposed = packaging_plan.propose(self.db, "task-1", "数字人混剪")
        self.assertTrue(proposed)
        self.assertTrue(all(get_plan(self.db, plan_id)["status"] == "draft" for plan_id in proposed))
        self.assertIsNone(
            self.db.connection.execute(
                "SELECT 1 FROM task_gate_events WHERE task_id='task-1' AND gate_type='packaging'"
            ).fetchone()
        )
        applied = packaging_plan.clone_plan(self.db, proposed[0], plan_name="采用推荐方案")
        self.assertEqual(get_plan(self.db, applied)["status"], "draft")
        self.assertEqual(
            [(i["slot"], i["asset_ref"]) for i in get_plan(self.db, proposed[0])["items"]],
            [(i["slot"], i["asset_ref"]) for i in get_plan(self.db, applied)["items"]],
        )
        self.assertEqual(get_plan(self.db, proposed[0])["origin_type"], "system_recommendation")
        for action in (
            lambda: packaging_plan.replace_item(self.db, proposed[0], "bgm", "bgm-b"),
            lambda: packaging_plan.disable_optional_slot(self.db, proposed[0], "bgm"),
        ):
            with self.assertRaisesRegex(RuntimeError, "PACKAGING_RECOMMENDATION_IMMUTABLE"):
                action()
        packaging_plan.replace_item(self.db, applied, "bgm", "bgm-b")
        self.assertEqual(get_plan(self.db, applied)["origin_type"], "custom")

    def test_blank_replace_and_disable_optional_slot(self):
        plan_id = packaging_plan.create_blank_plan(self.db, "task-1", "数字人混剪", "从空白开始")
        self.assertEqual(get_plan(self.db, plan_id)["items"], [])
        packaging_plan.replace_item(self.db, plan_id, "subtitle", "sub-a")
        packaging_plan.replace_item(self.db, plan_id, "bgm", "bgm-a")
        packaging_plan.replace_item(self.db, plan_id, "bgm", "bgm-b")
        self.assertEqual(
            [item["asset_ref"] for item in get_plan(self.db, plan_id)["items"] if item["slot"] == "bgm"],
            ["bgm-b"],
        )
        packaging_plan.disable_optional_slot(self.db, plan_id, "bgm")
        self.assertFalse(any(item["slot"] == "bgm" for item in get_plan(self.db, plan_id)["items"]))
        with self.assertRaisesRegex(ValueError, "required"):
            packaging_plan.disable_optional_slot(self.db, plan_id, "subtitle")

    def test_personal_template_is_task_independent_immutable_snapshot(self):
        source = self._complete_draft()
        template = packaging_plan.save_personal_template(self.db, source, "我的稳重方案")
        self.assertEqual(template["resource_kind"], "personal_packaging_template")
        self.assertEqual(template["status"], "template")
        self.assertIsNone(template.get("task_id"))
        self.assertEqual(
            [item["template_id"] for item in packaging_plan.list_personal_templates(
                self.db, "数字人混剪"
            )],
            [template["template_id"]],
        )
        template_id = template["template_id"]
        self._create_task("task-2")
        reused = packaging_plan.clone_plan(
            self.db, template_id, task_id="task-2", plan_name="任务2包装"
        )
        self.assertEqual(get_plan(self.db, reused)["task_id"], "task-2")
        self.assertEqual(get_plan(self.db, reused)["status"], "draft")
        self.db.connection.execute("DELETE FROM tasks WHERE task_id='task-1'")
        self.db.connection.commit()
        reused_after_delete = packaging_plan.clone_plan(
            self.db, template_id, task_id="task-2", plan_name="源任务删除后复用"
        )
        self.assertEqual(get_plan(self.db, reused_after_delete)["task_id"], "task-2")
        for action in (
            lambda: confirm_plan(self.db, template_id),
            lambda: packaging_plan.replace_item(
                self.db, template_id, "bgm", "bgm-b"
            ),
            lambda: packaging_plan.disable_optional_slot(self.db, template_id, "bgm"),
        ):
            with self.assertRaisesRegex(RuntimeError, "PACKAGING_TEMPLATE_IMMUTABLE"):
                action()

    def test_confirmed_plan_is_immutable_but_can_be_cloned(self):
        plan_id = self._complete_draft()
        confirm_plan(self.db, plan_id)
        for action in (
            lambda: packaging_plan.replace_item(self.db, plan_id, "bgm", "bgm-b"),
            lambda: packaging_plan.disable_optional_slot(self.db, plan_id, "bgm"),
            lambda: add_item(self.db, plan_id, "sticker", "sticker-a"),
        ):
            with self.assertRaisesRegex(RuntimeError, "PACKAGING_PLAN_CONFIRMED"):
                action()
        cloned = packaging_plan.clone_plan(self.db, plan_id, plan_name="修改版")
        self.assertEqual(get_plan(self.db, cloned)["status"], "draft")
        packaging_plan.replace_item(self.db, cloned, "bgm", "bgm-b")

    def test_confirmation_requires_picture_lock_and_required_slot(self):
        self._create_task("task-unlocked")
        unlocked = packaging_plan.create_blank_plan(self.db, "task-unlocked", "数字人混剪", "未锁画面")
        packaging_plan.replace_item(self.db, unlocked, "subtitle", "sub-a")
        with self.assertRaisesRegex(RuntimeError, "PICTURE_LOCK_MISSING"):
            confirm_plan(self.db, unlocked)
        blank = packaging_plan.create_blank_plan(self.db, "task-1", "数字人混剪", "缺字幕")
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_REQUIRED_SLOT_MISSING"):
            confirm_plan(self.db, blank)

    def test_confirmation_validates_manifest_license_file_and_route(self):
        for asset_ref, code in (
            ("not-in-manifest", "PACKAGING_MANIFEST_UNKNOWN"),
            ("bgm-unknown", "PACKAGING_LICENSE_BLOCKED"),
            ("bgm-missing", "PACKAGING_FILE_MISSING"),
            ("bgm-broken", "PACKAGING_FILE_UNREADABLE"),
            ("bgm-park", "PACKAGING_NOT_APPLICABLE"),
        ):
            with self.subTest(asset_ref=asset_ref):
                plan_id = packaging_plan.create_blank_plan(self.db, "task-1", "数字人混剪", asset_ref)
                packaging_plan.replace_item(self.db, plan_id, "subtitle", "sub-a")
                packaging_plan.replace_item(self.db, plan_id, "bgm", asset_ref)
                with self.assertRaisesRegex(RuntimeError, code):
                    confirm_plan(self.db, plan_id)

    def test_recommendations_survive_when_only_bgm_is_unreadable(self):
        bgm_path = self.manifest_dir / "bgm.json"
        payload = json.loads(bgm_path.read_text(encoding="utf-8"))
        payload["items"] = [
            item for item in payload["items"] if item["id"] == "bgm-broken"
        ]
        bgm_path.write_text(json.dumps(payload), encoding="utf-8")

        plan_ids = packaging_plan.propose(self.db, "task-1", "数字人混剪")

        self.assertTrue(plan_ids)
        self.assertTrue(all(
            not any(item["slot"] == "bgm" for item in get_plan(self.db, plan_id)["items"])
            for plan_id in plan_ids
        ))

    def test_confirmation_digest_binds_the_complete_combination(self):
        first = self._complete_draft()
        confirm_plan(self.db, first)
        first_digest = self.db.connection.execute(
            "SELECT gate_digest FROM task_gate_events WHERE task_id='task-1' "
            "AND gate_type='packaging' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        second = packaging_plan.clone_plan(self.db, first, plan_name="第二组合")
        packaging_plan.replace_item(self.db, second, "bgm", "bgm-b")
        confirm_plan(self.db, second)
        second_digest = self.db.connection.execute(
            "SELECT gate_digest FROM task_gate_events WHERE task_id='task-1' "
            "AND gate_type='packaging' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        self.assertNotEqual(first_digest, second_digest)

    def test_confirm_is_idempotent_and_records_one_gate_event(self):
        plan_id = self._complete_draft()
        first = confirm_plan(self.db, plan_id)
        second = confirm_plan(self.db, plan_id)
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_gate_events "
                "WHERE task_id='task-1' AND gate_type='packaging'"
            ).fetchone()[0],
            1,
        )

    def test_two_connections_concurrently_confirm_once(self):
        plan_id = self._complete_draft()
        database_path = self.db.path

        def worker():
            other = CatalogDatabase.open(database_path)
            try:
                return confirm_plan(other, plan_id)["status"]
            finally:
                other.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: worker(), range(2)))
        self.assertEqual(results, ["confirmed", "confirmed"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_gate_events "
                "WHERE task_id='task-1' AND gate_type='packaging'"
            ).fetchone()[0],
            1,
        )

    def test_confirmed_plan_cannot_be_changed_back_to_draft(self):
        plan_id = self._complete_draft()
        confirm_plan(self.db, plan_id)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "confirmed packaging plan"):
            self.db.connection.execute(
                "UPDATE packaging_plans SET status='draft' WHERE plan_id=?",
                (plan_id,),
            )
        self.db.connection.rollback()

    def test_confirmed_plan_items_reject_direct_insert_update_and_delete(self):
        plan_id = self._complete_draft()
        confirm_plan(self.db, plan_id)
        item_id = self.db.connection.execute(
            "SELECT item_id FROM packaging_plan_items "
            "WHERE plan_id=? AND slot='bgm'",
            (plan_id,),
        ).fetchone()[0]

        attacks = (
            (
                "INSERT INTO packaging_plan_items(plan_id,slot,asset_ref) "
                "VALUES(?, 'sticker', 'sticker-a')",
                (plan_id,),
            ),
            (
                "UPDATE packaging_plan_items SET asset_ref='bgm-b' "
                "WHERE item_id=?",
                (item_id,),
            ),
            (
                "DELETE FROM packaging_plan_items WHERE item_id=?",
                (item_id,),
            ),
        )
        for sql, params in attacks:
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "confirmed packaging item"
                ):
                    self.db.connection.execute(sql, params)
                self.db.connection.rollback()

        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM packaging_plan_items WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0],
            2,
        )

    def test_cross_plan_item_update_cannot_enter_or_leave_confirmed_plan(self):
        confirmed_id = self._complete_draft()
        confirm_plan(self.db, confirmed_id)
        draft_id = self._complete_draft()
        confirmed_item = self.db.connection.execute(
            "SELECT item_id FROM packaging_plan_items "
            "WHERE plan_id=? AND slot='bgm'",
            (confirmed_id,),
        ).fetchone()[0]
        draft_item = self.db.connection.execute(
            "SELECT item_id FROM packaging_plan_items "
            "WHERE plan_id=? AND slot='bgm'",
            (draft_id,),
        ).fetchone()[0]

        for item_id, target_plan_id in (
            (confirmed_item, draft_id),
            (draft_item, confirmed_id),
        ):
            with self.subTest(item_id=item_id, target_plan_id=target_plan_id):
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "confirmed packaging item"
                ):
                    self.db.connection.execute(
                        "UPDATE packaging_plan_items SET plan_id=? WHERE item_id=?",
                        (target_plan_id, item_id),
                    )
                self.db.connection.rollback()

    def test_task_delete_cascades_confirmed_plan_and_items_without_dirty_state(self):
        plan_id = self._complete_draft()
        confirm_plan(self.db, plan_id)

        self.db.connection.execute("DELETE FROM tasks WHERE task_id='task-1'")
        self.db.connection.commit()

        self.assertIsNone(
            self.db.connection.execute(
                "SELECT 1 FROM packaging_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM packaging_plan_items WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            list(self.db.connection.execute("PRAGMA foreign_key_check")),
            [],
        )
        self.assertFalse(self.db.connection.in_transaction)

    def test_canonical_route_is_derived_for_create_clone_and_confirm(self):
        with self.assertRaisesRegex(ValueError, "PACKAGING_ROUTE_MISMATCH"):
            packaging_plan.create_blank_plan(
                self.db, "task-1", "园区宣传", "注入错误路线"
            )
        self._create_task("task-park", task_type="园区宣传")
        shared = packaging_plan.create_blank_plan(
            self.db, "task-1", "数字人混剪", "跨路线共享"
        )
        packaging_plan.replace_item(self.db, shared, "subtitle", "sub-shared")
        packaging_plan.replace_item(self.db, shared, "bgm", "bgm-shared")
        cloned = packaging_plan.clone_plan(self.db, shared, task_id="task-park")
        self.assertEqual(get_plan(self.db, cloned)["route"], "园区宣传")
        incompatible = self._complete_draft()
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_NOT_APPLICABLE"):
            packaging_plan.clone_plan(self.db, incompatible, task_id="task-park")
        self.db.connection.execute(
            "UPDATE packaging_plans SET route='园区宣传' WHERE plan_id=?",
            (incompatible,),
        )
        self.db.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_ROUTE_MISMATCH"):
            confirm_plan(self.db, incompatible)

    def test_switching_packaging_gate_invalidates_only_after_new_confirmation(self):
        first = self._complete_draft()
        confirm_plan(self.db, first)
        seed_stage_states(self.db, "task-1")
        for stage in ("fine_render", "auto_qa", "full_review", "publish_package"):
            complete_stage(
                self.db, "task-1", stage, input_digest=f"{stage}-digest", details={}
            )
        second = packaging_plan.clone_plan(self.db, first, plan_name="替代方案")
        self.assertTrue(all(
            row["status"] == "completed"
            for row in self.db.connection.execute(
                "SELECT status FROM task_stage_states WHERE task_id='task-1' "
                "AND stage_key IN ('fine_render','auto_qa','full_review','publish_package')"
            )
        ))
        packaging_plan.replace_item(self.db, second, "bgm", "bgm-b")
        confirm_plan(self.db, second)
        self.assertEqual(
            {
                row["stage_key"]: row["status"]
                for row in self.db.connection.execute(
                    "SELECT stage_key,status FROM task_stage_states WHERE task_id='task-1'"
                )
                if row["stage_key"] in {
                    "fine_render", "auto_qa", "full_review", "publish_package"
                }
            },
            {
                "fine_render": "invalidated",
                "auto_qa": "invalidated",
                "full_review": "invalidated",
                "publish_package": "invalidated",
            },
        )

    def test_duplicate_backup_rank_and_empty_slot_fail_closed(self):
        plan_id = self._complete_draft()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO packaging_plan_items"
                "(plan_id,slot,asset_ref,is_backup,rank) VALUES(?,?,?,?,?)",
                (plan_id, "bgm", "bgm-a", 1, 1),
            )
            connection.execute(
                "INSERT INTO packaging_plan_items"
                "(plan_id,slot,asset_ref,is_backup,rank) VALUES(?,?,?,?,?)",
                (plan_id, "bgm", "bgm-b", 1, 1),
            )
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_ITEM_DUPLICATE"):
            confirm_plan(self.db, plan_id)
        empty_slot = self._complete_draft()
        self.db.connection.execute(
            "INSERT INTO packaging_plan_items(plan_id,slot,asset_ref) VALUES(?,?,?)",
            (empty_slot, "", "bgm-a"),
        )
        self.db.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_SLOT_INVALID"):
            confirm_plan(self.db, empty_slot)

    def test_recommendations_filter_unusable_or_wrong_route_items(self):
        bgm_path = self.manifest_dir / "bgm.json"
        payload = json.loads(bgm_path.read_text(encoding="utf-8"))
        payload["items"] = [
            {
                "id": "bad-license",
                "file": "assets/pack/bgm-a.mp3",
                "license": "restricted",
                "default_for": ["数字人混剪"],
            },
            {
                "id": "bad-file",
                "file": "assets/pack/not-there.mp3",
                "license": "commercial-approved",
                "default_for": ["数字人混剪"],
            },
            {
                "id": "wrong-route",
                "file": "assets/pack/bgm-a.mp3",
                "license": "commercial-approved",
                "default_for": ["园区宣传"],
            },
            payload["items"][0],
        ]
        bgm_path.write_text(json.dumps(payload), encoding="utf-8")
        plan_ids = packaging_plan.propose(self.db, "task-1", "数字人混剪")
        self.assertTrue(plan_ids)
        selected = {
            item["asset_ref"]
            for plan_id in plan_ids
            for item in get_plan(self.db, plan_id)["items"]
            if item["slot"] == "bgm"
        }
        self.assertEqual(selected, {"bgm-a"})


if __name__ == "__main__":
    unittest.main()
