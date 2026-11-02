import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_catalog import task_sources as task_sources_module
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.scanner import LibraryOffline, scan_library
from media_catalog.task_sources import (
    get_task_sources,
    preview_source_sync,
    set_task_sources,
    source_health,
    sync_task_sources,
)
from media_catalog.tasks import add_asset, create_task, list_task_assets
from media_catalog.workflow import seed_stage_states


class TaskSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = {}
        for library_id in (
            "primary",
            "primary-2",
            "selected-a",
            "selected-b",
            "default-a",
            "default-b",
            "generic-other",
            "other-park",
            "packaging",
        ):
            path = self.root / library_id
            path.mkdir()
            self.paths[library_id] = path

        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        register_library(
            self.db,
            "primary",
            "任务主素材",
            "local",
            str(self.paths["primary"]),
            purpose="task_specific",
        )
        register_library(
            self.db,
            "primary-2",
            "替换主素材",
            "local",
            str(self.paths["primary-2"]),
            purpose="task_specific",
        )
        for library_id in (
            "selected-a",
            "selected-b",
            "generic-other",
            "other-park",
        ):
            register_library(
                self.db,
                library_id,
                library_id,
                "local",
                str(self.paths[library_id]),
                purpose="generic",
            )
        for library_id in ("default-a", "default-b"):
            register_library(
                self.db,
                library_id,
                library_id,
                "local",
                str(self.paths[library_id]),
                purpose="generic",
                is_default_generic=True,
            )
        register_library(
            self.db,
            "packaging",
            "包装素材",
            "local",
            str(self.paths["packaging"]),
            purpose="packaging",
        )
        create_task(self.db, "task-1", "视频任务")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _set_default_scope(self):
        return set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-b", "default-a", "selected-a"],
            include_default_generic=True,
        )

    def _insert_asset(self, asset_id, library_id, relative_path):
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO assets(
                  asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                ) VALUES(?, 'video', 1, ?, datetime('now'))
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

    def _confirmed_sync(self):
        preview = preview_source_sync(self.db, "task-1")
        return sync_task_sources(
            self.db,
            "task-1",
            impact_token=preview["impact_token"],
        )

    def _configuration_state(self):
        queries = {
            "settings": (
                "SELECT * FROM task_source_settings WHERE task_id='task-1' "
                "ORDER BY task_id"
            ),
            "scopes": (
                "SELECT * FROM task_source_scopes WHERE task_id='task-1' "
                "ORDER BY scope_id"
            ),
            "snapshots": (
                "SELECT * FROM task_source_snapshots WHERE task_id='task-1' "
                "ORDER BY snapshot_id"
            ),
            "lock": "SELECT * FROM picture_lock WHERE task_id='task-1'",
            "workflow": (
                "SELECT * FROM task_stage_states WHERE task_id='task-1' "
                "ORDER BY stage_key"
            ),
        }
        return {
            name: [tuple(row) for row in self.db.connection.execute(sql)]
            for name, sql in queries.items()
        }

    def test_first_binding_rejects_preexisting_out_of_scope_references_atomically(self):
        self._insert_asset("unbound", "other-park", "secret.mp4")
        self._insert_asset("packaged", "packaging", "logo.png")
        add_asset(self.db, "task-1", "unbound", "legacy")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
                VALUES('legacy-card', 'task-1', 0, 'packaged')
                """
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )
        seed_stage_states(self.db, "task-1")
        self.db.connection.execute(
            "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()
        before = self._configuration_state()

        with self.assertRaisesRegex(ValueError, "现有.*素材范围"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="primary",
                selected_generic_ids=[],
                include_default_generic=False,
            )

        self.assertEqual(self._configuration_state(), before)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT asset_id FROM task_assets WHERE task_id='task-1'"
            ).fetchone()[0],
            "unbound",
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT asset_id FROM scene_cards WHERE card_id='legacy-card'"
            ).fetchone()[0],
            "packaged",
        )

    def test_first_binding_accepts_legacy_references_with_any_available_scoped_location(self):
        self._insert_asset("inside", "primary", "park/inside.mp4")
        self._insert_asset("multi", "other-park", "wrong.mp4")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns, file_identity
                ) VALUES('multi', 'primary', 'park/nested/right.mp4', 1, 'multi-right')
                """
            )
        add_asset(self.db, "task-1", "inside", "legacy")
        add_asset(self.db, "task-1", "multi", "legacy")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
                VALUES('legacy-card', 'task-1', 0, 'multi')
                """
            )

        result = set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )

        self.assertEqual(result["primary"]["library_id"], "primary")
        self.assertEqual(
            {
                row[0]
                for row in self.db.connection.execute(
                    "SELECT asset_id FROM task_assets WHERE task_id='task-1'"
                )
            },
            {"inside", "multi"},
        )

    def test_rebinding_accepts_reference_with_available_location_in_proposed_scope(self):
        self._insert_asset("multi", "primary", "park/right.mp4")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns, file_identity
                ) VALUES('multi', 'primary-2', 'other/right.mp4', 1, 'multi-primary-2')
                """
            )
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        add_asset(self.db, "task-1", "multi", "legacy")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
                VALUES('multi-card', 'task-1', 0, 'multi')
                """
            )

        result = set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary-2",
            selected_generic_ids=[],
            include_default_generic=False,
        )

        self.assertEqual(result["primary"]["library_id"], "primary-2")
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_assets WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM scene_cards WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )

    def test_rebinding_validation_failure_preserves_the_complete_configuration(self):
        self._insert_asset("only-old", "primary", "inside.mp4")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        add_asset(self.db, "task-1", "only-old", "selected")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
                VALUES('only-old-card', 'task-1', 0, 'only-old')
                """
            )
        before = self._configuration_state()

        with self.assertRaisesRegex(ValueError, "现有.*素材范围"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="primary-2",
                selected_generic_ids=[],
                include_default_generic=False,
            )

        self.assertEqual(self._configuration_state(), before)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT asset_id FROM scene_cards WHERE card_id='only-old-card'"
            ).fetchone()[0],
            "only-old",
        )

    def test_first_binding_uses_path_segments_not_string_prefixes(self):
        self._insert_asset("outside", "primary", "parking/outside.mp4")
        add_asset(self.db, "task-1", "outside", "legacy")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_source_scopes(
                  scope_id, task_id, library_id, role, relative_path,
                  source_mode, active, priority
                ) VALUES('legacy-park', 'task-1', 'primary', 'primary',
                         'park', 'primary', 1, 0)
                """
            )
        before = self._configuration_state()

        with self.assertRaisesRegex(ValueError, "现有.*素材范围"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="primary",
                selected_generic_ids=[],
                include_default_generic=False,
            )

        self.assertEqual(self._configuration_state(), before)

    def test_python_entrypoints_fail_closed_for_legacy_unsafe_scope_paths(self):
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self.db.connection.execute(
            "DROP TRIGGER IF EXISTS task_source_relative_path_validate_update"
        )
        self.db.connection.execute(
            """
            UPDATE task_source_scopes SET relative_path='../other-park'
            WHERE task_id='task-1' AND active=1
            """
        )
        self.db.connection.commit()

        with self.assertRaisesRegex(ValueError, "relative_path"):
            get_task_sources(self.db, "task-1")
        with self.assertRaisesRegex(ValueError, "relative_path"):
            preview_source_sync(self.db, "task-1")
        with self.assertRaisesRegex(ValueError, "relative_path"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="primary",
                selected_generic_ids=["selected-a"],
                include_default_generic=False,
            )

    def test_preview_is_read_only_stable_and_reports_scope_diffs(self):
        (self.paths["primary"] / "new.mp4").write_bytes(b"new material")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a"],
            include_default_generic=False,
        )
        before = {
            table: self.db.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "assets",
                "asset_locations",
                "task_source_snapshots",
                "task_stage_states",
            )
        }
        revision = self.db.connection.execute(
            "SELECT revision FROM task_source_settings WHERE task_id='task-1'"
        ).fetchone()[0]
        total_changes = self.db.connection.total_changes

        first = preview_source_sync(self.db, "task-1")
        second = preview_source_sync(self.db, "task-1")

        self.assertTrue(first["changed"])
        self.assertFalse(first["no_op"])
        self.assertEqual(first["impact_token"], second["impact_token"])
        self.assertEqual(len(first["impact_token"]), 64)
        self.assertEqual(first["health"]["status"], "healthy")
        primary = next(
            item for item in first["scopes"] if item["source_mode"] == "primary"
        )
        self.assertEqual(primary["change_status"], "changed")
        self.assertEqual(primary["added"], {"count": 1, "paths": ["new.mp4"]})
        self.assertEqual(primary["changed"], {"count": 0, "paths": []})
        self.assertEqual(primary["missing"], {"count": 0, "paths": []})
        self.assertFalse(first["invalidation"]["will_invalidate_picture_lock"])
        self.assertEqual(
            {
                table: self.db.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in before
            },
            before,
        )
        self.assertEqual(self.db.connection.total_changes, total_changes)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT revision FROM task_source_settings WHERE task_id='task-1'"
            ).fetchone()[0],
            revision,
        )

    def test_changed_sync_rejects_missing_wrong_and_stale_tokens_before_scan(self):
        material = self.paths["primary"] / "main.mp4"
        material.write_bytes(b"version one")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )

        with self.assertRaisesRegex(ValueError, "确认"):
            sync_task_sources(self.db, "task-1")
        with self.assertRaisesRegex(ValueError, "token"):
            sync_task_sources(self.db, "task-1", impact_token="wrong-token")
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM asset_locations"
            ).fetchone()[0],
            0,
        )

        stale = preview_source_sync(self.db, "task-1")
        material.write_bytes(b"version two is different")
        with self.assertRaisesRegex(ValueError, "已过期"):
            sync_task_sources(
                self.db, "task-1", impact_token=stale["impact_token"]
            )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM asset_locations"
            ).fetchone()[0],
            0,
        )

        fresh = preview_source_sync(self.db, "task-1")
        result = sync_task_sources(
            self.db, "task-1", impact_token=fresh["impact_token"]
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM asset_locations"
            ).fetchone()[0],
            1,
        )

    def test_no_change_sync_is_a_write_free_no_op_without_token(self):
        (self.paths["primary"] / "main.mp4").write_bytes(b"stable")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        snapshot_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM task_source_snapshots"
        ).fetchone()[0]
        revision = self.db.connection.execute(
            "SELECT revision FROM task_source_settings WHERE task_id='task-1'"
        ).fetchone()[0]
        total_changes = self.db.connection.total_changes

        preview = preview_source_sync(self.db, "task-1")
        result = sync_task_sources(self.db, "task-1")

        self.assertFalse(preview["changed"])
        self.assertTrue(preview["no_op"])
        self.assertIsNone(preview["impact_token"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["snapshot_ids"], [])
        self.assertEqual(self.db.connection.total_changes, total_changes)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots"
            ).fetchone()[0],
            snapshot_count,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT revision FROM task_source_settings WHERE task_id='task-1'"
            ).fetchone()[0],
            revision,
        )

    def test_sync_task_state_rolls_back_and_next_preview_still_requires_confirmation(self):
        (self.paths["primary"] / "main.mp4").write_bytes(b"new")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )
        baseline_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id='task-1'"
        ).fetchone()[0]
        preview = preview_source_sync(self.db, "task-1")
        self.assertTrue(preview["invalidation"]["will_invalidate_picture_lock"])
        self.db.connection.executescript(
            """
            CREATE TRIGGER fail_sync_workflow_invalidation
            BEFORE UPDATE OF status ON task_stage_states
            WHEN NEW.task_id='task-1' AND NEW.stage_key='ai_storyboard'
            BEGIN
              SELECT RAISE(ABORT, 'forced sync workflow failure');
            END;
            """
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "forced sync workflow failure"
        ):
            sync_task_sources(
                self.db, "task-1", impact_token=preview["impact_token"]
            )

        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM asset_locations WHERE library_id='primary'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id='task-1'"
            ).fetchone()[0],
            baseline_count,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT changed_since_lock FROM picture_lock WHERE task_id='task-1'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.connection.execute(
                """
                SELECT status FROM task_stage_states
                WHERE task_id='task-1' AND stage_key='ai_storyboard'
                """
            ).fetchone()[0],
            "completed",
        )

        retry_preview = preview_source_sync(self.db, "task-1")
        self.assertTrue(retry_preview["changed"])
        with self.assertRaisesRegex(ValueError, "确认"):
            sync_task_sources(self.db, "task-1")

        self.db.connection.execute("DROP TRIGGER fail_sync_workflow_invalidation")
        self.db.connection.commit()
        result = sync_task_sources(
            self.db,
            "task-1",
            impact_token=retry_preview["impact_token"],
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT changed_since_lock FROM picture_lock WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )

    def test_offline_generic_is_only_a_warning_not_a_lock_invalidation(self):
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a"],
            include_default_generic=False,
        )
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )
        self.paths["selected-a"].rename(self.root / "selected-offline")

        preview = preview_source_sync(self.db, "task-1")

        self.assertEqual(preview["health"]["status"], "warning")
        self.assertFalse(preview["changed"])
        self.assertTrue(preview["health_changed"])
        self.assertFalse(preview["no_op"])
        self.assertIsNone(preview["impact_token"])
        self.assertFalse(preview["invalidation"]["will_invalidate_picture_lock"])
        result = sync_task_sources(self.db, "task-1")
        self.assertFalse(result["changed"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT changed_since_lock FROM picture_lock WHERE task_id='task-1'"
            ).fetchone()[0],
            0,
        )

    def test_default_set_preview_adds_and_removes_while_selected_survives(self):
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["default-b"],
            include_default_generic=True,
        )
        retired_scope = self.db.connection.execute(
            """
            SELECT scope_id FROM task_source_scopes
            WHERE task_id='task-1' AND active=1
              AND source_mode='default_generic' AND library_id='default-a'
            """
        ).fetchone()[0]
        retired_snapshots = self.db.connection.execute(
            "SELECT COUNT(*) FROM task_source_snapshots WHERE scope_id=?",
            (retired_scope,),
        ).fetchone()[0]
        register_library(
            self.db,
            "default-a",
            "default-a",
            "local",
            str(self.paths["default-a"]),
            purpose="generic",
            is_default_generic=False,
        )
        register_library(
            self.db,
            "default-b",
            "default-b",
            "local",
            str(self.paths["default-b"]),
            purpose="generic",
            is_default_generic=False,
        )
        register_library(
            self.db,
            "generic-other",
            "generic-other",
            "local",
            str(self.paths["generic-other"]),
            purpose="generic",
            is_default_generic=True,
        )

        preview = preview_source_sync(self.db, "task-1")

        self.assertEqual(
            [item["library_id"] for item in preview["configuration"]["additions"]],
            ["generic-other"],
        )
        self.assertEqual(
            [item["library_id"] for item in preview["configuration"]["removals"]],
            ["default-a"],
        )
        self.assertTrue(preview["changed"])
        self.assertIsNotNone(preview["impact_token"])

        scanned = []

        def tracked_scan(db, library_id):
            scanned.append(library_id)
            return scan_library(db, library_id)

        with patch("media_catalog.task_sources.scan_library", side_effect=tracked_scan):
            result = sync_task_sources(
                self.db,
                "task-1",
                impact_token=preview["impact_token"],
            )

        self.assertTrue(result["changed"])
        self.assertTrue(result["configuration_changed"])
        self.assertNotIn("default-a", scanned)
        self.assertIn("generic-other", scanned)
        self.assertEqual(
            [item["library_id"] for item in result["sources"]["selected_generic"]],
            ["default-b"],
        )
        self.assertEqual(
            [item["library_id"] for item in result["sources"]["default_generic"]],
            ["generic-other"],
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT active FROM task_source_scopes WHERE scope_id=?",
                (retired_scope,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE scope_id=?",
                (retired_scope,),
            ).fetchone()[0],
            retired_snapshots,
        )

    def test_default_reconciliation_token_binds_additions_and_removals(self):
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=True,
        )
        register_library(
            self.db,
            "generic-other",
            "generic-other",
            "local",
            str(self.paths["generic-other"]),
            purpose="generic",
            is_default_generic=True,
        )
        stale = preview_source_sync(self.db, "task-1")
        register_library(
            self.db,
            "default-a",
            "default-a",
            "local",
            str(self.paths["default-a"]),
            purpose="generic",
            is_default_generic=False,
        )

        with patch("media_catalog.task_sources.scan_library") as mocked_scan:
            with self.assertRaisesRegex(ValueError, "已过期"):
                sync_task_sources(
                    self.db,
                    "task-1",
                    impact_token=stale["impact_token"],
                )
        mocked_scan.assert_not_called()

    def test_confirmed_proposal_rejects_default_removal_race_before_commit(self):
        material = self.paths["primary"] / "main.mp4"
        material.write_bytes(b"version one")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=True,
        )
        self._confirmed_sync()
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )

        material.write_bytes(b"version two")
        confirmed = preview_source_sync(self.db, "task-1")
        self.assertTrue(confirmed["changed"])
        self.assertEqual(confirmed["configuration"]["removals"], [])
        before = self._configuration_state()
        competing = CatalogDatabase.open(self.db.path)
        original_verify = task_sources_module._verified_post_scan_preview

        def change_defaults_after_verification(*args, **kwargs):
            verified = original_verify(*args, **kwargs)
            self.assertEqual(verified["configuration"]["removals"], [])
            competing.connection.execute(
                """
                UPDATE libraries SET is_default_generic=0
                WHERE library_id='default-a'
                """
            )
            competing.connection.commit()
            return verified

        try:
            with patch(
                "media_catalog.task_sources._verified_post_scan_preview",
                side_effect=change_defaults_after_verification,
            ):
                with self.assertRaisesRegex(ValueError, "已过期|重新预检"):
                    sync_task_sources(
                        self.db,
                        "task-1",
                        impact_token=confirmed["impact_token"],
                    )
        finally:
            competing.close()

        self.assertEqual(self._configuration_state(), before)

    def test_confirmed_media_state_rejects_competing_catalog_refresh_before_commit(self):
        material = self.paths["primary"] / "main.mp4"
        material.write_bytes(b"version one")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )

        material.write_bytes(b"version two")
        confirmed = preview_source_sync(self.db, "task-1")
        self.assertTrue(confirmed["changed"])
        before = self._configuration_state()
        competing = CatalogDatabase.open(self.db.path)
        original_commit = task_sources_module._commit_confirmed_source_sync

        def refresh_catalog_after_verification(db, task_id, post_scan_preview):
            material.write_bytes(b"version three after verification")
            scan_library(competing, "primary")
            return original_commit(db, task_id, post_scan_preview)

        try:
            with patch(
                "media_catalog.task_sources._commit_confirmed_source_sync",
                side_effect=refresh_catalog_after_verification,
            ):
                with self.assertRaisesRegex(ValueError, "已过期|重新预检"):
                    sync_task_sources(
                        self.db,
                        "task-1",
                        impact_token=confirmed["impact_token"],
                    )
        finally:
            competing.close()

        self.assertEqual(self._configuration_state(), before)
        retry = preview_source_sync(self.db, "task-1")
        self.assertTrue(retry["changed"])
        self.assertNotEqual(retry["impact_token"], confirmed["impact_token"])

    def test_post_scan_commit_proposal_freezes_verified_scope_media_state(self):
        material = self.paths["primary"] / "main.mp4"
        material.write_bytes(b"version one")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()

        material.write_bytes(b"version two")
        scan_library(self.db, "primary")
        proposal = task_sources_module._preview_source_sync(
            self.db,
            "task-1",
            include_commit_guard=True,
        )

        primary = next(
            item
            for item in proposal["_verified_media_state"]
            if item["scope"]["source_mode"] == "primary"
        )
        self.assertEqual(
            primary["scope"],
            {
                "scope_id": primary["scope"]["scope_id"],
                "library_id": "primary",
                "source_mode": "primary",
                "relative_path": "",
                "priority": 0,
                "pending_configuration": False,
                "pending_removal": False,
            },
        )
        self.assertTrue(primary["health"]["online"])
        self.assertIsNotNone(primary["baseline"]["snapshot_id"])
        self.assertIn('"assets"', primary["baseline"]["summary_json"])
        self.assertEqual(len(primary["baseline"]["digest"]), 64)
        self.assertEqual(
            primary["filesystem"]["manifest"][0]["relative_path"],
            "main.mp4",
        )
        self.assertEqual(len(primary["filesystem"]["state_digest"]), 64)
        self.assertIn('"assets"', primary["catalog"]["summary_json"])
        self.assertEqual(primary["catalog"]["asset_count"], 1)
        self.assertEqual(len(primary["catalog"]["digest"]), 64)

    def test_confirmed_media_state_rejects_new_baseline_before_commit(self):
        material = self.paths["primary"] / "main.mp4"
        material.write_bytes(b"version one")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )

        material.write_bytes(b"version two")
        confirmed = preview_source_sync(self.db, "task-1")
        scope_id = self.db.connection.execute(
            """
            SELECT scope_id FROM task_source_scopes
            WHERE task_id='task-1' AND active=1 AND source_mode='primary'
            """
        ).fetchone()[0]
        competing = CatalogDatabase.open(self.db.path)
        original_commit = task_sources_module._commit_confirmed_source_sync
        competing_state = []

        def write_new_baseline_after_verification(db, task_id, post_scan_preview):
            previous = competing.connection.execute(
                """
                SELECT summary_json, asset_count, total_size_bytes
                FROM task_source_snapshots
                WHERE scope_id=?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (scope_id,),
            ).fetchone()
            competing.connection.execute(
                """
                INSERT INTO task_source_snapshots(
                  snapshot_id, task_id, scope_id, summary_json,
                  asset_count, total_size_bytes, created_at
                ) VALUES('concurrent-baseline', 'task-1', ?, ?, ?, ?,
                         datetime('now'))
                """,
                (
                    scope_id,
                    previous["summary_json"],
                    previous["asset_count"],
                    previous["total_size_bytes"],
                ),
            )
            competing.connection.commit()
            competing_state.append(self._configuration_state())
            return original_commit(db, task_id, post_scan_preview)

        try:
            with patch(
                "media_catalog.task_sources._commit_confirmed_source_sync",
                side_effect=write_new_baseline_after_verification,
            ):
                with self.assertRaisesRegex(ValueError, "已过期|重新预检"):
                    sync_task_sources(
                        self.db,
                        "task-1",
                        impact_token=confirmed["impact_token"],
                    )
        finally:
            competing.close()

        self.assertEqual(self._configuration_state(), competing_state[0])

    def test_unscanned_file_change_after_verification_is_not_silently_baselined(self):
        material = self.paths["primary"] / "main.mp4"
        material.write_bytes(b"version one")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()

        material.write_bytes(b"version two")
        confirmed = preview_source_sync(self.db, "task-1")
        self.assertTrue(confirmed["changed"])
        before = self._configuration_state()
        original_commit = task_sources_module._commit_confirmed_source_sync

        def mutate_file_after_verification(db, task_id, post_scan_preview):
            material.write_bytes(b"version three without catalog scan")
            return original_commit(db, task_id, post_scan_preview)

        with patch(
            "media_catalog.task_sources._commit_confirmed_source_sync",
            side_effect=mutate_file_after_verification,
        ):
            with self.assertRaisesRegex(ValueError, "已过期|重新预检"):
                sync_task_sources(
                    self.db,
                    "task-1",
                    impact_token=confirmed["impact_token"],
                )

        self.assertEqual(self._configuration_state(), before)
        retry = preview_source_sync(self.db, "task-1")
        self.assertTrue(retry["changed"])
        self.assertIsNotNone(retry["impact_token"])

    def test_default_reconciliation_rolls_back_all_task_state_together(self):
        (self.paths["generic-other"] / "new.mp4").write_bytes(b"new default")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=True,
        )
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )
        register_library(
            self.db,
            "default-a",
            "default-a",
            "local",
            str(self.paths["default-a"]),
            purpose="generic",
            is_default_generic=False,
        )
        register_library(
            self.db,
            "generic-other",
            "generic-other",
            "local",
            str(self.paths["generic-other"]),
            purpose="generic",
            is_default_generic=True,
        )
        before = self._configuration_state()
        preview = preview_source_sync(self.db, "task-1")
        self.db.connection.executescript(
            """
            CREATE TRIGGER fail_default_reconciliation_invalidation
            BEFORE UPDATE OF status ON task_stage_states
            WHEN NEW.task_id='task-1' AND NEW.stage_key='ai_storyboard'
            BEGIN
              SELECT RAISE(ABORT, 'forced default reconciliation failure');
            END;
            """
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "forced default reconciliation failure"
        ):
            sync_task_sources(
                self.db,
                "task-1",
                impact_token=preview["impact_token"],
            )

        self.assertEqual(self._configuration_state(), before)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM asset_locations WHERE library_id='generic-other'"
            ).fetchone()[0],
            1,
        )
        retry = preview_source_sync(self.db, "task-1")
        self.assertTrue(retry["changed"])
        self.assertEqual(
            [item["library_id"] for item in retry["configuration"]["removals"]],
            ["default-a"],
        )

    def test_sync_rejects_filesystem_change_that_happens_during_scan(self):
        material = self.paths["primary"] / "main.mp4"
        material.write_bytes(b"version one")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )
        material.write_bytes(b"version two")
        preview = preview_source_sync(self.db, "task-1")
        before = self._configuration_state()

        def mutate_after_scan(db, library_id):
            result = scan_library(db, library_id)
            if library_id == "primary":
                (self.paths["primary"] / "late.mp4").write_bytes(b"late")
            return result

        with patch(
            "media_catalog.task_sources.scan_library",
            side_effect=mutate_after_scan,
        ):
            with self.assertRaisesRegex(ValueError, "扫描期间|重新预检"):
                sync_task_sources(
                    self.db,
                    "task-1",
                    impact_token=preview["impact_token"],
                )

        self.assertEqual(self._configuration_state(), before)
        retry = preview_source_sync(self.db, "task-1")
        self.assertTrue(retry["changed"])
        self.assertNotEqual(retry["impact_token"], preview["impact_token"])

    def test_primary_offline_sync_persists_health_before_blocking(self):
        (self.paths["primary"] / "main.mp4").write_bytes(b"main")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        snapshot_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id='task-1'"
        ).fetchone()[0]
        self.paths["primary"].rename(self.root / "primary-offline")
        preview = preview_source_sync(self.db, "task-1")
        self.assertTrue(preview["health_changed"])
        self.assertFalse(preview["changed"])

        with self.assertRaisesRegex(LibraryOffline, "主素材目录"):
            sync_task_sources(self.db, "task-1")

        self.assertEqual(
            self.db.connection.execute(
                "SELECT online FROM libraries WHERE library_id='primary'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT state FROM asset_locations WHERE library_id='primary'"
            ).fetchone()[0],
            "offline",
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id='task-1'"
            ).fetchone()[0],
            snapshot_count,
        )

    def test_generic_offline_and_reconnect_only_persist_health(self):
        (self.paths["primary"] / "main.mp4").write_bytes(b"main")
        (self.paths["selected-a"] / "shared.mp4").write_bytes(b"shared")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a"],
            include_default_generic=False,
        )
        self._confirmed_sync()
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )
        snapshot_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id='task-1'"
        ).fetchone()[0]
        selected_offline = self.root / "selected-offline-health"
        self.paths["selected-a"].rename(selected_offline)

        offline_preview = preview_source_sync(self.db, "task-1")
        self.assertFalse(offline_preview["changed"])
        self.assertTrue(offline_preview["health_changed"])
        offline_result = sync_task_sources(self.db, "task-1")

        self.assertFalse(offline_result["changed"])
        self.assertTrue(offline_result["health_changed"])
        self.assertEqual(offline_result["health"]["status"], "warning")
        self.assertEqual(
            tuple(
                self.db.connection.execute(
                    """
                    SELECT l.online, al.state FROM libraries l
                    JOIN asset_locations al USING(library_id)
                    WHERE l.library_id='selected-a'
                    """
                ).fetchone()
            ),
            (0, "offline"),
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id='task-1'"
            ).fetchone()[0],
            snapshot_count,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT changed_since_lock FROM picture_lock WHERE task_id='task-1'"
            ).fetchone()[0],
            0,
        )

        selected_offline.rename(self.paths["selected-a"])
        reconnect_preview = preview_source_sync(self.db, "task-1")
        self.assertFalse(reconnect_preview["changed"])
        self.assertTrue(reconnect_preview["health_changed"])
        reconnect_result = sync_task_sources(self.db, "task-1")

        self.assertFalse(reconnect_result["changed"])
        self.assertTrue(reconnect_result["health_changed"])
        self.assertEqual(reconnect_result["health"]["status"], "healthy")
        self.assertEqual(
            tuple(
                self.db.connection.execute(
                    """
                    SELECT l.online, al.state FROM libraries l
                    JOIN asset_locations al USING(library_id)
                    WHERE l.library_id='selected-a'
                    """
                ).fetchone()
            ),
            (1, "available"),
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id='task-1'"
            ).fetchone()[0],
            snapshot_count,
        )
        self.assertEqual(
            self.db.connection.execute(
                """
                SELECT status FROM task_stage_states
                WHERE task_id='task-1' AND stage_key='ai_storyboard'
                """
            ).fetchone()[0],
            "completed",
        )

    def test_set_and_get_sources_have_stable_priority_and_explicit_defaults(self):
        result = self._set_default_scope()

        self.assertEqual(result["primary"]["library_id"], "primary")
        self.assertEqual(
            [item["library_id"] for item in result["selected_generic"]],
            ["selected-b", "default-a", "selected-a"],
        )
        self.assertEqual(
            [item["library_id"] for item in result["default_generic"]],
            ["default-b"],
        )
        self.assertTrue(result["include_default_generic"])
        expected_fields = {
            "library_id",
            "name",
            "mount_path",
            "online",
            "asset_count",
            "last_synced_at",
        }
        for item in (
            [result["primary"]]
            + result["selected_generic"]
            + result["default_generic"]
        ):
            self.assertTrue(expected_fields <= set(item))

    def test_only_explicitly_marked_default_generic_libraries_are_included(self):
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=True,
        )
        result = get_task_sources(self.db, "task-1")

        self.assertEqual(
            [item["library_id"] for item in result["default_generic"]],
            ["default-a", "default-b"],
        )
        self.assertNotIn(
            "generic-other",
            [item["library_id"] for item in result["default_generic"]],
        )

    def test_repeated_set_is_idempotent(self):
        first = self._set_default_scope()
        before = self.db.connection.execute(
            "SELECT COUNT(*), MAX(updated_at) FROM task_source_scopes"
        ).fetchone()
        before_scope_ids = [
            row[0]
            for row in self.db.connection.execute(
                "SELECT scope_id FROM task_source_scopes ORDER BY scope_id"
            )
        ]
        before_snapshot_ids = [
            row[0]
            for row in self.db.connection.execute(
                "SELECT snapshot_id FROM task_source_snapshots ORDER BY snapshot_id"
            )
        ]
        second = self._set_default_scope()
        after = self.db.connection.execute(
            "SELECT COUNT(*), MAX(updated_at) FROM task_source_scopes"
        ).fetchone()

        self.assertEqual(first, second)
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(
            [
                row[0]
                for row in self.db.connection.execute(
                    "SELECT scope_id FROM task_source_scopes ORDER BY scope_id"
                )
            ],
            before_scope_ids,
        )
        self.assertEqual(
            [
                row[0]
                for row in self.db.connection.execute(
                    "SELECT snapshot_id FROM task_source_snapshots ORDER BY snapshot_id"
                )
            ],
            before_snapshot_ids,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT revision FROM task_source_settings WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )

    def test_rebinding_preserves_subdirectory_scopes_and_generic_mode_migration(self):
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a"],
            include_default_generic=False,
        )
        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE task_source_scopes SET relative_path='park-a'
                WHERE task_id='task-1' AND active=1 AND source_mode='primary'
                """
            )
            connection.execute(
                """
                UPDATE task_source_scopes SET relative_path='shared/a'
                WHERE task_id='task-1' AND active=1
                  AND source_mode='selected_generic'
                """
            )
            connection.execute(
                """
                INSERT INTO task_source_scopes(
                  scope_id, task_id, library_id, role, relative_path,
                  source_mode, active, priority
                ) VALUES(
                  'scope-selected-a-b', 'task-1', 'selected-a',
                  'supplementary', 'shared/b', 'selected_generic', 1, 1001
                )
                """
            )

        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a", "selected-b"],
            include_default_generic=False,
        )

        primary_path = self.db.connection.execute(
            """
            SELECT relative_path FROM task_source_scopes
            WHERE task_id='task-1' AND active=1 AND source_mode='primary'
            """
        ).fetchone()[0]
        selected_a_paths = {
            row[0]
            for row in self.db.connection.execute(
                """
                SELECT relative_path FROM task_source_scopes
                WHERE task_id='task-1' AND active=1
                  AND source_mode='selected_generic' AND library_id='selected-a'
                """
            )
        }
        self.assertEqual(primary_path, "park-a")
        self.assertEqual(selected_a_paths, {"shared/a", "shared/b"})

        self.db.connection.execute(
            "UPDATE libraries SET is_default_generic=1 WHERE library_id='selected-a'"
        )
        self.db.connection.commit()
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-b"],
            include_default_generic=True,
        )

        migrated_paths = {
            row[0]
            for row in self.db.connection.execute(
                """
                SELECT relative_path FROM task_source_scopes
                WHERE task_id='task-1' AND active=1
                  AND source_mode='default_generic' AND library_id='selected-a'
                """
            )
        }
        self.assertEqual(migrated_paths, {"shared/a", "shared/b"})
        self.assertEqual(
            self.db.connection.execute(
                """
                SELECT relative_path FROM task_source_scopes
                WHERE task_id='task-1' AND active=1 AND source_mode='primary'
                """
            ).fetchone()[0],
            "park-a",
        )

    def test_new_scopes_capture_catalog_baselines_and_preserve_snapshot_history(self):
        self._insert_asset("selected-b-existing", "selected-b", "existing.mp4")

        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a"],
            include_default_generic=False,
        )
        first_scope_ids = {
            row[0]
            for row in self.db.connection.execute(
                """
                SELECT scope_id FROM task_source_scopes
                WHERE task_id='task-1' AND active=1
                """
            )
        }
        first_snapshots = self.db.connection.execute(
            """
            SELECT scope_id, summary_json, asset_count, total_size_bytes
            FROM task_source_snapshots WHERE task_id='task-1'
            """
        ).fetchall()
        self.assertEqual({row["scope_id"] for row in first_snapshots}, first_scope_ids)
        self.assertTrue(all(row["asset_count"] == 0 for row in first_snapshots))
        self.assertTrue(
            all(json.loads(row["summary_json"])["assets"] == [] for row in first_snapshots)
        )

        first_snapshot_ids = {
            row[0]
            for row in self.db.connection.execute(
                "SELECT snapshot_id FROM task_source_snapshots"
            )
        }
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a", "selected-b"],
            include_default_generic=False,
        )

        active_scope_ids = {
            row[0]
            for row in self.db.connection.execute(
                """
                SELECT scope_id FROM task_source_scopes
                WHERE task_id='task-1' AND active=1
                """
            )
        }
        current_snapshot_scope_ids = {
            row[0]
            for row in self.db.connection.execute(
                """
                SELECT scope_id FROM task_source_snapshots
                WHERE scope_id IN (
                  SELECT scope_id FROM task_source_scopes
                  WHERE task_id='task-1' AND active=1
                )
                """
            )
        }
        self.assertEqual(current_snapshot_scope_ids, active_scope_ids)
        self.assertTrue(
            first_snapshot_ids
            <= {
                row[0]
                for row in self.db.connection.execute(
                    "SELECT snapshot_id FROM task_source_snapshots"
                )
            }
        )
        selected_b_snapshot = self.db.connection.execute(
            """
            SELECT ss.summary_json, ss.asset_count, ss.total_size_bytes
            FROM task_source_snapshots ss
            JOIN task_source_scopes s USING(scope_id)
            WHERE s.task_id='task-1' AND s.active=1
              AND s.library_id='selected-b'
            """
        ).fetchone()
        self.assertEqual(selected_b_snapshot["asset_count"], 1)
        self.assertEqual(selected_b_snapshot["total_size_bytes"], 1)
        self.assertEqual(
            [item["asset_id"] for item in json.loads(selected_b_snapshot["summary_json"])["assets"]],
            ["selected-b-existing"],
        )

    def test_rebinding_rolls_back_scopes_snapshots_revision_lock_and_workflow_together(self):
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a"],
            include_default_generic=False,
        )
        seed_stage_states(self.db, "task-1")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
            )
            connection.execute(
                """
                INSERT INTO picture_lock(task_id, lock_version, changed_since_lock)
                VALUES('task-1', 'v1', 0)
                """
            )

        before_scopes = [
            tuple(row)
            for row in self.db.connection.execute(
                """
                SELECT scope_id, library_id, relative_path, source_mode,
                       active, priority, retired_at
                FROM task_source_scopes WHERE task_id='task-1'
                ORDER BY scope_id
                """
            )
        ]
        before_snapshot_ids = [
            row[0]
            for row in self.db.connection.execute(
                "SELECT snapshot_id FROM task_source_snapshots ORDER BY snapshot_id"
            )
        ]
        before_revision = self.db.connection.execute(
            "SELECT revision FROM task_source_settings WHERE task_id='task-1'"
        ).fetchone()[0]
        before_stages = [
            tuple(row)
            for row in self.db.connection.execute(
                """
                SELECT stage_key, status, detail_json FROM task_stage_states
                WHERE task_id='task-1' ORDER BY stage_key
                """
            )
        ]
        self.db.connection.executescript(
            """
            CREATE TRIGGER fail_task_source_workflow_invalidation
            BEFORE UPDATE OF status ON task_stage_states
            WHEN NEW.task_id='task-1' AND NEW.stage_key='ai_storyboard'
            BEGIN
              SELECT RAISE(ABORT, 'forced workflow invalidation failure');
            END;
            """
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "forced workflow invalidation failure"
        ):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="primary-2",
                selected_generic_ids=["selected-b"],
                include_default_generic=False,
            )

        self.assertEqual(
            [
                tuple(row)
                for row in self.db.connection.execute(
                    """
                    SELECT scope_id, library_id, relative_path, source_mode,
                           active, priority, retired_at
                    FROM task_source_scopes WHERE task_id='task-1'
                    ORDER BY scope_id
                    """
                )
            ],
            before_scopes,
        )
        self.assertEqual(
            [
                row[0]
                for row in self.db.connection.execute(
                    "SELECT snapshot_id FROM task_source_snapshots ORDER BY snapshot_id"
                )
            ],
            before_snapshot_ids,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT revision FROM task_source_settings WHERE task_id='task-1'"
            ).fetchone()[0],
            before_revision,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT changed_since_lock FROM picture_lock WHERE task_id='task-1'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            [
                tuple(row)
                for row in self.db.connection.execute(
                    """
                    SELECT stage_key, status, detail_json FROM task_stage_states
                    WHERE task_id='task-1' ORDER BY stage_key
                    """
                )
            ],
            before_stages,
        )

    def test_invalid_source_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "主素材"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="",
                selected_generic_ids=[],
                include_default_generic=False,
            )
        with self.assertRaisesRegex(ValueError, "任务专属"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="selected-a",
                selected_generic_ids=[],
                include_default_generic=False,
            )
        with self.assertRaisesRegex(ValueError, "通用"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="primary",
                selected_generic_ids=["primary-2"],
                include_default_generic=False,
            )
        with self.assertRaisesRegex(ValueError, "包装"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="primary",
                selected_generic_ids=["packaging"],
                include_default_generic=False,
            )

    def test_primary_offline_blocks_sync_before_supplementary_scans(self):
        (self.paths["selected-a"] / "shared.mp4").write_bytes(b"shared")
        self._set_default_scope()
        self.paths["primary"].rename(self.root / "primary-offline")

        health = source_health(self.db, "task-1")
        self.assertEqual(health["status"], "blocking")
        self.assertEqual(health["blocking"][0]["library_id"], "primary")
        preview = preview_source_sync(self.db, "task-1")
        self.assertEqual(preview["health"]["status"], "blocking")
        with self.assertRaisesRegex(LibraryOffline, "主素材目录"):
            sync_task_sources(self.db, "task-1")
        self.assertEqual(
            self.db.connection.execute(
                """
                SELECT COUNT(*) FROM asset_locations
                WHERE library_id='selected-a'
                """
            ).fetchone()[0],
            0,
        )

    def test_offline_generic_is_warning_and_other_bound_sources_sync(self):
        (self.paths["primary"] / "main.mp4").write_bytes(b"main")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a"],
            include_default_generic=False,
        )
        self.paths["selected-a"].rename(self.root / "selected-offline")

        preview = preview_source_sync(self.db, "task-1")
        self.assertEqual(preview["health"]["status"], "warning")
        result = sync_task_sources(
            self.db, "task-1", impact_token=preview["impact_token"]
        )

        self.assertEqual(result["health"]["status"], "warning")
        self.assertEqual(
            result["health"]["warnings"][0]["library_id"],
            "selected-a",
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM asset_locations WHERE library_id='primary'"
            ).fetchone()[0],
            1,
        )

    def test_sync_scans_only_bound_libraries(self):
        (self.paths["primary"] / "main.mp4").write_bytes(b"main")
        (self.paths["selected-a"] / "shared.mp4").write_bytes(b"shared")
        (self.paths["other-park"] / "secret.mp4").write_bytes(b"secret")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=["selected-a"],
            include_default_generic=False,
        )

        self._confirmed_sync()

        scanned = {
            row[0]
            for row in self.db.connection.execute(
                "SELECT DISTINCT library_id FROM asset_locations"
            )
        }
        self.assertEqual(scanned, {"primary", "selected-a"})

    def test_rename_preserves_asset_id_and_task_selection(self):
        original = self.paths["primary"] / "before.mp4"
        original.write_bytes(b"stable")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        asset_id = self.db.connection.execute(
            "SELECT asset_id FROM asset_locations WHERE library_id='primary'"
        ).fetchone()[0]
        add_asset(self.db, "task-1", asset_id, "candidate")

        original.rename(self.paths["primary"] / "after.mp4")
        self._confirmed_sync()

        location = self.db.connection.execute(
            """
            SELECT asset_id, relative_path FROM asset_locations
            WHERE library_id='primary'
            """
        ).fetchone()
        self.assertEqual((location["asset_id"], location["relative_path"]), (asset_id, "after.mp4"))
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_assets WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )

    def test_replacing_scope_rejects_out_of_scope_edits_without_mutating_history(self):
        (self.paths["primary"] / "main.mp4").write_bytes(b"main")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        asset_id = self.db.connection.execute(
            "SELECT asset_id FROM asset_locations WHERE library_id='primary'"
        ).fetchone()[0]
        add_asset(self.db, "task-1", asset_id, "candidate")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id, task_id, idx, asset_id, confirm_status
                ) VALUES('card-1', 'task-1', 0, ?, 1)
                """,
                (asset_id,),
            )
            connection.execute(
                """
                INSERT INTO picture_lock(
                  task_id, lock_version, changed_since_lock
                ) VALUES('task-1', 'v1', 0)
                """
            )
        seed_stage_states(self.db, "task-1")
        self.db.connection.execute(
            "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()
        old_scope = self.db.connection.execute(
            """
            SELECT scope_id FROM task_source_scopes
            WHERE task_id='task-1' AND active=1 AND source_mode='primary'
            """
        ).fetchone()[0]
        old_snapshot_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM task_source_snapshots"
        ).fetchone()[0]

        before = self._configuration_state()

        with self.assertRaisesRegex(ValueError, "现有.*素材范围"):
            set_task_sources(
                self.db,
                "task-1",
                primary_library_id="primary-2",
                selected_generic_ids=[],
                include_default_generic=False,
            )

        self.assertEqual(self._configuration_state(), before)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT active FROM task_source_scopes WHERE scope_id=?",
                (old_scope,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots"
            ).fetchone()[0],
            old_snapshot_count,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_assets WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM scene_cards WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )
        statuses = {
            row["stage_key"]: row["status"]
            for row in self.db.connection.execute(
                """
                SELECT stage_key, status FROM task_stage_states
                WHERE task_id='task-1'
                """
            )
        }
        self.assertEqual(statuses["material_sync"], "completed")
        self.assertEqual(statuses["ai_storyboard"], "completed")
        self.assertEqual(statuses["picture_lock"], "completed")

    def test_summary_change_after_picture_lock_invalidates_without_replacing_edits(self):
        first = self.paths["primary"] / "first.mp4"
        first.write_bytes(b"first")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        asset_id = self.db.connection.execute(
            "SELECT asset_id FROM asset_locations WHERE library_id='primary'"
        ).fetchone()[0]
        add_asset(self.db, "task-1", asset_id, "candidate")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id, task_id, idx, asset_id, confirm_status
                ) VALUES('card-1', 'task-1', 0, ?, 1)
                """,
                (asset_id,),
            )
            connection.execute(
                """
                INSERT INTO picture_lock(
                  task_id, lock_version, changed_since_lock
                ) VALUES('task-1', 'v1', 0)
                """
            )
        seed_stage_states(self.db, "task-1")
        self.db.connection.execute(
            "UPDATE task_stage_states SET status='completed' WHERE task_id='task-1'"
        )
        self.db.connection.commit()

        (self.paths["primary"] / "second.mp4").write_bytes(b"second")
        self._confirmed_sync()

        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id='task-1'"
            ).fetchone()[0],
            3,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_assets WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM scene_cards WHERE task_id='task-1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.db.connection.execute(
                """
                SELECT status FROM task_stage_states
                WHERE task_id='task-1' AND stage_key='ai_storyboard'
                """
            ).fetchone()[0],
            "invalidated",
        )

    def test_add_and_list_assets_enforce_bound_scope_without_leaking_locations(self):
        self._insert_asset("bound", "primary", "inside.mp4")
        self._insert_asset("unbound", "other-park", "secret.mp4")
        self._insert_asset("packaged", "packaging", "logo.png")
        self._insert_asset("multi", "primary", "chosen.mp4")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns, file_identity
                ) VALUES('multi', 'other-park', 'secret-copy.mp4', 1, 'multi-copy')
                """
            )
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )

        add_asset(self.db, "task-1", "bound", "candidate")
        add_asset(self.db, "task-1", "multi", "candidate")
        with self.assertRaisesRegex(ValueError, "未绑定"):
            add_asset(self.db, "task-1", "unbound", "candidate")
        with self.assertRaisesRegex(ValueError, "未绑定"):
            add_asset(self.db, "task-1", "packaged", "candidate")

        listed = list_task_assets(self.db, "task-1")

        self.assertEqual(
            {item["asset_id"] for item in listed},
            {"bound", "multi"},
        )
        self.assertEqual(
            next(item for item in listed if item["asset_id"] == "multi")[
                "relative_path"
            ],
            "chosen.mp4",
        )

    def test_relative_path_scope_does_not_match_same_name_prefix(self):
        self._insert_asset("inside", "primary", r"\park\inside.mp4")
        self._insert_asset("outside", "primary", r"parking\outside.mp4")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self.db.connection.execute(
            """
            UPDATE task_source_scopes SET relative_path='park'
            WHERE task_id='task-1' AND active=1
            """
        )
        self.db.connection.commit()

        add_asset(self.db, "task-1", "inside", "candidate")
        with self.assertRaisesRegex(ValueError, "未绑定"):
            add_asset(self.db, "task-1", "outside", "candidate")
        self.assertEqual(
            get_task_sources(self.db, "task-1")["primary"]["asset_count"],
            1,
        )

    def test_snapshot_prevents_history_scope_from_being_deleted(self):
        (self.paths["primary"] / "main.mp4").write_bytes(b"main")
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        self._confirmed_sync()
        scope_id = self.db.connection.execute(
            """
            SELECT scope_id FROM task_source_scopes
            WHERE task_id='task-1' AND active=1
            """
        ).fetchone()[0]

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                "DELETE FROM task_source_scopes WHERE scope_id=?",
                (scope_id,),
            )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE scope_id=?",
                (scope_id,),
            ).fetchone()[0],
            2,
        )

    def test_new_explicit_default_is_reconciled_on_next_sync(self):
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=True,
        )
        register_library(
            self.db,
            "generic-other",
            "generic-other",
            "local",
            str(self.paths["generic-other"]),
            purpose="generic",
            is_default_generic=True,
        )

        self._confirmed_sync()

        self.assertIn(
            "generic-other",
            [
                item["library_id"]
                for item in get_task_sources(self.db, "task-1")["default_generic"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
