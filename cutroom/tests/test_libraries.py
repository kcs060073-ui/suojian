import tempfile
import unittest
import sqlite3
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.libraries import (
    register_library,
    resolve_location,
    update_mount,
)


class LibraryTests(unittest.TestCase):
    def test_active_scope_library_purpose_cannot_be_changed_by_sql_or_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            try:
                db.migrate()
                db.connection.execute(
                    "INSERT INTO tasks(task_id, title) VALUES('task-1', 'Task')"
                )
                register_library(
                    db,
                    "primary",
                    "园区库",
                    "local",
                    str(Path(tmp) / "primary"),
                    purpose="task_specific",
                )
                register_library(
                    db,
                    "generic",
                    "通用库",
                    "local",
                    str(Path(tmp) / "generic"),
                    purpose="generic",
                )
                db.connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id, task_id, library_id, role, source_mode, active
                    ) VALUES('primary-scope', 'task-1', 'primary', 'primary',
                             'primary', 1)
                    """
                )
                db.connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id, task_id, library_id, role, source_mode, active
                    ) VALUES('generic-scope', 'task-1', 'generic', 'supplementary',
                             'selected_generic', 1)
                    """
                )
                db.connection.commit()

                with self.assertRaises(sqlite3.IntegrityError):
                    db.connection.execute(
                        "UPDATE libraries SET purpose='packaging' WHERE library_id='generic'"
                    )
                db.connection.rollback()
                with self.assertRaises(sqlite3.IntegrityError):
                    register_library(
                        db,
                        "primary",
                        "园区库",
                        "local",
                        str(Path(tmp) / "primary"),
                        purpose="generic",
                    )

                self.assertEqual(
                    db.connection.execute(
                        "SELECT purpose FROM libraries WHERE library_id='primary'"
                    ).fetchone()[0],
                    "task_specific",
                )
                self.assertEqual(
                    db.connection.execute(
                        "SELECT purpose FROM libraries WHERE library_id='generic'"
                    ).fetchone()[0],
                    "generic",
                )
            finally:
                db.close()

    def test_library_purpose_and_explicit_default_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            try:
                db.migrate()
                register_library(
                    db,
                    "generic",
                    "通用镜头",
                    "local",
                    str(Path(tmp) / "generic"),
                    purpose="generic",
                    is_default_generic=True,
                )
                row = db.connection.execute(
                    """
                    SELECT purpose, is_default_generic FROM libraries
                    WHERE library_id='generic'
                    """
                ).fetchone()
                self.assertEqual(tuple(row), ("generic", 1))

                with self.assertRaisesRegex(ValueError, "默认通用"):
                    register_library(
                        db,
                        "packaging",
                        "包装",
                        "local",
                        str(Path(tmp) / "packaging"),
                        purpose="packaging",
                        is_default_generic=True,
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.connection.execute(
                        "UPDATE libraries SET purpose='unknown' WHERE library_id='generic'"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.connection.execute(
                        """
                        UPDATE libraries SET purpose='packaging'
                        WHERE library_id='generic'
                        """
                    )
            finally:
                db.close()

    def test_mount_can_change_without_changing_library_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            try:
                db.migrate()
                register_library(db, "park-01", "园区库", "local", r"D:\素材")
                update_mount(db, "park-01", r"E:\素材")
                row = db.connection.execute(
                    "SELECT * FROM libraries WHERE library_id='park-01'"
                ).fetchone()
                self.assertEqual(row["mount_path"], r"E:\素材")
            finally:
                db.close()

    def test_resolve_location_uses_current_mount_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            try:
                db.migrate()
                register_library(db, "park-01", "园区库", "local", r"E:\素材")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('ast_1', 'video', 10, 'abc', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO asset_locations(
                          asset_id, library_id, relative_path, mtime_ns, file_identity
                        ) VALUES('ast_1', 'park-01', '厂房\\a.mp4', 1, 'file-1')
                        """
                    )
                self.assertEqual(
                    resolve_location(db, "ast_1"),
                    Path(r"E:\素材\厂房\a.mp4"),
                )
            finally:
                db.close()

    def test_missing_library_uses_readable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            try:
                db.migrate()
                with self.assertRaisesRegex(KeyError, "找不到素材库"):
                    update_mount(db, "missing", r"E:\素材")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
