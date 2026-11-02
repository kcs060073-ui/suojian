import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_catalog.database import CatalogDatabase


class RoughCutApiTests(unittest.TestCase):
    def test_bounded_rough_cut_api_exists(self):
        try:
            module = importlib.import_module("media_catalog.rough_cut")
        except ModuleNotFoundError as error:
            self.fail(f"rough-cut module is missing: {error}")

        for name in (
            "preview_edit_impact",
            "apply_edit",
            "undo_last_edit",
            "redo_last_edit",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(module, name, None)))


class RoughCutEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalog.sqlite"
        self.library_dirs = {}
        for library_id in ("primary", "selected", "default", "other"):
            directory = self.root / library_id
            directory.mkdir()
            self.library_dirs[library_id] = directory

        self.asset_files = {
            "digital": self.library_dirs["primary"] / "digital.mp4",
            "broll-a": self.library_dirs["primary"] / "broll-a.mp4",
            "photo-broll": self.library_dirs["primary"] / "photo-broll.jpg",
            "info": self.library_dirs["primary"] / "info.png",
            "selected-b": self.library_dirs["selected"] / "selected-b.mp4",
            "default-b": self.library_dirs["default"] / "default-b.mp4",
            "other-park": self.library_dirs["other"] / "other-park.mp4",
        }
        for asset_id, path in self.asset_files.items():
            path.write_bytes((f"source:{asset_id}:" * 3).encode("utf-8"))

        self.db = CatalogDatabase.open(self.db_path)
        self.db.migrate()
        self._seed_catalog()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _seed_catalog(self):
        with self.db.transaction() as conn:
            for library_id in ("primary", "selected", "default", "other"):
                purpose = "generic" if library_id in ("selected", "default") else "task_specific"
                conn.execute(
                    """
                    INSERT INTO libraries(
                      library_id,name,kind,mount_path,online,created_at,updated_at,
                      purpose,is_default_generic
                    ) VALUES(?,?,?,?,1,datetime('now'),datetime('now'),?,?)
                    """,
                    (
                        library_id,
                        library_id,
                        "local",
                        str(self.library_dirs[library_id]),
                        purpose,
                        int(library_id == "default"),
                    ),
                )
            for asset_id, path in self.asset_files.items():
                library_id = {
                    "digital": "primary",
                    "broll-a": "primary",
                    "photo-broll": "primary",
                    "info": "primary",
                    "selected-b": "selected",
                    "default-b": "default",
                    "other-park": "other",
                }[asset_id]
                kind = "image" if asset_id in ("info", "photo-broll") else "video"
                payload = path.read_bytes()
                conn.execute(
                    """
                    INSERT INTO assets(
                      asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                    ) VALUES(?,?,?,?,datetime('now'))
                    """,
                    (asset_id, kind, len(payload), f"fp-{asset_id}"),
                )
                conn.execute(
                    """
                    INSERT INTO asset_locations(
                      asset_id,library_id,relative_path,mtime_ns,state
                    ) VALUES(?,?,?,?, 'available')
                    """,
                    (asset_id, library_id, path.name, 1),
                )
            conn.execute(
                "INSERT INTO tasks(task_id,title,status,brief_json) VALUES('task','task','draft','{}')"
            )
            conn.executemany(
                """
                INSERT INTO task_source_scopes(
                  scope_id,task_id,library_id,role,relative_path,
                  source_mode,active,priority
                ) VALUES(?, 'task', ?, ?, '', ?, 1, ?)
                """,
                (
                    ("scope-primary", "primary", "primary", "primary", 0),
                    ("scope-selected", "selected", "supplementary", "selected_generic", 1000),
                    ("scope-default", "default", "supplementary", "default_generic", 2000),
                ),
            )
            conn.execute(
                "INSERT INTO task_source_settings(task_id,include_default_generic) VALUES('task',1)"
            )
            conn.executemany(
                "INSERT INTO task_assets(task_id,asset_id,role) VALUES('task',?,?)",
                (
                    ("digital", "digital_human"),
                    ("broll-a", "broll"),
                    ("photo-broll", "broll"),
                    ("info", "information_card"),
                ),
            )
            conn.executemany(
                "INSERT INTO segments(segment_id,asset_id,start_sec,end_sec,label) VALUES(?,?,?,?,?)",
                (
                    ("seg-a", "broll-a", 0.0, 10.0, "primary"),
                    ("seg-selected", "selected-b", 1.0, 9.0, "selected"),
                    ("seg-default", "default-b", 2.0, 10.0, "default"),
                    ("seg-other", "other-park", 0.0, 8.0, "other"),
                ),
            )
            conn.executemany(
                """
                INSERT INTO scene_cards(
                  card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                  asset_id,segment_id,mix_form,enter_tc,exit_tc,
                  audio_policy_draft,source_type,confirm_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    (
                        "opaque-digital", "task", 0, "continuous narration", 0.0, 4.0,
                        "digital", None, "数字人全屏", 0.0, 0.0, "keep", "用户指定",
                    ),
                    (
                        "opaque-broll", "task", 1, "continuous narration", 4.0, 8.0,
                        "broll-a", "seg-a", "broll全屏", 0.0, 0.0, "mute", "系统推荐",
                    ),
                    (
                        "opaque-info", "task", 2, "continuous narration", 8.0, 10.0,
                        "info", None, "信息卡", 0.0, 0.0, "mute", "系统推荐",
                    ),
                ),
            )

    def _cards(self):
        return [
            dict(row)
            for row in self.db.connection.execute(
                "SELECT * FROM scene_cards WHERE task_id='task' ORDER BY idx,card_id"
            ).fetchall()
        ]

    def _source_bytes(self):
        return {asset_id: path.read_bytes() for asset_id, path in self.asset_files.items()}

    def _lock_with_completed_stages(self):
        from media_catalog.workflow import WORKFLOW_STAGES, seed_stage_states

        seed_stage_states(self.db, "task")
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO picture_lock(
                  task_id,lock_version,changed_since_lock,selection_digest
                ) VALUES('task','lock-v1',0,'locked-selection')
                """
            )
            conn.executemany(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task' AND stage_key=?",
                ((stage,) for stage in WORKFLOW_STAGES),
            )

    def _reopen(self):
        self.db.close()
        self.db = CatalogDatabase.open(self.db_path)
        self.db.migrate()

    def test_replace_changes_only_current_card_and_accepts_bound_source_types(self):
        from media_catalog.rough_cut import apply_edit, undo_last_edit

        for asset_id, segment_id in (
            ("selected-b", "seg-selected"),
            ("default-b", "seg-default"),
        ):
            with self.subTest(asset_id=asset_id):
                before = {card["card_id"]: card for card in self._cards()}
                result = apply_edit(
                    self.db,
                    "task",
                    {
                        "action": "replace",
                        "card_id": "opaque-broll",
                        "asset_id": asset_id,
                        "segment_id": segment_id,
                    },
                )
                after = {card["card_id"]: card for card in self._cards()}
                self.assertEqual(after["opaque-broll"]["asset_id"], asset_id)
                self.assertEqual(after["opaque-broll"]["segment_id"], segment_id)
                segment_start = 1.0 if asset_id == "selected-b" else 2.0
                self.assertEqual(
                    (after["opaque-broll"]["enter_tc"], after["opaque-broll"]["exit_tc"]),
                    (segment_start, segment_start + 4.0),
                )
                self.assertEqual(after["opaque-digital"], before["opaque-digital"])
                self.assertEqual(after["opaque-info"], before["opaque-info"])
                self.assertEqual(result["status"], "applied")
                self.assertEqual(
                    self.db.connection.execute(
                        "SELECT status FROM rough_cut_edits WHERE edit_id=?",
                        (result["edit_id"],),
                    ).fetchone()[0],
                    "applied",
                )
                undo_last_edit(self.db, "task")

    def test_replace_rejects_other_park_asset_without_mutation_or_log(self):
        from media_catalog.rough_cut import apply_edit

        before = self._cards()
        with self.assertRaisesRegex(ValueError, "允许.*素材范围|素材范围"):
            apply_edit(
                self.db,
                "task",
                {
                    "action": "replace",
                    "card_id": "opaque-broll",
                    "asset_id": "other-park",
                    "segment_id": "seg-other",
                },
            )
        self.assertEqual(self._cards(), before)
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM rough_cut_edits").fetchone()[0],
            0,
        )

    def test_reorder_requires_every_current_card_exactly_once(self):
        from media_catalog.rough_cut import apply_edit

        valid = ["opaque-digital", "opaque-info", "opaque-broll"]
        result = apply_edit(
            self.db, "task", {"action": "reorder", "card_ids": valid}
        )
        self.assertEqual(
            [card["card_id"] for card in self._cards()],
            ["opaque-digital", "opaque-broll", "opaque-info"],
        )
        self.assertEqual(result["status"], "applied")

        for invalid in (
            valid[:-1],
            ["opaque-info", "opaque-info", "opaque-broll"],
            ["opaque-info", "opaque-digital", "foreign-card"],
            ["opaque-digital", ["opaque-info"], "opaque-broll"],
            ["opaque-digital", 7, "opaque-broll"],
        ):
            with self.subTest(card_ids=invalid):
                before = self._cards()
                with self.assertRaisesRegex(ValueError, "恰好.*一次|当前.*卡片"):
                    apply_edit(
                        self.db,
                        "task",
                        {"action": "reorder", "card_ids": invalid},
                    )
                self.assertEqual(self._cards(), before)

    def test_reorder_keeps_narrative_slots_and_anchor_but_moves_visual_payload(self):
        from media_catalog.rough_cut import apply_edit

        before = {card["card_id"]: card for card in self._cards()}
        apply_edit(
            self.db,
            "task",
            {
                "action": "reorder",
                "card_ids": ["opaque-digital", "opaque-info", "opaque-broll"],
            },
        )
        after = {card["card_id"]: card for card in self._cards()}
        for card_id in before:
            self.assertEqual(
                (
                    after[card_id]["narrative_text"],
                    after[card_id]["narrative_start"],
                    after[card_id]["narrative_end"],
                ),
                (
                    before[card_id]["narrative_text"],
                    before[card_id]["narrative_start"],
                    before[card_id]["narrative_end"],
                ),
            )
        self.assertEqual(after["opaque-digital"]["asset_id"], "digital")
        self.assertEqual(after["opaque-digital"]["mix_form"], "数字人全屏")
        self.assertEqual(after["opaque-broll"]["asset_id"], "info")
        self.assertEqual(after["opaque-broll"]["mix_form"], "信息卡")
        self.assertEqual(after["opaque-info"]["asset_id"], "broll-a")
        self.assertEqual(after["opaque-info"]["mix_form"], "broll全屏")

        with self.assertRaisesRegex(ValueError, "数字人|锚点|固定"):
            apply_edit(
                self.db,
                "task",
                {
                    "action": "reorder",
                    "card_ids": ["opaque-info", "opaque-broll", "opaque-digital"],
                },
            )

    def test_trim_stays_inside_source_segment_and_covers_scene_duration(self):
        from media_catalog.rough_cut import apply_edit

        apply_edit(
            self.db,
            "task",
            {
                "action": "trim",
                "card_id": "opaque-broll",
                "enter_tc": 2.0,
                "exit_tc": 7.0,
            },
        )
        card = next(card for card in self._cards() if card["card_id"] == "opaque-broll")
        self.assertEqual((card["enter_tc"], card["exit_tc"]), (2.0, 7.0))

        for enter_tc, exit_tc in ((-0.1, 5.0), (2.0, 10.1), (5.0, 5.0), (2.0, 5.0)):
            with self.subTest(enter_tc=enter_tc, exit_tc=exit_tc):
                before = self._cards()
                with self.assertRaisesRegex(ValueError, "裁剪|边界|时长"):
                    apply_edit(
                        self.db,
                        "task",
                        {
                            "action": "trim",
                            "card_id": "opaque-broll",
                            "enter_tc": enter_tc,
                            "exit_tc": exit_tc,
                        },
                    )
                self.assertEqual(self._cards(), before)

    def test_delete_allows_only_fullscreen_broll_or_information_card(self):
        from media_catalog.rough_cut import apply_edit, undo_last_edit

        for card_id in ("opaque-broll", "opaque-info"):
            with self.subTest(card_id=card_id):
                apply_edit(
                    self.db, "task", {"action": "delete", "card_id": card_id}
                )
                self.assertNotIn(card_id, {card["card_id"] for card in self._cards()})
                undo_last_edit(self.db, "task")

        for card_id, mix_form in (("opaque-digital", None), ("opaque-broll", "画中画")):
            with self.subTest(card_id=card_id, mix_form=mix_form):
                if mix_form:
                    self.db.connection.execute(
                        "UPDATE scene_cards SET mix_form=? WHERE card_id=?",
                        (mix_form, card_id),
                    )
                    self.db.connection.commit()
                before = self._cards()
                with self.assertRaisesRegex(ValueError, "只能删除|口播主轴"):
                    apply_edit(
                        self.db, "task", {"action": "delete", "card_id": card_id}
                    )
                self.assertEqual(self._cards(), before)

    def test_split_visual_segment_is_continuous_and_preserves_narration_source(self):
        from media_catalog.rough_cut import apply_edit

        original = next(card for card in self._cards() if card["card_id"] == "opaque-broll")
        result = apply_edit(
            self.db,
            "task",
            {"action": "split", "card_id": "opaque-broll", "playhead": 6.0},
        )
        cards = self._cards()
        halves = [
            card for card in cards
            if card["narrative_start"] >= 4.0 and card["narrative_end"] <= 8.0
        ]
        self.assertEqual(len(halves), 2)
        self.assertNotIn("opaque-broll", {card["card_id"] for card in cards})
        self.assertEqual(len({card["card_id"] for card in halves}), 2)
        self.assertTrue(all(not card["card_id"].startswith("task#") for card in halves))
        self.assertEqual(
            [(card["narrative_start"], card["narrative_end"]) for card in halves],
            [(4.0, 6.0), (6.0, 8.0)],
        )
        self.assertEqual(
            sum(card["narrative_end"] - card["narrative_start"] for card in halves),
            original["narrative_end"] - original["narrative_start"],
        )
        self.assertEqual(
            [(card["enter_tc"], card["exit_tc"]) for card in halves],
            [(0.0, 2.0), (2.0, 4.0)],
        )
        self.assertEqual(halves[0]["exit_tc"], halves[1]["enter_tc"])
        self.assertEqual(
            sum(card["exit_tc"] - card["enter_tc"] for card in halves),
            4.0,
        )
        for card in halves:
            self.assertEqual(card["narrative_text"], original["narrative_text"])
            self.assertEqual(card["asset_id"], original["asset_id"])
            self.assertEqual(card["segment_id"], original["segment_id"])
        self.assertEqual(result["status"], "applied")

    def test_split_rejects_endpoints_and_out_of_range(self):
        from media_catalog.rough_cut import apply_edit

        for playhead in (4.0, 8.0, 3.9, 8.1):
            with self.subTest(playhead=playhead):
                before = self._cards()
                with self.assertRaisesRegex(ValueError, "播放头.*内部|端点|范围"):
                    apply_edit(
                        self.db,
                        "task",
                        {
                            "action": "split",
                            "card_id": "opaque-broll",
                            "playhead": playhead,
                        },
                    )
                self.assertEqual(self._cards(), before)

    def test_split_rejects_digital_human_anchor_without_mutation(self):
        from media_catalog.rough_cut import apply_edit

        before_asset = self.asset_files["digital"].read_bytes()
        before = self._cards()
        with self.assertRaisesRegex(ValueError, "数字人|口播主轴|不可拆"):
            apply_edit(
                self.db,
                "task",
                {"action": "split", "card_id": "opaque-digital", "playhead": 2.0},
            )
        self.assertEqual(self._cards(), before)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_assets WHERE task_id='task' AND asset_id='digital' AND role='digital_human'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(self.asset_files["digital"].read_bytes(), before_asset)

    def test_replace_rejects_digital_anchor_and_incompatible_media_roles(self):
        from media_catalog.rough_cut import apply_edit

        attempts = (
            {
                "action": "replace", "card_id": "opaque-digital",
                "asset_id": "selected-b", "segment_id": "seg-selected",
            },
            {
                "action": "replace", "card_id": "opaque-broll",
                "asset_id": "digital", "mix_form": "画中画",
            },
            {
                "action": "replace", "card_id": "opaque-broll",
                "asset_id": "info", "mix_form": "broll全屏",
            },
            {
                "action": "replace", "card_id": "opaque-info",
                "asset_id": "selected-b", "segment_id": "seg-selected",
                "mix_form": "信息卡",
            },
        )
        for edit in attempts:
            with self.subTest(edit=edit):
                before = self._cards()
                with self.assertRaisesRegex(ValueError, "数字人|媒体|角色|混剪形态"):
                    apply_edit(self.db, "task", edit)
                self.assertEqual(self._cards(), before)

    def test_fullscreen_broll_accepts_task_scoped_photo_with_broll_role(self):
        from media_catalog.rough_cut import apply_edit

        apply_edit(
            self.db,
            "task",
            {
                "action": "replace",
                "card_id": "opaque-broll",
                "asset_id": "photo-broll",
                "mix_form": "broll全屏",
            },
        )

        card = next(item for item in self._cards() if item["card_id"] == "opaque-broll")
        self.assertEqual(card["asset_id"], "photo-broll")
        self.assertEqual(card["mix_form"], "broll全屏")
        self.assertEqual((card["enter_tc"], card["exit_tc"]), (0.0, 0.0))

    def test_identical_replace_is_rejected_instead_of_logging_false_change(self):
        from media_catalog.rough_cut import apply_edit

        edit = {
            "action": "replace", "card_id": "opaque-broll",
            "asset_id": "selected-b", "segment_id": "seg-selected",
        }
        apply_edit(self.db, "task", edit)
        count = self.db.connection.execute("SELECT COUNT(*) FROM rough_cut_edits").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "没有产生变化"):
            apply_edit(self.db, "task", edit)
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM rough_cut_edits").fetchone()[0],
            count,
        )

    def test_invalid_narrative_segment_and_source_times_fail_closed(self):
        from media_catalog.rough_cut import apply_edit

        cases = (
            (
                "UPDATE scene_cards SET narrative_end=? WHERE card_id='opaque-broll'",
                (float("inf"),),
                {"action": "reorder", "card_ids": ["opaque-digital", "opaque-info", "opaque-broll"]},
            ),
            (
                "UPDATE segments SET start_sec=? WHERE segment_id='seg-selected'",
                (float("inf"),),
                {
                    "action": "replace", "card_id": "opaque-broll",
                    "asset_id": "selected-b", "segment_id": "seg-selected",
                },
            ),
            (
                "UPDATE scene_cards SET enter_tc=?,exit_tc=4 WHERE card_id='opaque-broll'",
                (float("inf"),),
                {"action": "split", "card_id": "opaque-broll", "playhead": 6.0},
            ),
        )
        for sql, params, edit in cases:
            with self.subTest(sql=sql):
                self.db.connection.execute(sql, params)
                self.db.connection.commit()
                with self.assertRaisesRegex(ValueError, "有限|时间|边界|顺序"):
                    apply_edit(self.db, "task", edit)
                self.tearDown()
                self.setUp()

    def test_undo_then_new_edit_truncates_old_redo_branch_but_keeps_audit(self):
        from media_catalog.rough_cut import apply_edit, redo_last_edit, undo_last_edit

        first = apply_edit(
            self.db, "task", {"action": "trim", "card_id": "opaque-broll", "enter_tc": 1, "exit_tc": 5}
        )
        second = apply_edit(
            self.db, "task", {"action": "trim", "card_id": "opaque-broll", "enter_tc": 2, "exit_tc": 6}
        )
        undo_last_edit(self.db, "task")
        third = apply_edit(
            self.db, "task", {"action": "trim", "card_id": "opaque-broll", "enter_tc": 3, "exit_tc": 7}
        )

        statuses = dict(self.db.connection.execute("SELECT edit_id,status FROM rough_cut_edits"))
        self.assertEqual(statuses[first["edit_id"]], "applied")
        self.assertEqual(statuses[second["edit_id"]], "pending")
        self.assertEqual(statuses[third["edit_id"]], "applied")
        undo_last_edit(self.db, "task")
        redone = redo_last_edit(self.db, "task")
        self.assertEqual(redone["edit_id"], third["edit_id"])

    def test_multiple_undo_redo_steps_follow_the_current_branch(self):
        from media_catalog.rough_cut import apply_edit, redo_last_edit, undo_last_edit

        original = self._cards()
        first = apply_edit(
            self.db, "task", {"action": "trim", "card_id": "opaque-broll", "enter_tc": 1, "exit_tc": 5}
        )
        after_first = self._cards()
        second = apply_edit(
            self.db, "task", {"action": "trim", "card_id": "opaque-broll", "enter_tc": 2, "exit_tc": 6}
        )
        after_second = self._cards()

        self.assertEqual(undo_last_edit(self.db, "task")["edit_id"], second["edit_id"])
        self.assertEqual(self._cards(), after_first)
        self.assertEqual(undo_last_edit(self.db, "task")["edit_id"], first["edit_id"])
        self.assertEqual(self._cards(), original)
        self.assertEqual(redo_last_edit(self.db, "task")["edit_id"], first["edit_id"])
        self.assertEqual(self._cards(), after_first)
        self.assertEqual(redo_last_edit(self.db, "task")["edit_id"], second["edit_id"])
        self.assertEqual(self._cards(), after_second)

    def test_locked_edit_requires_matching_unexpired_impact_token(self):
        from media_catalog.rough_cut import apply_edit, preview_edit_impact

        self._lock_with_completed_stages()
        edit = {"action": "delete", "card_id": "opaque-info"}
        with self.assertRaisesRegex(RuntimeError, "IMPACT_PREVIEW_REQUIRED|先预览"):
            apply_edit(self.db, "task", edit)

        preview = preview_edit_impact(self.db, "task", edit)
        self.assertTrue(preview["locked"])
        self.assertTrue(preview["impact_token"])
        self.assertIn("picture_order", preview["invalidated_stages"])

        with self.assertRaisesRegex(ValueError, "不匹配|过期"):
            apply_edit(
                self.db,
                "task",
                {"action": "delete", "card_id": "opaque-broll"},
                impact_token=preview["impact_token"],
            )
        with patch("media_catalog.rough_cut.time.time", return_value=preview["expires_at"] + 1):
            with self.assertRaisesRegex(ValueError, "过期"):
                apply_edit(
                    self.db,
                    "task",
                    edit,
                    impact_token=preview["impact_token"],
                )

        fresh = preview_edit_impact(self.db, "task", edit)
        result = apply_edit(
            self.db,
            "task",
            edit,
            impact_token=fresh["impact_token"],
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            self.db.connection.execute(
                "SELECT changed_since_lock FROM picture_lock WHERE task_id='task'"
            ).fetchone()[0],
            1,
        )
        statuses = dict(
            self.db.connection.execute(
                "SELECT stage_key,status FROM task_stage_states WHERE task_id='task'"
            ).fetchall()
        )
        self.assertEqual(statuses["rough_cut"], "completed")
        for stage in (
            "picture_order", "picture_lock", "packaging_confirm", "fine_render",
            "auto_qa", "full_review", "publish_package",
        ):
            self.assertEqual(statuses[stage], "invalidated")

    def test_locked_token_is_stale_after_scene_change(self):
        from media_catalog.rough_cut import apply_edit, preview_edit_impact

        self._lock_with_completed_stages()
        edit = {"action": "delete", "card_id": "opaque-info"}
        preview = preview_edit_impact(self.db, "task", edit)
        self.db.connection.execute(
            "UPDATE scene_cards SET narrative_text='changed elsewhere' WHERE card_id='opaque-info'"
        )
        self.db.connection.commit()
        with self.assertRaisesRegex(ValueError, "不匹配|过期"):
            apply_edit(
                self.db,
                "task",
                edit,
                impact_token=preview["impact_token"],
            )

    def test_log_failure_rolls_back_edit_lock_and_stage_invalidation(self):
        from media_catalog.rough_cut import apply_edit, preview_edit_impact

        self._lock_with_completed_stages()
        edit = {"action": "delete", "card_id": "opaque-info"}
        preview = preview_edit_impact(self.db, "task", edit)
        before = self._cards()
        self.db.connection.execute(
            """
            CREATE TRIGGER reject_rough_cut_log
            BEFORE INSERT ON rough_cut_edits
            BEGIN
              SELECT RAISE(ABORT, 'forced rough-cut log failure');
            END
            """
        )
        self.db.connection.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced rough-cut log failure"):
            apply_edit(
                self.db,
                "task",
                edit,
                impact_token=preview["impact_token"],
            )

        self.assertEqual(self._cards(), before)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT changed_since_lock FROM picture_lock WHERE task_id='task'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            {row[0] for row in self.db.connection.execute(
                "SELECT status FROM task_stage_states WHERE task_id='task'"
            )},
            {"completed"},
        )
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM rough_cut_edits").fetchone()[0],
            0,
        )

    def test_undo_redo_survive_database_restart_and_never_touch_sources(self):
        from media_catalog.rough_cut import apply_edit, redo_last_edit, undo_last_edit

        sources_before = self._source_bytes()
        original = self._cards()
        applied = apply_edit(
            self.db,
            "task",
            {"action": "split", "card_id": "opaque-broll", "playhead": 6.0},
        )
        split_cards = self._cards()
        log = self.db.connection.execute(
            "SELECT before_json,after_json,status FROM rough_cut_edits WHERE edit_id=?",
            (applied["edit_id"],),
        ).fetchone()
        self.assertEqual(log["status"], "applied")
        self.assertEqual(json.loads(log["before_json"])["cards"], original)
        self.assertEqual(json.loads(log["after_json"])["cards"], split_cards)

        self._reopen()
        undone = undo_last_edit(self.db, "task")
        self.assertEqual(undone["status"], "reverted")
        self.assertEqual(self._cards(), original)

        self._reopen()
        redone = redo_last_edit(self.db, "task")
        self.assertEqual(redone["status"], "applied")
        self.assertEqual(self._cards(), split_cards)
        self.assertEqual(self._source_bytes(), sources_before)


if __name__ == "__main__":
    unittest.main()
