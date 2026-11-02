import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, mock_open, patch

from media_catalog.database import CatalogDatabase
from media_catalog.fingerprint import full_sha256, quick_fingerprint
from media_catalog.libraries import register_library
from media_catalog.scanner import (
    LibraryOffline,
    confirm_duplicate_candidate,
    discover_files,
    scan_library,
)
from media_catalog.libraries import resolve_location


class ScannerTests(unittest.TestCase):
    def test_discovery_ignores_non_media_and_fingerprint_survives_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "a.mp4"
            original.write_bytes(b"x" * 4096 + b"middle" + b"y" * 4096)
            (root / "note.txt").write_text("ignore", encoding="utf-8")
            first = quick_fingerprint(original)
            renamed = root / "改名.mp4"
            original.rename(renamed)
            self.assertEqual(first, quick_fingerprint(renamed))
            self.assertEqual([renamed], list(discover_files(root)))
            self.assertEqual(len(full_sha256(renamed)), 64)

    def test_scan_supports_long_chinese_path_when_os_allows_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root
            while len(str(nested / "园区长路径素材.mp4")) <= 280:
                nested = nested / ("园区素材目录" * 3)
            extended_nested = (
                Path("\\\\?\\" + str(nested.resolve()))
                if os.name == "nt"
                else nested
            )
            extended_nested.mkdir(parents=True)
            media = extended_nested / "园区长路径素材.mp4"
            media.write_bytes(b"video")
            try:
                with self.open_catalog(root) as db:
                    summary = scan_library(db, "lib")
                    self.assertEqual((summary.completed, summary.failed), (1, 0))
                    relative_path = db.connection.execute(
                        "SELECT relative_path FROM asset_locations"
                    ).fetchone()[0]
                    self.assertGreater(len(str(root / relative_path)), 260)
            finally:
                media.unlink(missing_ok=True)
                current = extended_nested
                extended_root = (
                    Path("\\\\?\\" + str(root.resolve()))
                    if os.name == "nt"
                    else root
                )
                while current != extended_root:
                    current.rmdir()
                    current = current.parent

    def test_quick_collision_creates_candidate_without_merging_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp4").write_bytes(b"a")
            (root / "b.mp4").write_bytes(b"b")
            with self.open_catalog(root) as db:
                summary = scan_library(db, "lib", fingerprint_fn=lambda _: "same")
                self.assertEqual(summary.completed, 2)
                self.assertEqual(
                    db.connection.execute("SELECT count(*) FROM assets").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM duplicate_candidates"
                    ).fetchone()[0],
                    1,
                )

    def test_full_hash_marks_collision_as_different_without_merging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp4").write_bytes(b"a")
            (root / "b.mp4").write_bytes(b"b")
            with self.open_catalog(root) as db:
                scan_library(db, "lib", fingerprint_fn=lambda _: "same")
                row = db.connection.execute(
                    "SELECT left_asset_id, right_asset_id FROM duplicate_candidates"
                ).fetchone()
                status = confirm_duplicate_candidate(db, row[0], row[1])
                self.assertEqual(status, "different")
                self.assertEqual(
                    db.connection.execute("SELECT count(*) FROM assets").fetchone()[0],
                    2,
                )

    def test_confirmed_local_and_nas_copies_share_asset_and_prefer_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            nas = root / "nas"
            local.mkdir()
            nas.mkdir()
            (local / "园区.mp4").write_bytes(b"same-video")
            (nas / "备份.mp4").write_bytes(b"same-video")
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                register_library(db, "nas", "NAS备份", "nas", str(nas))
                register_library(db, "local", "本地库", "local", str(local))
                scan_library(db, "nas")
                scan_library(db, "local")
                candidate = db.connection.execute(
                    "SELECT left_asset_id, right_asset_id FROM duplicate_candidates"
                ).fetchone()
                self.assertEqual(
                    confirm_duplicate_candidate(db, candidate[0], candidate[1]),
                    "confirmed",
                )
                self.assertEqual(
                    db.connection.execute("SELECT count(*) FROM assets").fetchone()[0],
                    1,
                )
                asset_id = db.connection.execute(
                    "SELECT asset_id FROM assets"
                ).fetchone()[0]
                locations = db.connection.execute(
                    "SELECT library_id FROM asset_locations WHERE asset_id=?",
                    (asset_id,),
                ).fetchall()
                self.assertEqual({row[0] for row in locations}, {"local", "nas"})
                self.assertEqual(resolve_location(db, asset_id), local / "园区.mp4")

    def test_confirmed_computer_and_removable_copies_share_one_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            computer = root / "computer"
            removable = root / "removable"
            computer.mkdir()
            removable.mkdir()
            (computer / "园区.mp4").write_bytes(b"same-video")
            (removable / "移动盘备份.mp4").write_bytes(b"same-video")
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                register_library(db, "computer", "本机素材", "local", str(computer))
                register_library(
                    db,
                    "removable",
                    "移动硬盘素材",
                    "local",
                    str(removable),
                )
                scan_library(db, "computer")
                scan_library(db, "removable")
                candidate = db.connection.execute(
                    "SELECT left_asset_id, right_asset_id FROM duplicate_candidates"
                ).fetchone()
                self.assertEqual(
                    confirm_duplicate_candidate(db, candidate[0], candidate[1]),
                    "confirmed",
                )
                self.assertEqual(
                    db.connection.execute("SELECT count(*) FROM assets").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM asset_locations"
                    ).fetchone()[0],
                    2,
                )

    def test_rename_reuses_asset_id_when_file_identity_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "a.mp4"
            original.write_bytes(b"video")
            with self.open_catalog(root) as db:
                scan_library(db, "lib")
                before = db.connection.execute(
                    "SELECT asset_id FROM asset_locations"
                ).fetchone()[0]
                original.rename(root / "renamed.mp4")
                scan_library(db, "lib")
                rows = db.connection.execute(
                    "SELECT asset_id, relative_path FROM asset_locations"
                ).fetchall()
                self.assertEqual(
                    [(before, "renamed.mp4")],
                    [(row[0], row[1]) for row in rows],
                )

    def test_repeat_scan_without_file_identity_does_not_duplicate_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp4").write_bytes(b"video")
            with self.open_catalog(root) as db:
                scan_library(db, "lib", identity_fn=lambda _: "")
                scan_library(db, "lib", identity_fn=lambda _: "")
                self.assertEqual(
                    db.connection.execute("SELECT count(*) FROM assets").fetchone()[0],
                    1,
                )

    def test_unchanged_file_reuses_fingerprint_without_reading_media_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "a.mp4"
            media.write_bytes(b"video")
            with self.open_catalog(root) as db:
                scan_library(db, "lib")

                def fail_if_called(_path):
                    raise AssertionError("unchanged media must not be fingerprinted again")

                summary = scan_library(db, "lib", fingerprint_fn=fail_if_called)
                self.assertEqual((summary.completed, summary.failed), (1, 0))
                self.assertEqual(
                    db.connection.execute(
                        "SELECT state FROM asset_locations WHERE relative_path='a.mp4'"
                    ).fetchone()[0],
                    "available",
                )

    def test_file_level_scan_error_is_reported_without_stopping_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.mp4").write_bytes(b"bad")
            (root / "good.mp4").write_bytes(b"good")
            failures = []

            def fingerprint(path):
                if path.name == "bad.mp4":
                    raise OSError("temporary read failure")
                return "good-fingerprint"

            with self.open_catalog(root) as db:
                summary = scan_library(
                    db,
                    "lib",
                    fingerprint_fn=fingerprint,
                    on_error=lambda path, error: failures.append((path, error)),
                )
                self.assertEqual((summary.completed, summary.failed), (1, 1))
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0][0], "bad.mp4")
                self.assertIn("temporary read failure", failures[0][1])

    def test_offline_library_does_not_mark_assets_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            root.mkdir()
            (root / "a.mp4").write_bytes(b"video")
            with CatalogDatabase.open(Path(tmp) / "catalog.sqlite") as db:
                db.migrate()
                register_library(db, "lib", "测试库", "local", str(root))
                scan_library(db, "lib")
                offline = root.with_name("media-offline")
                root.rename(offline)
                with self.assertRaisesRegex(LibraryOffline, "素材目录当前不可访问"):
                    scan_library(db, "lib")
                state = db.connection.execute(
                    "SELECT state FROM asset_locations"
                ).fetchone()[0]
                online = db.connection.execute(
                    "SELECT online FROM libraries WHERE library_id='lib'"
                ).fetchone()[0]
                self.assertEqual((state, online), ("offline", 0))

    def test_removed_file_is_marked_missing_after_complete_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "a.mp4"
            media.write_bytes(b"video")
            with self.open_catalog(root) as db:
                scan_library(db, "lib")
                media.unlink()
                scan_library(db, "lib")
                self.assertEqual(
                    db.connection.execute(
                        "SELECT state FROM asset_locations"
                    ).fetchone()[0],
                    "missing",
                )

    def test_resume_follows_discovery_order_instead_of_raw_string_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp4").write_bytes(b"a")
            (root / "Z.mp4").write_bytes(b"z")
            with self.open_catalog(root) as db:
                summary = scan_library(db, "lib", start_after="a.mp4")
                self.assertEqual(summary.discovered, 1)
                self.assertEqual(summary.completed, 1)
                self.assertEqual(summary.remaining, 0)
                self.assertEqual(
                    db.connection.execute(
                        "SELECT relative_path FROM asset_locations"
                    ).fetchone()[0],
                    "Z.mp4",
                )

    def test_missing_resume_cursor_falls_back_to_safe_full_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp4").write_bytes(b"a")
            with self.open_catalog(root) as db:
                summary = scan_library(db, "lib", start_after="removed.mp4")
                self.assertEqual(summary.completed, 1)

    def test_full_hash_never_requests_unbounded_read(self):
        handle = mock_open(read_data=b"abc").return_value
        handle.read.side_effect = [b"abc", b""]
        with patch.object(Path, "open", return_value=handle):
            full_sha256(Path("large.mp4"), chunk_size=1024)
        handle.read.assert_any_call(1024)
        self.assertNotIn(call(), handle.read.call_args_list)

    @staticmethod
    def open_catalog(root: Path) -> CatalogDatabase:
        db = CatalogDatabase.open(root / "catalog.sqlite")
        db.migrate()
        register_library(db, "lib", "测试库", "local", str(root))
        return db


if __name__ == "__main__":
    unittest.main()
