import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog import timecode
from media_catalog.cache import clean_cache
from media_catalog.cli import main
from media_catalog.database import CatalogDatabase
from media_catalog.preview import (
    DEFAULT_LIBRARY_KINDS,
    generate_asset_previews,
    generate_previews,
)
from media_catalog.timecode import verify_timecode_consistency


class PreviewDefaultsTests(unittest.TestCase):
    @staticmethod
    def _seed_video(db, root: Path) -> None:
        source = root / "source.mp4"
        source.write_bytes(b"video")
        with db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO libraries(
                  library_id, name, kind, mount_path, online, created_at, updated_at
                ) VALUES('lib', '测试库', 'local', ?, 1, datetime('now'), datetime('now'))
                """,
                (str(root),),
            )
            connection.execute(
                """
                INSERT INTO assets(
                  asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                """
            )
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns, state
                ) VALUES('asset-1', 'lib', 'source.mp4', 1, 'available')
                """
            )
            connection.execute(
                """
                INSERT INTO analysis_runs(
                  asset_id, stage, status, result_json, updated_at
                ) VALUES(
                  'asset-1', 'technical_probe', 'completed',
                  '{"format":{"duration":10.0},"streams":[{"codec_type":"audio"}]}',
                  datetime('now')
                )
                """
            )

    @staticmethod
    def _fake_ffmpeg(calls: list[list[str]]):
        def runner(command, **kwargs):
            from PIL import Image

            args = [str(value) for value in command]
            calls.append(args)
            output = Path(args[-1])
            if "%03d" in output.name:
                frame = Path(str(output).replace("%03d", "001"))
                frame.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (16, 9), "white").save(frame)
            elif output.suffix.lower() in {".jpg", ".jpeg"}:
                output.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (16, 9), "white").save(output)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"generated")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return runner

    def test_library_preview_defaults_do_not_generate_proxy(self):
        self.assertEqual(
            DEFAULT_LIBRARY_KINDS,
            {"thumbnail", "contact_sheet", "audio_preview"},
        )

    def test_library_preview_default_behavior_never_invokes_proxy_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls: list[list[str]] = []
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                self._seed_video(db, root)

                summary = generate_previews(
                    db,
                    "lib",
                    Path("ffmpeg.exe"),
                    runner=self._fake_ffmpeg(calls),
                    kinds=None,
                )

                self.assertEqual(summary.completed, 1)
                self.assertFalse(
                    any(arg.endswith(".proxy.mp4") for call in calls for arg in call)
                )
                self.assertEqual(
                    {
                        row["kind"]
                        for row in db.connection.execute(
                            "SELECT kind FROM preview_cache WHERE asset_id='asset-1'"
                        )
                    },
                    {"thumbnail", "contact_sheet", "audio_preview"},
                )

    def test_single_asset_preview_default_behavior_still_generates_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls: list[list[str]] = []
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                self._seed_video(db, root)

                produced = generate_asset_previews(
                    db,
                    "asset-1",
                    Path("ffmpeg.exe"),
                    kinds=None,
                    runner=self._fake_ffmpeg(calls),
                )

                self.assertIn("proxy", produced)
                self.assertTrue((root / "cache" / produced["proxy"]).exists())
                self.assertTrue(
                    any(arg.endswith(".proxy.mp4") for call in calls for arg in call)
                )


