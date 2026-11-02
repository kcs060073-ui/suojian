import inspect
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase


class DatabaseTests(unittest.TestCase):
    def test_v035_upgrade_backfills_one_verified_legacy_candidate_idempotently(self):
        from media_catalog.snapshots import (
            record_qa_review_snapshot,
            record_render_dependency_snapshot,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migration_dir = root / "migrations"
            migration_dir.mkdir()
            source_dir = Path(__file__).parents[1] / "media_catalog" / "migrations"
            for source in sorted(source_dir.glob("0*.sql")):
                if int(source.name.split("_", 1)[0]) <= 34:
                    shutil.copy2(source, migration_dir / source.name)

            db = CatalogDatabase.open(root / "catalog.sqlite")
            try:
                db.migrate(migration_dir=migration_dir)
                video = root / "legacy.mp4"
                video.write_bytes(b"verified-legacy-video")
                with db.transaction() as connection:
                    connection.execute(
                        "INSERT INTO tasks(task_id,title,status) VALUES('legacy','Legacy','draft')"
                    )
                render = record_render_dependency_snapshot(
                    db,
                    "legacy",
                    {
                        "output_path": str(video),
                        "dependency_digest": "legacy-digest",
                        "dependency_snapshot": {"files": []},
                        "plan_path": str(root / "legacy.plan.json"),
                        "ass_path": None,
                        "packaging": {"plan_id": "legacy-plan"},
                    },
                )
                report = {
                    "report_id": "legacy-qa",
                    "task_id": "legacy",
                    "video_path": str(video.resolve()),
                    "status": "passed",
                    "issue_count": 0,
                    "blocking_count": 0,
                    "issues": [],
                    "video_spec": {"duration": 12.0},
                }
                with db.transaction() as connection:
                    connection.execute(
                        """INSERT INTO qa_reports(
                          report_id,task_id,video_path,status,issue_count,blocking_count,report_json
                        ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            "legacy-qa", "legacy", str(video.resolve()), "passed",
                            0, 0, json.dumps(report),
                        ),
                    )
                    connection.execute(
                        """INSERT INTO publish_records(record_id,task_id,platform,status)
                        VALUES('legacy-publish','legacy','视频号','cleared')"""
                    )
                qa_snapshot = record_qa_review_snapshot(db, "legacy", render, report)

                shutil.copy2(
                    source_dir / "035_candidate_versions_and_rework.sql",
                    migration_dir / "035_candidate_versions_and_rework.sql",
                )
                db.migrate(migration_dir=migration_dir)
                db.migrate(migration_dir=migration_dir)

                candidate = db.connection.execute(
                    "SELECT * FROM candidate_versions WHERE task_id='legacy'"
                ).fetchone()
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate["mix_snapshot_id"], render["mix_snapshot_id"])
                self.assertEqual(candidate["qa_snapshot_id"], qa_snapshot["qa_snapshot_id"])
                self.assertEqual(candidate["qa_report_id"], "legacy-qa")
                self.assertEqual(
                    db.connection.execute(
                        "SELECT candidate_id FROM publish_records WHERE record_id='legacy-publish'"
                    ).fetchone()[0],
                    candidate["candidate_id"],
                )
                self.assertEqual(
                    db.connection.execute(
                        "SELECT COUNT(*) FROM candidate_versions WHERE task_id='legacy'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                db.close()

    def test_transaction_commits_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            try:
                db.migrate()
                with db.transaction() as connection:
                    connection.execute(
                        "INSERT INTO tasks(task_id, title) VALUES('normal', '正常事务')"
                    )

                self.assertEqual(
                    db.connection.execute(
                        "SELECT title FROM tasks WHERE task_id='normal'"
                    ).fetchone()[0],
                    "正常事务",
                )
            finally:
                db.close()

    def test_immediate_transaction_holds_writer_lock_until_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            db = CatalogDatabase.open(path)
            competing = CatalogDatabase.open(path)
            try:
                db.migrate()
                competing.connection.execute("PRAGMA busy_timeout=0")

                with db.transaction(immediate=True):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        competing.connection.execute(
                            "INSERT INTO tasks(task_id, title) VALUES('blocked', '被锁')"
                        )
                competing.connection.rollback()

                competing.connection.execute(
                    "INSERT INTO tasks(task_id, title) VALUES('after', '锁后成功')"
                )
                competing.connection.commit()
                self.assertEqual(
                    db.connection.execute(
                        "SELECT title FROM tasks WHERE task_id='after'"
                    ).fetchone()[0],
                    "锁后成功",
                )
            finally:
                competing.close()
                db.close()

    def test_immediate_transaction_rolls_back_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            try:
                db.migrate()
                with self.assertRaisesRegex(RuntimeError, "forced rollback"):
                    with db.transaction(immediate=True) as connection:
                        connection.execute(
                            "INSERT INTO tasks(task_id, title) VALUES('rolled-back', '回滚')"
                        )
                        raise RuntimeError("forced rollback")

                self.assertIsNone(
                    db.connection.execute(
                        "SELECT 1 FROM tasks WHERE task_id='rolled-back'"
                    ).fetchone()
                )
            finally:
                db.close()

    def test_migration_is_idempotent_and_enables_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            try:
                db.migrate()
                db.migrate()
                tables = {
                    row[0]
                    for row in db.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "libraries",
                        "assets",
                        "asset_locations",
                        "analysis_runs",
                        "jobs",
                        "job_controls",
                        "storyboard_feedback",
                        "tts_voice_catalog",
                        "tts_voice_catalog_state",
                    }
                    <= tables
                )
                self.assertEqual(
                    db.connection.execute("PRAGMA foreign_keys").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM schema_migrations WHERE version=1"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    [
                        row[0]
                        for row in db.connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    ],
                    list(range(1, 40)),
                )
                task_columns = {
                    row[1]: row
                    for row in db.connection.execute("PRAGMA table_info(tasks)")
                }
                self.assertIn("orchestration_mode", task_columns)
                artifact_columns = {
                    row[1]
                    for row in db.connection.execute(
                        "PRAGMA table_info(agent_artifacts)"
                    )
                }
                self.assertTrue(
                    {
                        "artifact_id",
                        "task_id",
                        "artifact_type",
                        "schema_version",
                        "content_json",
                        "content_digest",
                        "input_digest",
                        "source_agent",
                        "source_model_label",
                        "status",
                        "created_at",
                        "confirmed_at",
                    }
                    <= artifact_columns
                )
                catalog_pk = {
                    row[1]: row[5]
                    for row in db.connection.execute(
                        "PRAGMA table_info(tts_voice_catalog)"
                    )
                }
                self.assertEqual(
                    {
                        key: catalog_pk[key]
                        for key in ("account_id", "resource_id", "voice_id")
                    },
                    {"account_id": 1, "resource_id": 2, "voice_id": 3},
                )
                catalog_foreign_keys = list(
                    db.connection.execute(
                        "PRAGMA foreign_key_list(tts_voice_catalog)"
                    )
                )
                self.assertTrue(
                    any(
                        row[2] == "model_provider_accounts"
                        and row[3] == "account_id"
                        and row[6].upper() == "CASCADE"
                        for row in catalog_foreign_keys
                    )
                )
            finally:
                db.close()

    def test_v030_upgrade_defaults_existing_tasks_to_standalone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migration_dir = root / "migrations"
            migration_dir.mkdir()
            source_dir = Path(__file__).parents[1] / "media_catalog" / "migrations"
            for source in sorted(source_dir.glob("0*.sql")):
                if int(source.name.split("_", 1)[0]) <= 30:
                    shutil.copy2(source, migration_dir / source.name)

            db = CatalogDatabase.open(root / "catalog.sqlite")
            try:
                db.migrate(migration_dir=migration_dir)
                with db.transaction() as connection:
                    connection.execute(
                        "INSERT INTO tasks(task_id, title) VALUES(?, ?)",
                        ("legacy-task", "历史任务"),
                    )
                migration_31 = source_dir / "031_agent_orchestration.sql"
                if not migration_31.is_file():
                    self.fail("migration 031_agent_orchestration.sql must exist")
                shutil.copy2(migration_31, migration_dir / migration_31.name)
                db.migrate(migration_dir=migration_dir)

                row = db.connection.execute(
                    "SELECT orchestration_mode FROM tasks WHERE task_id=?",
                    ("legacy-task",),
                ).fetchone()
                self.assertEqual(row["orchestration_mode"], "standalone")
            finally:
                db.close()

    def test_legacy_v014_upgrade_preserves_distinct_subdirectory_scopes_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migration_dir = root / "migrations"
            migration_dir.mkdir()
            source_dir = Path(__file__).parents[1] / "media_catalog" / "migrations"
            for source in sorted(source_dir.glob("0*.sql")):
                if int(source.name.split("_", 1)[0]) <= 14:
                    shutil.copy2(source, migration_dir / source.name)

            db = CatalogDatabase.open(root / "catalog.sqlite")
            try:
                db.migrate(migration_dir=migration_dir)
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO libraries(
                          library_id, name, kind, mount_path, created_at, updated_at
                        ) VALUES('legacy-lib', '旧素材库', 'local', 'E:/media',
                                 datetime('now'), datetime('now'))
                        """
                    )
                    connection.execute(
                        "INSERT INTO tasks(task_id, title) VALUES('legacy-task', '旧任务')"
                    )
                    connection.execute(
                        """
                        INSERT INTO task_source_scopes(
                          scope_id, task_id, library_id, role, relative_path
                        ) VALUES('legacy-scope-a', 'legacy-task', 'legacy-lib',
                                 'supplementary', 'park-a')
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO task_source_scopes(
                          scope_id, task_id, library_id, role, relative_path
                        ) VALUES('legacy-scope-b', 'legacy-task', 'legacy-lib',
                                 'supplementary', 'park-b')
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO task_source_snapshots(
                          snapshot_id, task_id, scope_id, summary_json
                        ) VALUES('legacy-snapshot', 'legacy-task', 'legacy-scope-a', '{}')
                        """
                    )

                shutil.copy2(
                    source_dir / "015_task_sources.sql",
                    migration_dir / "015_task_sources.sql",
                )
                try:
                    db.migrate(migration_dir=migration_dir)
                except sqlite3.IntegrityError as error:
                    self.fail(
                        "v014 migration must preserve distinct supplementary "
                        f"subdirectory scopes: {error}"
                    )
                db.migrate(migration_dir=migration_dir)

                scopes = db.connection.execute(
                    """
                    SELECT relative_path, source_mode, active
                    FROM task_source_scopes
                    WHERE task_id='legacy-task'
                    ORDER BY relative_path
                    """
                ).fetchall()
                self.assertEqual(
                    [tuple(scope) for scope in scopes],
                    [
                        ("park-a", "selected_generic", 1),
                        ("park-b", "selected_generic", 1),
                    ],
                )
                self.assertEqual(
                    db.connection.execute(
                        """
                        SELECT COUNT(*) FROM task_source_snapshots
                        WHERE snapshot_id='legacy-snapshot'
                        """
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.connection.execute(
                        """
                        SELECT COUNT(*) FROM schema_migrations WHERE version=15
                        """
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    tuple(
                        db.connection.execute(
                            """
                            SELECT include_default_generic, revision
                            FROM task_source_settings
                            WHERE task_id='legacy-task'
                            """
                        ).fetchone()
                    ),
                    (0, 1),
                )
            finally:
                db.close()

    def test_old_015_upgrade_to_016_rebuilds_scope_index_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migration_dir = root / "migrations"
            migration_dir.mkdir()
            source_dir = Path(__file__).parents[1] / "media_catalog" / "migrations"
            for source in sorted(source_dir.glob("0*.sql")):
                if int(source.name.split("_", 1)[0]) <= 14:
                    shutil.copy2(source, migration_dir / source.name)

            current_015 = (source_dir / "015_task_sources.sql").read_text(
                encoding="utf-8"
            )
            old_015 = current_015.replace(
                "ON task_source_scopes(task_id, library_id, relative_path)",
                "ON task_source_scopes(task_id, library_id)",
            )
            self.assertNotEqual(old_015, current_015)
            (migration_dir / "015_task_sources.sql").write_text(
                old_015,
                encoding="utf-8",
            )

            db = CatalogDatabase.open(root / "catalog.sqlite")
            try:
                db.migrate(migration_dir=migration_dir)
                self.assertEqual(
                    db.connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    15,
                )
                self.assertEqual(
                    [
                        row[2]
                        for row in db.connection.execute(
                            "PRAGMA index_info(idx_task_source_scopes_one_active_library)"
                        )
                    ],
                    ["task_id", "library_id"],
                )
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO libraries(
                          library_id, name, kind, mount_path, purpose,
                          created_at, updated_at
                        ) VALUES('generic-lib', '通用素材库', 'local', 'E:/media',
                                 'generic', datetime('now'), datetime('now'))
                        """
                    )
                    connection.execute(
                        "INSERT INTO tasks(task_id, title) VALUES('task-1', '任务')"
                    )
                    connection.execute(
                        """
                        INSERT INTO task_source_scopes(
                          scope_id, task_id, library_id, role, relative_path
                        ) VALUES('scope-a', 'task-1', 'generic-lib',
                                 'supplementary', 'park-a')
                        """
                    )

                migration_016 = source_dir / "016_task_source_scope_indexes.sql"
                self.assertTrue(migration_016.exists(), "migration 016 must exist")
                shutil.copy2(migration_016, migration_dir / migration_016.name)
                db.migrate(migration_dir=migration_dir)
                db.migrate(migration_dir=migration_dir)

                db.connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id, task_id, library_id, role, relative_path
                    ) VALUES('scope-b', 'task-1', 'generic-lib',
                             'supplementary', 'park-b')
                    """
                )
                self.assertEqual(
                    [
                        row[2]
                        for row in db.connection.execute(
                            "PRAGMA index_info(idx_task_source_scopes_one_active_library)"
                        )
                    ],
                    ["task_id", "library_id", "relative_path"],
                )
                self.assertEqual(
                    [
                        row[0]
                        for row in db.connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    ],
                    list(range(1, 17)),
                )
            finally:
                db.close()

    def test_failed_migration_is_atomic_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migration_dir = root / "migrations"
            migration_dir.mkdir()
            (migration_dir / "001_base.sql").write_text(
                "CREATE TABLE stable(value TEXT);",
                encoding="utf-8",
            )
            db = CatalogDatabase.open(root / "catalog.sqlite")
            try:
                self.assertIn(
                    "migration_dir",
                    inspect.signature(db.migrate).parameters,
                    "migrate must support an injectable migration directory",
                )
                db.migrate(migration_dir=migration_dir)

                broken = migration_dir / "002_broken.sql"
                broken.write_text(
                    """
                    ALTER TABLE stable ADD COLUMN added TEXT;
                    CREATE TABLE transient(value TEXT);
                    THIS IS NOT SQL;
                    """,
                    encoding="utf-8",
                )
                with self.assertRaises(sqlite3.OperationalError):
                    db.migrate(migration_dir=migration_dir)

                self.assertEqual(
                    [row[1] for row in db.connection.execute("PRAGMA table_info(stable)")],
                    ["value"],
                )
                self.assertIsNone(
                    db.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transient'"
                    ).fetchone()
                )
                self.assertEqual(
                    [
                        row[0]
                        for row in db.connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    ],
                    [1],
                )

                broken.write_text(
                    "ALTER TABLE stable ADD COLUMN added TEXT;",
                    encoding="utf-8",
                )
                db.migrate(migration_dir=migration_dir)
                self.assertEqual(
                    [row[1] for row in db.connection.execute("PRAGMA table_info(stable)")],
                    ["value", "added"],
                )
                self.assertEqual(
                    [
                        row[0]
                        for row in db.connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    ],
                    [1, 2],
                )
            finally:
                db.close()

    def test_v017_upgrade_rejects_unsafe_scope_then_accepts_legal_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migration_dir = root / "migrations"
            migration_dir.mkdir()
            source_dir = Path(__file__).parents[1] / "media_catalog" / "migrations"
            for source in sorted(source_dir.glob("0*.sql")):
                if int(source.name.split("_", 1)[0]) <= 17:
                    shutil.copy2(source, migration_dir / source.name)

            db = CatalogDatabase.open(root / "catalog.sqlite")
            try:
                db.migrate(migration_dir=migration_dir)
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO libraries(
                          library_id, name, kind, mount_path, purpose,
                          created_at, updated_at
                        ) VALUES('primary', '园区库', 'local', 'E:/media',
                                 'task_specific', 'now', 'now')
                        """
                    )
                    connection.execute(
                        "INSERT INTO tasks(task_id, title) VALUES('task-1', '任务')"
                    )
                    connection.execute(
                        """
                        INSERT INTO task_source_scopes(
                          scope_id, task_id, library_id, role, relative_path,
                          source_mode, active
                        ) VALUES('unsafe', 'task-1', 'primary', 'primary',
                                 '../other-park', 'primary', 0)
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO task_source_scopes(
                          scope_id, task_id, library_id, role, relative_path,
                          source_mode, active
                        ) VALUES('safe-a', 'task-1', 'primary', 'primary',
                                 'park/a', 'primary', 0)
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO task_source_scopes(
                          scope_id, task_id, library_id, role, relative_path,
                          source_mode, active
                        ) VALUES('safe-b', 'task-1', 'primary', 'primary',
                                 'park/b', 'primary', 0)
                        """
                    )

                migration_018 = source_dir / "018_task_source_boundary_guards.sql"
                self.assertTrue(migration_018.exists(), "migration 018 must exist")
                shutil.copy2(migration_018, migration_dir / migration_018.name)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.migrate(migration_dir=migration_dir)
                self.assertEqual(
                    db.connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    17,
                )

                db.connection.execute(
                    "UPDATE task_source_scopes SET relative_path='park/c' WHERE scope_id='unsafe'"
                )
                db.connection.commit()
                db.migrate(migration_dir=migration_dir)
                db.migrate(migration_dir=migration_dir)
                self.assertEqual(
                    db.connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    18,
                )
                self.assertEqual(
                    {
                        row[0]
                        for row in db.connection.execute(
                            "SELECT relative_path FROM task_source_scopes"
                        )
                    },
                    {"park/a", "park/b", "park/c"},
                )
            finally:
                db.close()

    def test_v018_upgrade_installs_configured_scope_guards_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migration_dir = root / "migrations"
            migration_dir.mkdir()
            source_dir = Path(__file__).parents[1] / "media_catalog" / "migrations"
            for source in sorted(source_dir.glob("0*.sql")):
                if int(source.name.split("_", 1)[0]) <= 18:
                    shutil.copy2(source, migration_dir / source.name)

            db = CatalogDatabase.open(root / "catalog.sqlite")
            try:
                db.migrate(migration_dir=migration_dir)
                self.assertEqual(
                    db.connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    18,
                )
                self.assertEqual(
                    db.connection.execute(
                        """
                        SELECT COUNT(*) FROM sqlite_master
                        WHERE type='trigger'
                          AND name LIKE 'configured_task_source_protect_%'
                        """
                    ).fetchone()[0],
                    0,
                    "a database recorded at v018 must not rely on edited v018 SQL",
                )
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO libraries(
                          library_id, name, kind, mount_path, purpose,
                          created_at, updated_at
                        ) VALUES('primary', '园区素材', 'local', 'E:/primary',
                                 'task_specific', 'now', 'now')
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO libraries(
                          library_id, name, kind, mount_path, purpose,
                          created_at, updated_at
                        ) VALUES('primary-2', '其他园区素材', 'local', 'E:/primary-2',
                                 'task_specific', 'now', 'now')
                        """
                    )
                    connection.execute(
                        "INSERT INTO tasks(task_id, title) VALUES('task-1', '任务')"
                    )
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('inside', 'video', 1, 'inside', 'now')
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO asset_locations(
                          asset_id, library_id, relative_path, mtime_ns, file_identity
                        ) VALUES('inside', 'primary', 'park/inside.mp4', 1, 'inside')
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO task_source_scopes(
                          scope_id, task_id, library_id, role, relative_path,
                          source_mode, active
                        ) VALUES('guard-scope', 'task-1', 'primary', 'primary',
                                 'park', 'primary', 1)
                        """
                    )
                    connection.execute(
                        "INSERT INTO task_source_settings(task_id) VALUES('task-1')"
                    )
                    connection.execute(
                        """
                        INSERT INTO task_assets(task_id, asset_id)
                        VALUES('task-1', 'inside')
                        """
                    )

                migration_019 = source_dir / "019_configured_task_source_guards.sql"
                self.assertTrue(migration_019.exists(), "migration 019 must exist")
                shutil.copy2(migration_019, migration_dir / migration_019.name)
                db.migrate(migration_dir=migration_dir)
                db.migrate(migration_dir=migration_dir)

                self.assertEqual(
                    db.connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    19,
                )
                self.assertEqual(
                    {
                        row[0]
                        for row in db.connection.execute(
                            """
                            SELECT name FROM sqlite_master
                            WHERE type='trigger'
                              AND name LIKE 'configured_task_source_protect_%'
                            """
                        )
                    },
                    {
                        "configured_task_source_protect_update",
                        "configured_task_source_protect_delete",
                    },
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "configured task source"
                ):
                    db.connection.execute(
                        """
                        UPDATE task_source_scopes SET library_id='primary-2'
                        WHERE scope_id='guard-scope'
                        """
                    )
                db.connection.rollback()
                self.assertEqual(
                    tuple(
                        db.connection.execute(
                            """
                            SELECT library_id, active FROM task_source_scopes
                            WHERE scope_id='guard-scope'
                            """
                        ).fetchone()
                    ),
                    ("primary", 1),
                )
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
