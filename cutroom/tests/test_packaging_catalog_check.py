import base64
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from media_catalog import packaging_plan
from media_catalog.database import CatalogDatabase
from media_catalog.packaging_catalog_check import (
    latest_packaging_catalog_check,
    relative_entry,
    request_packaging_catalog_check,
    run_packaging_catalog_check,
)
from media_catalog.storage_settings import configure_personal_packaging_root


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class PackagingCatalogCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.manifests = self.root / "assets/manifests"
        self.manifests.mkdir(parents=True)
        pack = self.root / "assets/pack"
        pack.mkdir(parents=True)
        (pack / "unchanged.png").write_bytes(PNG)
        (pack / "updated.png").write_bytes(PNG)
        self.manifest = self.manifests / "stickers.json"
        self.manifest.write_text(json.dumps({
            "category": "sticker",
            "items": [
                {
                    "id": "unchanged",
                    "file": "assets/pack/unchanged.png",
                    "license": "commercial-approved",
                },
                {
                    "id": "updated",
                    "file": "assets/pack/updated.png",
                    "license": "commercial-approved",
                    "size_bytes": 1,
                },
                {
                    "id": "missing",
                    "file": "assets/pack/missing.png",
                    "license": "commercial-approved",
                },
            ],
        }), encoding="utf-8")
        for filename, category in (
            ("subtitle-presets.json", "subtitle"),
            ("voice-profiles.json", "voice"),
            ("bgm.json", "bgm"),
            ("sfx.json", "sfx"),
            ("visual-cards.json", "title_card"),
            ("fancy-text-presets.json", "fancy_text"),
        ):
            (self.manifests / filename).write_text(
                json.dumps({"category": category, "items": []}),
                encoding="utf-8",
            )
        self.personal = self.root / "personal"
        self.personal.mkdir()
        configure_personal_packaging_root(self.db, self.root, str(self.personal))
        (self.personal / "stickers/inbox/unknown.png").write_bytes(PNG)
        (self.personal / "stickers/inbox/broken.png").write_bytes(b"not-image")
        self.manifest_patch = patch.object(
            packaging_plan, "MANIFEST_DIR", self.manifests
        )
        self.manifest_patch.start()

    def tearDown(self):
        self.manifest_patch.stop()
        self.db.close()
        self.temporary.cleanup()

    def test_check_reports_added_updated_missing_invalid_and_unchanged(self):
        result = run_packaging_catalog_check(self.db, self.root)
        self.assertEqual(
            {item["status"] for item in result["items"]},
            {"added", "updated", "missing", "invalid", "unchanged"},
        )
        self.assertEqual(latest_packaging_catalog_check(self.db)["checkId"], result["checkId"])

    def test_unknown_license_and_path_escape_never_become_selectable(self):
        result = run_packaging_catalog_check(self.db, self.root)
        unknown = next(
            item for item in result["items"]
            if item["relativePath"] == "stickers/inbox/unknown.png"
        )
        self.assertFalse(unknown["selectable"])
        self.assertEqual(unknown["reasonCode"], "PACKAGING_LICENSE_UNKNOWN")
        with self.assertRaisesRegex(ValueError, "PATH_OUTSIDE_PACKAGING_ROOT"):
            relative_entry(self.personal, self.root / "outside.png")

    def test_check_does_not_change_manifest_bytes(self):
        before = self.manifest.read_bytes()
        run_packaging_catalog_check(self.db, self.root)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_repeated_request_reuses_one_active_catalog_job(self):
        first = request_packaging_catalog_check(self.db, self.root)
        second = request_packaging_catalog_check(self.db, self.root)
        self.assertEqual(first["checkId"], second["checkId"])
        self.assertEqual(first["jobId"], second["jobId"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_type='packaging_catalog_check'"
            ).fetchone()[0],
            1,
        )

    def test_orphaned_queued_check_uses_retry_job_id(self):
        first = request_packaging_catalog_check(self.db, self.root)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed' WHERE job_id=?",
                (first["jobId"],),
            )

        retried = request_packaging_catalog_check(self.db, self.root)
        repeated = request_packaging_catalog_check(self.db, self.root)

        self.assertEqual(retried["checkId"], first["checkId"])
        self.assertNotEqual(retried["jobId"], first["jobId"])
        self.assertTrue(retried["jobId"].endswith("-r1"))
        self.assertEqual(repeated["jobId"], retried["jobId"])

    def test_unsigned_registered_asset_uses_previous_completed_check_as_baseline(self):
        first = run_packaging_catalog_check(self.db, self.root)
        baseline = next(item for item in first["items"] if item["assetRef"] == "unchanged")
        self.assertEqual(baseline["status"], "unchanged")
        path = self.root / "assets/pack/unchanged.png"
        path.write_bytes(PNG + b"replacement")

        second = run_packaging_catalog_check(self.db, self.root)
        changed = next(item for item in second["items"] if item["assetRef"] == "unchanged")
        self.assertEqual(changed["status"], "updated")
        self.assertEqual(changed["reasonCode"], "REGISTERED_CONTENT_CHANGED")

    def test_failed_check_is_persisted_and_a_new_request_can_run(self):
        queued = request_packaging_catalog_check(self.db, self.root)
        with patch(
            "media_catalog.packaging_catalog_check._decodable",
            side_effect=OSError("probe unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "probe unavailable"):
                run_packaging_catalog_check(
                    self.db,
                    self.root,
                    check_id=queued["checkId"],
                )
        failed = latest_packaging_catalog_check(self.db)
        self.assertEqual(failed["status"], "failed")
        replacement = request_packaging_catalog_check(self.db, self.root)
        self.assertNotEqual(replacement["checkId"], queued["checkId"])

    def test_manifest_path_outside_project_is_rejected_before_file_access(self):
        outside = self.root.parent / "outside-packaging.png"
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["items"].append({
            "id": "outside",
            "file": str(outside),
            "license": "commercial-approved",
        })
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        module = __import__(
            "media_catalog.packaging_catalog_check",
            fromlist=["_manifest_entries"],
        )
        entries = module._manifest_entries(self.root)
        outside_entry = next(item for item in entries if item["assetRef"] == "outside")
        self.assertEqual(outside_entry["reasonCode"], "PATH_OUTSIDE_PACKAGING_ROOT")


if __name__ == "__main__":
    unittest.main()