class CacheCleanupTests(unittest.TestCase):
    def test_cache_cleanup_rejects_reparse_cache_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                (root / "cache").mkdir()
                with patch("media_catalog.cache._is_reparse_path", return_value=True):
                    with self.assertRaisesRegex(ValueError, "重解析|符号链接"):
                        clean_cache(db, dry_run=True)

    def test_cli_rejects_unknown_keep_kind_without_deleting_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "catalog.sqlite"
            with CatalogDatabase.open(db_path) as db:
                db.migrate()
                proxy = root / "cache" / "asset-1.proxy.mp4"
                proxy.parent.mkdir()
                proxy.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'asset-1.proxy.mp4', 5)
                        """
                    )

            error = io.StringIO()
            with redirect_stderr(error):
                code = main(
                    [
                        "--db",
                        str(db_path),
                        "cache",
                        "clean",
                        "--keep",
                        "proxxy",
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("proxxy", error.getvalue())
            self.assertTrue(proxy.exists())
            with CatalogDatabase.open(db_path) as db:
                self.assertEqual(
                    db.connection.execute(
                        "SELECT COUNT(*) FROM preview_cache"
                    ).fetchone()[0],
                    1,
                )

    def test_kept_proxy_counts_toward_budget_and_remaining_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                cache = root / "cache"
                cache.mkdir()
                proxy = cache / "asset-1.proxy.mp4"
                thumbnail = cache / "asset-2.thumb.jpg"
                proxy.write_bytes(b"p" * 80)
                thumbnail.write_bytes(b"t" * 50)
                with db.transaction() as connection:
                    connection.executemany(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES(?, ?, ?, ?, datetime('now'))
                        """,
                        [
                            ("asset-1", "video", 80, "fingerprint-1"),
                            ("asset-2", "image", 50, "fingerprint-2"),
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES(?, ?, ?, ?)
                        """,
                        [
                            ("asset-1", "proxy", proxy.name, 80),
                            ("asset-2", "thumbnail", thumbnail.name, 50),
                        ],
                    )

                result = clean_cache(
                    db,
                    max_bytes=100,
                    keep_kinds={"proxy"},
                )

                self.assertTrue(proxy.exists())
                self.assertFalse(thumbnail.exists())
                self.assertEqual(result["remaining_bytes"], 80)
                self.assertEqual(
                    [row["kind"] for row in result["deleted"]],
                    ["thumbnail"],
                )

    def test_keep_kinds_excludes_proxy_from_cleanup_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                cache = root / "cache"
                cache.mkdir()
                proxy = cache / "asset-1.proxy.mp4"
                thumbnail = cache / "asset-2.thumb.jpg"
                proxy.write_bytes(b"proxy")
                thumbnail.write_bytes(b"thumb")
                with db.transaction() as connection:
                    connection.executemany(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES(?, ?, 5, ?, datetime('now'))
                        """,
                        [
                            ("asset-1", "video", "fingerprint-1"),
                            ("asset-2", "image", "fingerprint-2"),
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES(?, ?, ?, 5)
                        """,
                        [
                            ("asset-1", "proxy", proxy.name),
                            ("asset-2", "thumbnail", thumbnail.name),
                        ],
                    )

                result = clean_cache(db, keep_kinds={"proxy"})

                self.assertTrue(proxy.exists())
                self.assertFalse(thumbnail.exists())
                self.assertEqual(
                    [row["kind"] for row in result["deleted"]],
                    ["thumbnail"],
                )
                self.assertEqual(
                    [
                        row["kind"]
                        for row in db.connection.execute(
                            "SELECT kind FROM preview_cache ORDER BY kind"
                        )
                    ],
                    ["proxy"],
                )

    def test_cache_path_escape_is_rejected_without_deleting_file_or_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                victim = root / "victim.mp4"
                victim.write_bytes(b"original")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 8, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', '../victim.mp4', 8)
                        """
                    )

                result = clean_cache(db)

                self.assertTrue(victim.exists())
                self.assertEqual(
                    db.connection.execute(
                        "SELECT COUNT(*) FROM preview_cache"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(result["remaining_bytes"], 8)
                self.assertTrue(result["remaining_estimated"])
                self.assertEqual(result["failed"][0]["rel_path"], "../victim.mp4")

    def test_delete_failure_keeps_cache_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                cache_file = root / "cache" / "locked.proxy.mp4"
                cache_file.parent.mkdir()
                cache_file.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'locked.proxy.mp4', 5)
                        """
                    )

                with patch.object(
                    Path,
                    "unlink",
                    side_effect=PermissionError("file is locked"),
                ):
                    result = clean_cache(db)

                self.assertEqual(
                    db.connection.execute(
                        "SELECT COUNT(*) FROM preview_cache"
                    ).fetchone()[0],
                    1,
                )
                self.assertTrue(cache_file.exists())
                self.assertEqual(result["freed_bytes"], 0)
                self.assertEqual(result["failed"][0]["rel_path"], "locked.proxy.mp4")

    def test_stat_failure_keeps_cache_file_and_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                cache_file = root / "cache" / "locked.proxy.mp4"
                cache_file.parent.mkdir()
                cache_file.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'locked.proxy.mp4', 5)
                        """
                    )

                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path,
                        "stat",
                        side_effect=PermissionError("metadata is locked"),
                    ):
                        result = clean_cache(db)

                self.assertTrue(cache_file.exists())
                self.assertEqual(
                    db.connection.execute(
                        "SELECT COUNT(*) FROM preview_cache"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(result["remaining_bytes"], 5)
                self.assertTrue(result["remaining_estimated"])
                self.assertEqual(result["failed"][0]["rel_path"], "locked.proxy.mp4")

    def test_invalid_recorded_size_is_reported_during_stat_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                cache_file = root / "cache" / "locked.proxy.mp4"
                cache_file.parent.mkdir()
                cache_file.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'locked.proxy.mp4', 'not-a-number')
                        """
                    )

                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path,
                        "stat",
                        side_effect=PermissionError("metadata is locked"),
                    ):
                        result = clean_cache(db)

                self.assertTrue(cache_file.exists())
                self.assertEqual(result["remaining_bytes"], 0)
                self.assertTrue(result["remaining_estimated"])
                self.assertEqual(result["failed"][0]["rel_path"], "locked.proxy.mp4")
                self.assertIn("size_bytes", result["failed"][0]["error"])

    def test_fractional_recorded_size_rounds_up_during_stat_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                cache_file = root / "cache" / "locked.proxy.mp4"
                cache_file.parent.mkdir()
                cache_file.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'locked.proxy.mp4', 5.9)
                        """
                    )

                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path,
                        "stat",
                        side_effect=PermissionError("metadata is locked"),
                    ):
                        result = clean_cache(db)

                self.assertEqual(result["remaining_bytes"], 6)
                self.assertTrue(result["remaining_estimated"])

    def test_infinite_recorded_size_is_reported_during_stat_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                cache_file = root / "cache" / "locked.proxy.mp4"
                cache_file.parent.mkdir()
                cache_file.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'locked.proxy.mp4', ?)
                        """,
                        (float("inf"),),
                    )

                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path,
                        "stat",
                        side_effect=PermissionError("metadata is locked"),
                    ):
                        result = clean_cache(db)

                self.assertEqual(result["remaining_bytes"], 0)
                self.assertTrue(result["remaining_estimated"])
                self.assertIn("size_bytes", result["failed"][0]["error"])

    def test_missing_physical_file_removes_stale_cache_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'missing.proxy.mp4', 5)
                        """
                    )

                result = clean_cache(db)

                self.assertEqual(
                    db.connection.execute(
                        "SELECT COUNT(*) FROM preview_cache"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(result["deleted"][0]["rel_path"], "missing.proxy.mp4")

    def test_cli_reports_delete_failures_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "catalog.sqlite"
            with CatalogDatabase.open(db_path) as db:
                db.migrate()
                cache_file = root / "cache" / "locked.proxy.mp4"
                cache_file.parent.mkdir()
                cache_file.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'locked.proxy.mp4', 5)
                        """
                    )

            output = io.StringIO()
            with patch.object(
                Path,
                "unlink",
                side_effect=PermissionError("file is locked"),
            ):
                with redirect_stdout(output):
                    code = main(["--db", str(db_path), "cache", "clean"])

            self.assertEqual(code, 1)
            self.assertIn("失败 1", output.getvalue())


class TimecodeConsistencyTests(unittest.TestCase):
    def test_production_duration_probe_has_stable_tool_failure(self):
        def failed_runner(*args, **kwargs):
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="ffprobe failed deterministically",
            )

        with self.assertRaisesRegex(RuntimeError, "ffprobe failed deterministically"):
            timecode._probe_proxy_duration(
                Path("broken.proxy.mp4"),
                ffprobe=Path("ffprobe.exe"),
                runner=failed_runner,
            )

    def test_registered_proxy_uses_production_duration_probe_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                proxy = root / "cache" / "asset-1.proxy.mp4"
                proxy.parent.mkdir()
                proxy.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO analysis_runs(
                          asset_id, stage, status, result_json, updated_at
                        ) VALUES(
                          'asset-1', 'technical_probe', 'completed',
                          '{"format":{"duration":10.0}}', datetime('now')
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'asset-1.proxy.mp4', 5)
                        """
                    )

                with patch.object(
                    timecode,
                    "_probe_proxy_duration",
                    return_value=10.8,
                ) as probe:
                    result = verify_timecode_consistency(db, "asset-1")

                probe.assert_called_once_with(proxy)
                self.assertFalse(result["ok"])

    def test_proxy_duration_difference_blocks_consistency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                proxy = root / "cache" / "asset-1.proxy.mp4"
                proxy.parent.mkdir()
                proxy.write_bytes(b"proxy")
                with db.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                        ) VALUES('asset-1', 'video', 5, 'fingerprint', datetime('now'))
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO analysis_runs(
                          asset_id, stage, status, result_json, updated_at
                        ) VALUES(
                          'asset-1', 'technical_probe', 'completed',
                          '{"format":{"duration":10.0}}', datetime('now')
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                        VALUES('asset-1', 'proxy', 'asset-1.proxy.mp4', 5)
                        """
                    )

                result = verify_timecode_consistency(
                    db,
                    "asset-1",
                    probe_duration=lambda path: 10.8,
                )

                self.assertFalse(result["ok"])
                self.assertIn("代理与原片时长偏差", result["problems"][0])


if __name__ == "__main__":
    unittest.main()
