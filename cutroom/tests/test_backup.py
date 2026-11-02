import tempfile
import unittest
from pathlib import Path

from media_catalog.backup import backup_database, restore_database
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library


class BackupTests(unittest.TestCase):
    def test_backup_can_restore_openable_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "catalog.sqlite"
            target = root / "restored.sqlite"
            with CatalogDatabase.open(source) as db:
                db.migrate()
                register_library(db, "lib", "测试库", "local", str(root))
            backup = backup_database(source, root / "backups")
            restore_database(backup, target)
            with CatalogDatabase.open(target) as restored:
                row = restored.connection.execute(
                    "SELECT library_id FROM libraries"
                ).fetchone()
                self.assertEqual(row["library_id"], "lib")
                self.assertEqual(
                    restored.connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0],
                    "ok",
                )

    def test_restore_preserves_previous_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "backup.sqlite"
            target = root / "catalog.sqlite"
            with CatalogDatabase.open(backup) as db:
                db.migrate()
            target.write_bytes(b"old database bytes")
            preserved = restore_database(backup, target)
            self.assertEqual(preserved.read_bytes(), b"old database bytes")


if __name__ == "__main__":
    unittest.main()
