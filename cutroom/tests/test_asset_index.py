import json
import tempfile
import unittest
from pathlib import Path

from media_catalog.asset_index import list_asset_index
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.task_sources import set_task_sources
from media_catalog.tasks import create_task


class AssetIndexTests(unittest.TestCase):
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
            self.db,
            "primary",
            "本次项目",
            "local",
            str(self.primary),
            purpose="task_specific",
        )
        register_library(
            self.db,
            "other",
            "其他项目",
            "local",
            str(self.other),
            purpose="task_specific",
        )
        create_task(self.db, "task-1", "数字人任务", "digital_human")
        self._insert_asset(
            "asset-online",
            "primary",
            "园区/门头.mp4",
            state="available",
            full_sha256="full-hash",
            duration=12.5,
        )
        self._insert_asset(
            "asset-offline",
            "primary",
            "园区/航拍.mp4",
            state="available",
        )
        self._insert_asset(
            "asset-outside",
            "other",
            "外部/不应出现.mp4",
            state="available",
        )
        set_task_sources(
            self.db,
            "task-1",
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE asset_locations SET state='offline' WHERE asset_id='asset-offline'"
            )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _insert_asset(
        self,
        asset_id,
        library_id,
        relative_path,
        *,
        state,
        full_sha256=None,
        duration=None,
    ):
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO assets(
                  asset_id, media_kind, size_bytes, quick_fingerprint,
                  full_sha256, created_at
                ) VALUES(?, 'video', 2048, ?, ?, datetime('now'))
                """,
                (asset_id, f"quick-{asset_id}", full_sha256),
            )
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns,
                  file_identity, state
                ) VALUES(?, ?, ?, 1, ?, ?)
                """,
                (asset_id, library_id, relative_path, asset_id, state),
            )
            if duration is not None:
                connection.execute(
                    """
                    INSERT INTO analysis_runs(
                      asset_id, stage, status, result_json, updated_at
                    ) VALUES(?, 'technical_probe', 'completed', ?, datetime('now'))
                    """,
                    (
                        asset_id,
                        json.dumps({"format": {"duration": str(duration)}}),
                    ),
                )

    def test_task_scope_keeps_offline_assets_visible_and_excludes_other_sources(self):
        result = list_asset_index(self.db, task_id="task-1")

        self.assertEqual(result["total"], 2)
        self.assertEqual(
            [item["assetId"] for item in result["items"]],
            ["asset-offline", "asset-online"],
        )
        offline, online = result["items"]
        self.assertEqual(offline["availability"], "offline")
        self.assertFalse(offline["online"])
        self.assertIsNone(offline["previewUrl"])
        self.assertEqual(offline["authorizationStatus"], "unknown")
        self.assertEqual(online["previewUrl"], "/api/tasks/task-1/rough-media/asset-online/preview")
        self.assertEqual(online["durationSeconds"], 12.5)
        self.assertEqual(online["fingerprintStatus"], "full")

    def test_self_produced_status_is_visible_only_for_matching_asset_bytes(self):
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO self_produced_asset_authorizations(
                    asset_id,quick_fingerprint,confirmed_by
                ) VALUES('asset-online','quick-asset-online','reviewer')"""
            )
        approved = list_asset_index(self.db, task_id="task-1")
        self.assertEqual(
            next(item for item in approved["items"] if item["assetId"] == "asset-online")
            ["authorizationStatus"],
            "approved",
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE assets SET quick_fingerprint='changed' WHERE asset_id='asset-online'"
            )
        changed = list_asset_index(self.db, task_id="task-1")
        self.assertEqual(
            next(item for item in changed["items"] if item["assetId"] == "asset-online")
            ["authorizationStatus"],
            "unknown",
        )

    def test_task_scope_excludes_assets_added_after_the_latest_frozen_snapshot(self):
        self._insert_asset(
            "asset-added-later",
            "primary",
            "园区/新入库.mp4",
            state="available",
        )

        result = list_asset_index(self.db, task_id="task-1")

        self.assertNotIn(
            "asset-added-later",
            {item["assetId"] for item in result["items"]},
        )

    def test_search_filters_and_pagination_are_applied_to_the_same_result_set(self):
        filtered = list_asset_index(
            self.db,
            task_id="task-1",
            query="航拍",
            media_kind="video",
            availability="offline",
            limit=1,
            offset=0,
        )

        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["limit"], 1)
        self.assertEqual(filtered["offset"], 0)
        self.assertEqual(filtered["items"][0]["assetId"], "asset-offline")

    def test_authorization_filter_is_truthful_for_unverified_catalog_assets(self):
        unknown = list_asset_index(
            self.db,
            task_id="task-1",
            authorization="unknown",
        )
        approved = list_asset_index(
            self.db,
            task_id="task-1",
            authorization="approved",
        )

        self.assertEqual(unknown["total"], 2)
        self.assertEqual(approved["total"], 0)

    def test_invalid_filters_are_rejected_without_querying(self):
        for kwargs in (
            {"media_kind": "document"},
            {"availability": "maybe"},
            {"authorization": "maybe"},
            {"limit": 0},
            {"offset": -1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    list_asset_index(self.db, **kwargs)


if __name__ == "__main__":
    unittest.main()
