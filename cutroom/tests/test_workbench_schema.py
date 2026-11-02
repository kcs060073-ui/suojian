import sqlite3
import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase


STAGE_KEYS = (
    "create_preflight",
    "brief_lock",
    "material_sync",
    "ai_storyboard",
    "rough_cut",
    "picture_order",
    "picture_lock",
    "packaging_confirm",
    "fine_render",
    "auto_qa",
    "full_review",
    "publish_package",
)

STAGE_STATUSES = (
    "not_started",
    "ready",
    "in_progress",
    "completed",
    "invalidated",
    "blocked",
)


class WorkbenchSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase.open(Path(self.tmp.name) / "catalog.sqlite")
        self.db.migrate()
        self.db.connection.execute(
            "INSERT INTO tasks(task_id, title) VALUES('task-1', 'Workbench task')"
        )
        self.db.connection.execute(
            "INSERT INTO tasks(task_id, title) VALUES('task-2', 'Other task')"
        )
        self.db.connection.execute(
            """
            INSERT INTO libraries(
              library_id, name, kind, mount_path, created_at, updated_at
            ) VALUES('library-1', 'Local', 'local', 'E:/media', 'now', 'now')
            """
        )
        self.db.connection.execute(
            """
            INSERT INTO render_snapshots(
              snapshot_id, task_id, stage, created_at
            ) VALUES('snapshot-1', 'task-1', 'mix', 'now')
            """
        )
        self.db.connection.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def assertTableExists(self, table_name):
        found = self.db.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        self.assertIsNotNone(found, f"missing table: {table_name}")

    def _insert_library(self, library_id, purpose="task_specific"):
        self.db.connection.execute(
            """
            INSERT INTO libraries(
              library_id, name, kind, mount_path, purpose,
              created_at, updated_at
            ) VALUES(?, ?, 'local', ?, ?, 'now', 'now')
            """,
            (library_id, library_id, f"E:/{library_id}", purpose),
        )

    def _insert_asset_location(self, asset_id, library_id, relative_path, suffix=""):
        self.db.connection.execute(
            """
            INSERT OR IGNORE INTO assets(
              asset_id, media_kind, size_bytes, quick_fingerprint, created_at
            ) VALUES(?, 'video', 1, ?, 'now')
            """,
            (asset_id, asset_id),
        )
        self.db.connection.execute(
            """
            INSERT INTO asset_locations(
              asset_id, library_id, relative_path, mtime_ns, file_identity
            ) VALUES(?, ?, ?, 1, ?)
            """,
            (asset_id, library_id, relative_path, f"{asset_id}-{suffix}"),
        )

    def _configure_scope(self, relative_path=""):
        self.db.connection.execute(
            "INSERT INTO task_source_settings(task_id) VALUES('task-1')"
        )
        self.db.connection.execute(
            """
            INSERT INTO task_source_scopes(
              scope_id, task_id, library_id, role, relative_path,
              source_mode, active
            ) VALUES('guard-scope', 'task-1', 'library-1', 'primary', ?,
                     'primary', 1)
            """,
            (relative_path,),
        )

    def test_latest_migration_is_39_and_can_be_rerun(self):
        migration_018 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "018_task_source_boundary_guards.sql"
        )
        migration_019 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "019_configured_task_source_guards.sql"
        )
        migration_020 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "020_packaging_templates_and_guards.sql"
        )
        migration_021 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "021_confirmed_packaging_item_guards.sql"
        )
        migration_022 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "022_durable_job_queue.sql"
        )
        migration_023 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "023_harden_durable_job_execution.sql"
        )
        migration_024 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "024_job_request_cost_intent.sql"
        )
        migration_025 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "025_full_review_watch_gate.sql"
        )
        migration_027 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "027_model_profiles.sql"
        )
        migration_028 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "028_digital_human_source_binding.sql"
        )
        migration_029 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "029_personal_packaging_storage.sql"
        )
        migration_030 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "030_tts_voice_catalog.sql"
        )
        migration_031 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "031_agent_orchestration.sql"
        )
        migration_032 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "032_watch_playback_credit.sql"
        )
        migration_033 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "033_packaging_preview_and_catalog_check.sql"
        )
        migration_034 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "034_packaging_plan_origin.sql"
        )
        migration_035 = (
            Path(__file__).parents[1]
            / "media_catalog"
            / "migrations"
            / "035_candidate_versions_and_rework.sql"
        )
        migration_036 = migration_035.with_name("036_two_route_closure.sql")
        migration_037 = migration_035.with_name("037_job_controls.sql")
        migration_038 = migration_035.with_name("038_material_authorization_attestations.sql")
        migration_039 = migration_035.with_name("039_self_produced_assets.sql")

        self.assertNotIn(
            "configured_task_source_protect",
            migration_018.read_text(encoding="utf-8"),
            "published migration 018 must remain behaviorally unchanged",
        )
        self.assertTrue(migration_019.exists(), "migration 019 must exist")
        self.assertTrue(migration_020.exists(), "migration 020 must exist")
        self.assertTrue(migration_021.exists(), "migration 021 must exist")
        self.assertTrue(migration_022.exists(), "migration 022 must exist")
        self.assertTrue(migration_023.exists(), "migration 023 must exist")
        self.assertTrue(migration_024.exists(), "migration 024 must exist")
        self.assertTrue(migration_025.exists(), "migration 025 must exist")
        self.assertTrue(migration_027.exists(), "migration 027 must exist")
        self.assertTrue(migration_028.exists(), "migration 028 must exist")
        self.assertTrue(migration_029.exists(), "migration 029 must exist")
        self.assertTrue(migration_030.exists(), "migration 030 must exist")
        self.assertTrue(migration_031.exists(), "migration 031 must exist")
        self.assertTrue(migration_032.exists(), "migration 032 must exist")
        self.assertTrue(migration_033.exists(), "migration 033 must exist")
        self.assertTrue(migration_034.exists(), "migration 034 must exist")
        self.assertTrue(migration_035.exists(), "migration 035 must exist")
        self.assertTrue(migration_036.exists(), "migration 036 must exist")
        self.assertTrue(migration_037.exists(), "migration 037 must exist")
        self.assertTrue(migration_038.exists(), "migration 038 must exist")
        self.assertTrue(migration_039.exists(), "migration 039 must exist")
        self.db.connection.executescript(migration_019.read_text(encoding="utf-8"))
        self.db.connection.executescript(migration_020.read_text(encoding="utf-8"))
        self.db.connection.executescript(migration_021.read_text(encoding="utf-8"))

        version = self.db.connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        self.assertEqual(version, 39)
        self.db.migrate()
        self.assertIn("playback_credit_seconds", {
            row["name"] for row in self.db.connection.execute("PRAGMA table_info(review_watch_sessions)")
        })

    def test_locked_digital_human_source_is_the_only_out_of_scope_asset_allowed(self):
        self._insert_library("digital-source")
        self._insert_asset_location("digital", "digital-source", "xinjiang.mp4")
        self._insert_asset_location("other-digital", "digital-source", "other.mp4")
        self.db.connection.execute(
            """
            UPDATE tasks
            SET task_type='digital_human',
                brief_json='{"digital_human_source":{"asset_id":"digital"}}'
            WHERE task_id='task-1'
            """
        )
        self._configure_scope()

        self.db.connection.execute(
            """
            INSERT INTO task_assets(task_id, asset_id, role)
            VALUES('task-1', 'digital', 'digital_human')
            """
        )
        self.db.connection.execute(
            """
            INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
            VALUES('digital-card', 'task-1', 0, 'digital')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO task_assets(task_id, asset_id, role)
                VALUES('task-1', 'other-digital', 'digital_human')
                """
            )

    def test_model_profile_schema_supports_accounts_profiles_and_frozen_task_bindings(self):
        for table_name in (
            "model_provider_accounts",
            "model_profiles",
            "task_model_bindings",
        ):
            self.assertTableExists(table_name)

        self.db.connection.execute(
            """
            INSERT INTO model_provider_accounts(
              account_id,name,provider,base_url,credential_ref,settings_json
            ) VALUES('account-a','Gateway','new_api','http://127.0.0.1:3000',
                     'cutroom/provider/account-a','{}')
            """
        )
        self.db.connection.execute(
            """
            INSERT INTO model_profiles(
              profile_id,capability,display_name,account_id,model,settings_json,is_default
            ) VALUES('profile-a','general_text','A','account-a','model-a','{}',1)
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO model_profiles(
                  profile_id,capability,display_name,account_id,model,settings_json,is_default
                ) VALUES('profile-b','general_text','B','account-a','model-b','{}',1)
                """
            )
        self.db.connection.rollback()

    def test_packaging_template_schema_is_task_independent_and_guarded(self):
        self.assertTableExists("packaging_templates")
        columns = {
            row[1]
            for row in self.db.connection.execute(
                "PRAGMA table_info(packaging_templates)"
            )
        }
        self.assertTrue(
            {
                "template_id", "template_name", "route",
                "snapshot_json", "snapshot_digest",
            }
            <= columns
        )
        self.assertNotIn("task_id", columns)

    def test_first_sql_configuration_rejects_preexisting_out_of_scope_references(self):
        self._insert_library("other-park")
        self._insert_asset_location("unbound", "other-park", "secret.mp4")
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', 'unbound')"
        )
        self.db.connection.execute(
            """
            INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
            VALUES('legacy-card', 'task-2', 0, 'unbound')
            """
        )
        self.db.connection.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "existing task asset"):
            self.db.connection.execute(
                "INSERT INTO task_source_settings(task_id) VALUES('task-1')"
            )
        self.db.connection.rollback()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "existing scene card"):
            with self.db.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id, task_id, library_id, role, relative_path,
                      source_mode, active
                    ) VALUES('park-scope', 'task-2', 'library-1', 'primary',
                             'park', 'primary', 1)
                    """
                )
                connection.execute(
                    "INSERT INTO task_source_settings(task_id) VALUES('task-2')"
                )

        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_settings"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_scopes"
            ).fetchone()[0],
            0,
        )

    def test_configured_scope_sql_mutation_cannot_strand_existing_references(self):
        self._insert_library("primary-2")
        self._insert_asset_location("inside", "library-1", "park/inside.mp4")
        self._configure_scope("park")
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', 'inside')"
        )
        self.db.connection.execute(
            """
            INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
            VALUES('inside-card', 'task-1', 0, 'inside')
            """
        )
        self.db.connection.commit()

        statements = (
            "UPDATE task_source_scopes SET library_id='primary-2' "
            "WHERE scope_id='guard-scope'",
            "UPDATE task_source_scopes SET relative_path='other' "
            "WHERE scope_id='guard-scope'",
            "UPDATE task_source_scopes SET active=0 WHERE scope_id='guard-scope'",
            "DELETE FROM task_source_scopes WHERE scope_id='guard-scope'",
        )
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaisesRegex(
                sqlite3.IntegrityError, "configured task source"
            ):
                self.db.connection.execute(statement)
            self.db.connection.rollback()

        self.assertEqual(
            tuple(
                self.db.connection.execute(
                    """
                    SELECT library_id, relative_path, active
                    FROM task_source_scopes WHERE scope_id='guard-scope'
                    """
                ).fetchone()
            ),
            ("library-1", "park", 1),
        )

    def test_task_source_authorization_is_scoped_to_the_named_task(self):
        self._insert_asset_location("inside", "library-1", "park/inside.mp4")
        self._configure_scope("park")
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', 'inside')"
        )
        self.db.connection.execute(
            "INSERT INTO task_source_settings(task_id) VALUES('task-2')"
        )
        self.db.connection.execute(
            """
            INSERT INTO task_source_scopes(
              scope_id, task_id, library_id, role, relative_path,
              source_mode, active
            ) VALUES('other-guard-scope', 'task-2', 'library-1', 'primary',
                     'park', 'primary', 1)
            """
        )
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-2', 'inside')"
        )
        self.db.connection.commit()

        with self.db.transaction():
            with self.db.task_source_write_authorization("task-1"):
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "configured task source"
                ):
                    self.db.connection.execute(
                        """
                        UPDATE task_source_scopes SET active=0
                        WHERE scope_id='other-guard-scope'
                        """
                    )

        self.assertEqual(
            self.db.connection.execute(
                "SELECT active FROM task_source_scopes WHERE scope_id='other-guard-scope'"
            ).fetchone()[0],
            1,
        )

    def test_task_source_authorization_supports_nesting_and_clears_on_exit(self):
        self._insert_asset_location("inside", "library-1", "park/inside.mp4")
        self._configure_scope("park")
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', 'inside')"
        )
        self.db.connection.commit()

        with self.db.transaction():
            with self.db.task_source_write_authorization("task-1"):
                with self.db.task_source_write_authorization("task-1"):
                    self.db.connection.execute(
                        """
                        UPDATE task_source_scopes SET relative_path='park'
                        WHERE scope_id='guard-scope'
                        """
                    )
                self.db.connection.execute(
                    """
                    UPDATE task_source_scopes SET relative_path='park'
                    WHERE scope_id='guard-scope'
                    """
                )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "configured task source"):
            self.db.connection.execute(
                "UPDATE task_source_scopes SET active=0 WHERE scope_id='guard-scope'"
            )
        self.db.connection.rollback()

    def test_task_source_authorization_does_not_leak_after_exception(self):
        self._insert_asset_location("inside", "library-1", "park/inside.mp4")
        self._configure_scope("park")
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', 'inside')"
        )
        self.db.connection.commit()

        with self.assertRaisesRegex(RuntimeError, "stop"):
            with self.db.transaction():
                with self.db.task_source_write_authorization("task-1"):
                    raise RuntimeError("stop")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "configured task source"):
            self.db.connection.execute(
                "DELETE FROM task_source_scopes WHERE scope_id='guard-scope'"
            )
        self.db.connection.rollback()

    def test_task_source_authorization_requires_database_transaction_context(self):
        with self.assertRaisesRegex(RuntimeError, "transaction"):
            with self.db.task_source_write_authorization("task-1"):
                self.fail("authorization must not open outside db.transaction()")

    def test_task_source_authorization_is_local_to_its_database_connection(self):
        self._insert_asset_location("inside", "library-1", "park/inside.mp4")
        self._configure_scope("park")
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', 'inside')"
        )
        self.db.connection.commit()
        other = CatalogDatabase.open(self.db.path)
        try:
            other.migrate()
            with self.db.transaction():
                with self.db.task_source_write_authorization("task-1"):
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError, "configured task source"
                    ):
                        other.connection.execute(
                            """
                            UPDATE task_source_scopes SET active=0
                            WHERE scope_id='guard-scope'
                            """
                        )
                    other.connection.rollback()
        finally:
            other.close()

        self.assertEqual(
            self.db.connection.execute(
                "SELECT active FROM task_source_scopes WHERE scope_id='guard-scope'"
            ).fetchone()[0],
            1,
        )

    def test_scope_relative_path_sql_guards_reject_escapes_and_allow_safe_paths(self):
        unsafe_paths = (
            "../other-park",
            "park/../other-park",
            ".",
            "park/.",
            "/absolute",
            r"C:\other-park",
            r"\\server\share",
            "park//nested",
            "park\x00nested",
        )
        for index, relative_path in enumerate(unsafe_paths):
            with self.subTest(relative_path=repr(relative_path)), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.db.connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id, task_id, library_id, role, relative_path,
                      source_mode, active
                    ) VALUES(?, 'task-1', 'library-1', 'primary', ?, 'primary', 0)
                    """,
                    (f"unsafe-{index}", relative_path),
                )

        for scope_id, relative_path in (
            ("root", ""),
            ("nested-forward", "park/nested"),
            ("nested-backslash", r"park\nested"),
        ):
            self.db.connection.execute(
                """
                INSERT INTO task_source_scopes(
                  scope_id, task_id, library_id, role, relative_path,
                  source_mode, active
                ) VALUES(?, 'task-1', 'library-1', 'primary', ?, 'primary', 0)
                """,
                (scope_id, relative_path),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                UPDATE task_source_scopes SET relative_path='../escape'
                WHERE scope_id='nested-forward'
                """
            )

    def test_task_assets_sql_guard_enforces_active_scope_membership(self):
        self._insert_library("other-park")
        self._insert_library("packaging-lib", "packaging")
        self._insert_asset_location("inside", "library-1", r"\park\inside.mp4")
        self._insert_asset_location("outside", "library-1", r"parking\outside.mp4")
        self._insert_asset_location("unbound", "other-park", "secret.mp4")
        self._insert_asset_location("packaged", "packaging-lib", "logo.png")
        self._insert_asset_location("multi", "other-park", "wrong.mp4", "wrong")
        self._insert_asset_location(
            "multi", "library-1", "park/nested/right.mp4", "right"
        )
        self._configure_scope("park")

        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', 'inside')"
        )
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', 'multi')"
        )
        for asset_id in ("outside", "unbound", "packaged"):
            with self.subTest(asset_id=asset_id), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.db.connection.execute(
                    "INSERT INTO task_assets(task_id, asset_id) VALUES('task-1', ?)",
                    (asset_id,),
                )

        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-2', 'outside')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                UPDATE task_assets SET task_id='task-1'
                WHERE task_id='task-2' AND asset_id='outside'
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                UPDATE task_assets SET asset_id='outside'
                WHERE task_id='task-1' AND asset_id='inside'
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                "UPDATE task_source_scopes SET relative_path='///' WHERE scope_id='guard-scope'"
            )
        self.db.connection.execute(
            "INSERT INTO tasks(task_id, title) VALUES('task-3', 'Root scope task')"
        )
        self.db.connection.execute(
            "INSERT INTO task_source_settings(task_id) VALUES('task-3')"
        )
        self.db.connection.execute(
            """
            INSERT INTO task_source_scopes(
              scope_id, task_id, library_id, role, relative_path,
              source_mode, active
            ) VALUES('root-scope', 'task-3', 'library-1', 'primary', '',
                     'primary', 1)
            """
        )
        self.db.connection.execute(
            "INSERT INTO task_assets(task_id, asset_id) VALUES('task-3', 'outside')"
        )

    def test_scene_cards_sql_guard_allows_null_and_legacy_tasks_only(self):
        self._insert_library("other-park")
        self._insert_asset_location("inside", "library-1", "park/inside.mp4")
        self._insert_asset_location("unbound", "other-park", "secret.mp4")
        self._configure_scope("park")

        self.db.connection.execute(
            """
            INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
            VALUES('null-card', 'task-1', 0, NULL)
            """
        )
        self.db.connection.execute(
            """
            INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
            VALUES('inside-card', 'task-1', 1, 'inside')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
                VALUES('bad-card', 'task-1', 2, 'unbound')
                """
            )

        self.db.connection.execute(
            """
            INSERT INTO scene_cards(card_id, task_id, idx, asset_id)
            VALUES('legacy-card', 'task-2', 0, 'unbound')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                "UPDATE scene_cards SET asset_id='unbound' WHERE card_id='null-card'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                "UPDATE scene_cards SET task_id='task-1' WHERE card_id='legacy-card'"
            )
        self.db.connection.execute(
            "UPDATE scene_cards SET asset_id='inside' WHERE card_id='null-card'"
        )

    def test_workbench_tables_and_job_progress_columns_exist(self):
        tables = {
            row[0]
            for row in self.db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(
            {
                "task_source_scopes",
                "task_source_snapshots",
                "task_stage_states",
                "review_watch_progress",
                "review_watch_ranges",
                "model_provider_settings",
                "rough_cut_edits",
                "job_payloads",
            }
            <= tables
        )

        job_columns = {
            row[1] for row in self.db.connection.execute("PRAGMA table_info(jobs)")
        }
        self.assertTrue(
            {
                "task_id",
                "progress_current",
                "progress_total",
                "phase_label",
                "cancel_requested",
                "cancel_reason",
                "started_at",
                "finished_at",
            }
            <= job_columns
        )

        required_columns = {
            "task_source_scopes": {"task_id", "library_id", "role", "relative_path"},
            "task_source_snapshots": {"task_id", "scope_id", "summary_json"},
            "task_stage_states": {"task_id", "stage_key", "status"},
            "review_watch_progress": {
                "task_id",
                "snapshot_id",
                "watched_seconds",
                "duration_seconds",
            },
            "review_watch_ranges": {
                "task_id",
                "snapshot_id",
                "start_sec",
                "end_sec",
            },
            "model_provider_settings": {
                "capability",
                "provider",
                "model",
                "credential_ref",
            },
            "rough_cut_edits": {
                "task_id",
                "before_json",
                "after_json",
                "impact_digest",
                "status",
            },
            "job_payloads": {
                "job_id",
                "payload_json",
                "cost_status",
                "provider_request_id",
            },
        }
        for table_name, expected_columns in required_columns.items():
            with self.subTest(table=table_name):
                actual_columns = {
                    row[1]
                    for row in self.db.connection.execute(
                        f"PRAGMA table_info({table_name})"
                    )
                }
                self.assertTrue(expected_columns <= actual_columns)

    def test_task_has_at_most_one_active_primary_and_keeps_primary_history(self):
        self.assertTableExists("task_source_scopes")
        self.db.connection.execute(
            """
            INSERT INTO task_source_scopes(
              scope_id, task_id, library_id, role, relative_path
            ) VALUES('scope-1', 'task-1', 'library-1', 'primary', '')
            """
        )
        self.db.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO task_source_scopes(
                  scope_id, task_id, library_id, role, relative_path
                ) VALUES('scope-2', 'task-1', 'library-1', 'primary', 'other')
                """
            )
        self.db.connection.rollback()
        self.db.connection.execute(
            """
            UPDATE task_source_scopes
            SET active=0, retired_at=datetime('now')
            WHERE scope_id='scope-1'
            """
        )
        self.db.connection.execute(
            """
            INSERT INTO task_source_scopes(
              scope_id, task_id, library_id, role, relative_path
            ) VALUES('scope-2', 'task-1', 'library-1', 'primary', 'other')
            """
        )
        self.assertEqual(
            self.db.connection.execute(
                """
                SELECT COUNT(*) FROM task_source_scopes
                WHERE task_id='task-1' AND source_mode='primary'
                """
            ).fetchone()[0],
            2,
        )

    def test_source_scope_rejects_library_purpose_mismatch_and_active_overlap(self):
        self.db.connection.execute(
            """
            INSERT INTO libraries(
              library_id, name, kind, mount_path, purpose,
              created_at, updated_at
            ) VALUES('packaging-lib', '包装', 'local', 'E:/packaging',
                     'packaging', 'now', 'now')
            """
        )
        self.db.connection.execute(
            """
            INSERT INTO libraries(
              library_id, name, kind, mount_path, purpose,
              created_at, updated_at
            ) VALUES('generic-lib', '通用', 'local', 'E:/generic',
                     'generic', 'now', 'now')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO task_source_scopes(
                  scope_id, task_id, library_id, role, source_mode
                ) VALUES('bad-packaging', 'task-1', 'packaging-lib',
                         'supplementary', 'selected_generic')
                """
            )

        self.db.connection.execute(
            """
            INSERT INTO task_source_scopes(
              scope_id, task_id, library_id, role, source_mode
            ) VALUES('selected', 'task-1', 'generic-lib',
                     'supplementary', 'selected_generic')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO task_source_scopes(
                  scope_id, task_id, library_id, role, source_mode
                ) VALUES('default-overlap', 'task-1', 'generic-lib',
                         'supplementary', 'default_generic')
                """
            )

    def test_watch_seconds_must_be_nonnegative(self):
        self.assertTableExists("review_watch_progress")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO review_watch_progress(
                  task_id, snapshot_id, watched_seconds, duration_seconds
                ) VALUES('task-1', 'snapshot-1', -0.1, 10)
                """
            )

    def test_each_workbench_stage_key_is_writable(self):
        self.assertTableExists("task_stage_states")
        for stage_key in STAGE_KEYS:
            try:
                self.db.connection.execute(
                    """
                    INSERT INTO task_stage_states(task_id, stage_key, status)
                    VALUES('task-1', ?, 'not_started')
                    """,
                    (stage_key,),
                )
            except sqlite3.IntegrityError as error:
                self.fail(f"legal stage_key rejected: {stage_key}: {error}")
        self.assertEqual(
            {
                row[0]
                for row in self.db.connection.execute(
                    "SELECT stage_key FROM task_stage_states WHERE task_id='task-1'"
                )
            },
            set(STAGE_KEYS),
        )

    def test_each_workbench_stage_status_is_writable(self):
        self.assertTableExists("task_stage_states")
        try:
            self.db.connection.execute(
                """
                INSERT INTO task_stage_states(task_id, stage_key, status)
                VALUES('task-1', 'create_preflight', 'not_started')
                """
            )
        except sqlite3.IntegrityError as error:
            self.fail(f"legal initial stage state rejected: {error}")
        for status in STAGE_STATUSES:
            try:
                self.db.connection.execute(
                    """
                    UPDATE task_stage_states SET status=?
                    WHERE task_id='task-1' AND stage_key='create_preflight'
                    """,
                    (status,),
                )
            except sqlite3.IntegrityError as error:
                self.fail(f"legal stage status rejected: {status}: {error}")
            actual = self.db.connection.execute(
                """
                SELECT status FROM task_stage_states
                WHERE task_id='task-1' AND stage_key='create_preflight'
                """
            ).fetchone()[0]
            self.assertEqual(actual, status)

    def test_stage_enums_reject_old_and_adjacent_runtime_values(self):
        self.assertTableExists("task_stage_states")
        for stage_key in ("source_scope", "not_a_stage"):
            with self.subTest(stage_key=stage_key), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.db.connection.execute(
                    """
                    INSERT INTO task_stage_states(task_id, stage_key, status)
                    VALUES('task-1', ?, 'not_started')
                    """,
                    (stage_key,),
                )
        for status in ("pending", "running", "failed", "cancelled"):
            with self.subTest(status=status), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.db.connection.execute(
                    """
                    INSERT INTO task_stage_states(task_id, stage_key, status)
                    VALUES('task-1', 'create_preflight', ?)
                    """,
                    (status,),
                )

    def test_model_provider_settings_store_only_a_credential_reference(self):
        columns = {
            row[1]
            for row in self.db.connection.execute(
                "PRAGMA table_info(model_provider_settings)"
            )
        }
        self.assertIn("credential_ref", columns)
        self.assertTrue(
            {
                "settings_json",
                "api_key",
                "api_secret",
                "access_token",
                "secret_key",
            }.isdisjoint(columns)
        )
        self.db.connection.execute(
            """
            INSERT INTO model_provider_settings(
              capability, provider, model, credential_ref
            ) VALUES('storyboard', 'volcengine', 'doubao',
                     'env://WORKBENCH_MODEL_API_KEY')
            """
        )
        actual = self.db.connection.execute(
            """
            SELECT credential_ref FROM model_provider_settings
            WHERE capability='storyboard'
            """
        ).fetchone()[0]
        self.assertEqual(actual, "env://WORKBENCH_MODEL_API_KEY")
        with self.assertRaises(sqlite3.OperationalError):
            self.db.connection.execute(
                """
                INSERT INTO model_provider_settings(
                  capability, provider, model, api_key
                ) VALUES('script', 'volcengine', 'doubao', 'not-a-real-secret')
                """
            )

    def test_task_scoped_foreign_keys_reject_cross_task_bindings(self):
        self.db.connection.execute(
            """
            INSERT INTO task_source_scopes(
              scope_id, task_id, library_id, role, relative_path
            ) VALUES('scope-task-1', 'task-1', 'library-1', 'primary', '')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO task_source_snapshots(
                  snapshot_id, task_id, scope_id, summary_json
                ) VALUES('source-snapshot-cross', 'task-2', 'scope-task-1', '{}')
                """
            )
        self.db.connection.execute(
            """
            INSERT INTO task_source_snapshots(
              snapshot_id, task_id, scope_id, summary_json
            ) VALUES('source-snapshot-ok', 'task-1', 'scope-task-1', '{}')
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO review_watch_progress(
                  task_id, snapshot_id, watched_seconds, duration_seconds
                ) VALUES('task-2', 'snapshot-1', 1, 10)
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO review_watch_ranges(
                  task_id, snapshot_id, start_sec, end_sec
                ) VALUES('task-2', 'snapshot-1', 0, 1)
                """
            )

    def test_provider_request_id_is_unique(self):
        self.assertTableExists("job_payloads")
        for job_id in ("job-1", "job-2"):
            self.db.connection.execute(
                """
                INSERT INTO jobs(job_id, job_type, status, created_at, updated_at)
                VALUES(?, 'model', 'pending', 'now', 'now')
                """,
                (job_id,),
            )
        self.db.connection.execute(
            """
            INSERT INTO job_payloads(
              job_id, payload_json, cost_status, provider_request_id
            ) VALUES('job-1', '{}', 'unknown', 'provider-request-1')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                """
                INSERT INTO job_payloads(
                  job_id, payload_json, cost_status, provider_request_id
                ) VALUES('job-2', '{}', 'unknown', 'provider-request-1')
                """
            )


if __name__ == "__main__":
    unittest.main()
