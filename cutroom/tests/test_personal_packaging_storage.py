from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from workbench.api import ApiError, WorkbenchAPI


class PersonalPackagingStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "catalog.sqlite"
        self.db = CatalogDatabase.open(self.db_path)
        self.db.migrate()
        self.addCleanup(lambda: self.db.close())

    def _dispatch(self, method: str, path: str, body: dict | None = None):
        try:
            return WorkbenchAPI(self.db, self.root).dispatch(method, path, body), None
        except ApiError as error:
            return None, error

    def test_user_selected_root_persists_creates_categories_and_never_moves_old_files(self):
        default_root = self.root / "assets-local"
        default_root.mkdir()
        marker = default_root / "keep-me.txt"
        marker.write_text("original personal files stay here", encoding="utf-8")
        selected_root = self.root / "my-packaging"
        selected_root.mkdir()

        result, error = self._dispatch(
            "PUT",
            "/api/settings/storage/personal-packaging",
            {"path": str(selected_root)},
        )
        self.assertIsNone(error)
        self.assertIsNotNone(result, "personal packaging storage endpoint must exist")
        saved = result.payload
        self.assertEqual(saved["path"], str(selected_root.resolve()))
        self.assertFalse(saved["isDefault"])
        self.assertTrue(saved["exists"])
        self.assertEqual(
            [Path(item["path"]).relative_to(selected_root).as_posix() for item in saved["categories"]],
            [
                "subtitle-presets/inbox",
                "voice-profiles/inbox",
                "bgm/inbox",
                "sfx/inbox",
                "visual-cards/inbox",
                "stickers/inbox",
                "fancy-text-presets/inbox",
                "brand/inbox",
            ],
        )
        self.assertTrue(all(Path(item["path"]).is_dir() for item in saved["categories"]))
        self.assertEqual(marker.read_text(encoding="utf-8"), "original personal files stay here")

        self.db.close()
        self.db = CatalogDatabase.open(self.db_path)
        storage = WorkbenchAPI(self.db, self.root).dispatch(
            "GET", "/api/settings/storage"
        ).payload
        personal = next(
            item for item in storage["directories"] if item["key"] == "personalAssets"
        )
        self.assertEqual(personal["path"], str(selected_root.resolve()))
        self.assertEqual(storage["personalPackaging"]["path"], str(selected_root.resolve()))

        catalog = WorkbenchAPI(self.db, self.root).dispatch(
            "GET", "/api/packaging-catalog"
        ).payload
        sfx = next(item for item in catalog["categories"] if item["key"] == "sfx")
        local = next(item for item in sfx["directories"] if item["layer"] == "local")
        self.assertEqual(local["path"], "个人素材/sfx/inbox")

    def test_offline_root_stays_configured_until_user_reconnects_it(self):
        selected_root = self.root / "my-packaging"
        selected_root.mkdir()
        result, error = self._dispatch(
            "PUT",
            "/api/settings/storage/personal-packaging",
            {"path": str(selected_root)},
        )
        self.assertIsNone(error)
        self.assertIsNotNone(result)

        reconnected_root = self.root / "my-packaging-reconnected"
        selected_root.rename(reconnected_root)
        storage = WorkbenchAPI(self.db, self.root).dispatch(
            "GET", "/api/settings/storage"
        ).payload
        self.assertEqual(storage["personalPackaging"]["path"], str(selected_root.resolve()))
        self.assertFalse(storage["personalPackaging"]["exists"])

        result, error = self._dispatch(
            "PUT",
            "/api/settings/storage/personal-packaging",
            {"path": str(reconnected_root)},
        )
        self.assertIsNone(error)
        self.assertEqual(result.payload["path"], str(reconnected_root.resolve()))
        self.assertTrue(result.payload["exists"])

    def test_missing_relative_and_file_paths_are_rejected_without_changing_default(self):
        file_path = self.root / "not-a-directory.txt"
        file_path.write_text("file", encoding="utf-8")
        for invalid_path in ("relative/personal-assets", str(self.root / "missing"), str(file_path)):
            with self.subTest(path=invalid_path):
                result, error = self._dispatch(
                    "PUT",
                    "/api/settings/storage/personal-packaging",
                    {"path": invalid_path},
                )
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assertEqual(error.code, "PERSONAL_PACKAGING_PATH_INVALID")


if __name__ == "__main__":
    unittest.main()
