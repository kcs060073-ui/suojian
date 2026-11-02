import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from media_catalog import packaging_plan
from workbench.api import _packaging_catalog


ROOT = Path(__file__).resolve().parents[1]
PACK_MANIFEST = ROOT / "asset-packs" / "open-commercial-2026-09" / "manifest.json"
CORE_MANIFESTS = {
    "bgm": ROOT / "assets" / "manifests" / "bgm.json",
    "sfx": ROOT / "assets" / "manifests" / "sfx.json",
    "sticker": ROOT / "assets" / "manifests" / "stickers.json",
    "subtitle": ROOT / "assets" / "manifests" / "subtitle-presets.json",
    "title_card": ROOT / "assets" / "manifests" / "visual-cards.json",
    "fancy_text": ROOT / "assets" / "manifests" / "fancy-text-presets.json",
}


class OpenCommercialAssetPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = (
            json.loads(PACK_MANIFEST.read_text(encoding="utf-8"))
            if PACK_MANIFEST.is_file()
            else {"items": []}
        )
        cls.items = cls.pack["items"]
        cls.by_kind = {}
        for item in cls.items:
            cls.by_kind.setdefault(item["kind"], []).append(item)

    def test_pack_manifest_exists(self):
        self.assertTrue(PACK_MANIFEST.is_file(), PACK_MANIFEST)

    def test_pack_has_the_promised_curated_coverage(self):
        self.assertGreaterEqual(len(self.by_kind.get("bgm", [])), 5)
        self.assertGreaterEqual(len(self.by_kind.get("sfx", [])), 16)
        self.assertGreaterEqual(len(self.by_kind.get("sticker", [])), 12)
        self.assertGreaterEqual(len(self.by_kind.get("transition", [])), 8)
        self.assertGreaterEqual(len(self.by_kind.get("subtitle", [])), 2)
        self.assertGreaterEqual(len(self.by_kind.get("title_card", [])), 3)
        self.assertGreaterEqual(len(self.by_kind.get("fancy_text", [])), 3)

    def test_every_item_has_traceable_commercial_terms(self):
        allowed = {"CC0-1.0", "CC-BY-4.0", "OFL-1.1", "generated-from-permissive-source"}
        for item in self.items:
            with self.subTest(asset_id=item["id"]):
                self.assertTrue(item["source_url"].startswith("https://"))
                self.assertIn(item["source_license"], allowed)
                self.assertEqual(item["license"], "commercial-approved")
                self.assertRegex(item["downloaded_at"], r"^2026-09-10T")
                if item.get("attribution_required"):
                    self.assertTrue(item.get("attribution"))

    def test_downloaded_files_exist_and_match_recorded_hashes(self):
        for item in self.items:
            relative = item.get("file")
            if not relative:
                continue
            path = ROOT / relative
            with self.subTest(asset_id=item["id"]):
                self.assertTrue(path.is_file(), path)
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(actual, item["sha256"])

    def test_downloaded_media_decodes(self):
        ffprobe = shutil.which("ffprobe")
        self.assertIsNotNone(ffprobe)
        for kind in ("bgm", "sfx", "sticker"):
            for item in self.by_kind.get(kind, []):
                path = ROOT / item["file"]
                with self.subTest(asset_id=item["id"]):
                    completed = subprocess.run(
                        [ffprobe, "-v", "error", "-show_streams", str(path)],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertTrue(completed.stdout.strip())

    def test_enabled_items_are_registered_in_cutroom_manifests(self):
        registered = {}
        for kind, path in CORE_MANIFESTS.items():
            payload = json.loads(path.read_text(encoding="utf-8"))
            registered[kind] = {item["id"]: item for item in payload["items"]}
        for item in self.items:
            kind = item["kind"]
            if kind == "transition":
                continue
            with self.subTest(asset_id=item["id"]):
                self.assertIn(item["id"], registered[kind])
                if item.get("file"):
                    self.assertEqual(registered[kind][item["id"]].get("file"), item["file"])

    def test_transition_presets_use_ffmpeg_xfade_names(self):
        allowed = {
            "fade", "fadeblack", "fadewhite", "wipeleft", "wiperight",
            "slideleft", "slideright", "smoothup", "smoothdown", "circleopen",
            "circleclose", "radial", "hblur", "pixelize", "zoomin",
        }
        for item in self.by_kind.get("transition", []):
            with self.subTest(asset_id=item["id"]):
                self.assertIn(item["transition"], allowed)
                self.assertGreater(item["duration_s"], 0)
                self.assertLessEqual(item["duration_s"], 0.8)

    def test_cutroom_resolves_registered_team_extension_assets(self):
        try:
            resolved = packaging_plan.resolve_manifest_asset_path(
                "bgm", "bgm-sb-machinery-stars"
            )
        except RuntimeError as error:
            self.fail(f"registered asset-pack path was rejected: {error}")
        self.assertEqual(
            resolved,
            ROOT / "asset-packs" / "bgm" / "open-commercial-2026-09"
            / "machinery-of-the-stars.mp3",
        )

    def test_team_extension_allowlist_still_rejects_parent_escape(self):
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PATH_INVALID"):
            packaging_plan._packaging_path(
                {"file": "asset-packs/../outside/not-allowed.mp3"}, None
            )

    def test_every_enabled_bgm_is_present_and_readable(self):
        manifest = json.loads(CORE_MANIFESTS["bgm"].read_text(encoding="utf-8"))
        for entry in manifest["items"]:
            if entry.get("license") != "commercial-approved":
                continue
            with self.subTest(asset_id=entry["id"]):
                try:
                    path = packaging_plan.resolve_manifest_asset_path("bgm", entry["id"])
                except RuntimeError as error:
                    self.fail(f"enabled BGM path was rejected: {error}")
                self.assertIsNotNone(path)
                self.assertTrue(path.is_file(), path)
                self.assertTrue(packaging_plan._audio_file_readable(path), path)

    def test_downloaded_files_follow_category_layer_directories(self):
        expected_prefix = {
            "bgm": "asset-packs/bgm/open-commercial-2026-09/",
            "sfx": "asset-packs/sfx/open-commercial-2026-09/",
            "sticker": "asset-packs/stickers/open-commercial-2026-09/",
        }
        for kind, prefix in expected_prefix.items():
            for entry in self.by_kind[kind]:
                with self.subTest(asset_id=entry["id"]):
                    self.assertTrue(entry["file"].startswith(prefix), entry["file"])

    def test_workbench_reports_downloaded_items_as_team_extensions(self):
        categories = {category["key"]: category for category in _packaging_catalog()}
        target = next(
            entry for entry in categories["bgm"]["assets"]
            if entry["assetRef"] == "bgm-sb-machinery-stars"
        )
        self.assertEqual(target["sourceLayer"], "extension")
        extension = next(
            entry for entry in categories["bgm"]["directories"]
            if entry["layer"] == "extension"
        )
        self.assertTrue(extension["exists"])


if __name__ == "__main__":
    unittest.main()
