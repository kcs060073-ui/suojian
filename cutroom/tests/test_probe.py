import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from media_catalog.database import CatalogDatabase
from media_catalog.image_probe import probe_image
from media_catalog.libraries import register_library
from media_catalog.probe import probe_media, probe_pending_assets
from media_catalog.scanner import scan_library


class ProbeTests(unittest.TestCase):
    def test_chinese_path_is_passed_as_one_argument(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"streams": []}),
                stderr="",
            )

        self.assertEqual(
            probe_media(Path("园区/厂房.mp4"), Path("ffprobe.exe"), runner),
            {"streams": []},
        )
        self.assertEqual(calls[0][-1], str(Path("园区/厂房.mp4")))

    def test_failure_summary_is_bounded(self):
        def runner(args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="x" * 5000)

        with self.assertRaisesRegex(RuntimeError, "x{100}") as caught:
            probe_media(Path("broken.mp4"), Path("ffprobe.exe"), runner)
        self.assertLessEqual(len(str(caught.exception)), 2000)

    def test_timeout_is_not_swallowed(self):
        def runner(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 60)

        with self.assertRaises(subprocess.TimeoutExpired):
            probe_media(Path("slow.mp4"), Path("ffprobe.exe"), runner)

    def test_probe_results_are_persisted_and_completed_assets_are_not_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp4").write_bytes(b"video")
            with CatalogDatabase.open(root / "catalog.sqlite") as db:
                db.migrate()
                register_library(db, "lib", "测试库", "local", str(root))
                scan_library(db, "lib")
                calls = []

                def runner(args, **kwargs):
                    calls.append(args)
                    return SimpleNamespace(
                        returncode=0,
                        stdout='{"streams": []}',
                        stderr="",
                    )

                first = probe_pending_assets(
                    db, "lib", Path("ffprobe.exe"), runner
                )
                second = probe_pending_assets(
                    db, "lib", Path("ffprobe.exe"), runner
                )
                row = db.connection.execute(
                    """
                    SELECT status, result_json
                    FROM analysis_runs
                    WHERE stage='technical_probe'
                    """
                ).fetchone()
                self.assertEqual(
                    (first.completed, second.completed, len(calls)),
                    (1, 0, 1),
                )
                self.assertEqual(row["status"], "completed")
                self.assertEqual(json.loads(row["result_json"]), {"streams": []})

    def test_images_use_image_probe_and_not_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new("RGB", (32, 18)).save(root / "a.png")
            with self.open_catalog(root) as db:
                scan_library(db, "lib")
                ffprobe_calls = []
                image_calls = []
                summary = probe_pending_assets(
                    db,
                    "lib",
                    Path("ffprobe.exe"),
                    runner=lambda *args, **kwargs: ffprobe_calls.append(args),
                    image_probe_fn=lambda path: image_calls.append(path)
                    or {"kind": "image"},
                    workers=2,
                )
                self.assertEqual((summary.completed, summary.failed), (1, 0))
                self.assertEqual(len(image_calls), 1)
                self.assertEqual(ffprobe_calls, [])

    def test_worker_count_is_bounded_and_results_persist_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(8):
                Image.new("RGB", (8, 8)).save(root / f"{index}.png")
            with self.open_catalog(root) as db:
                scan_library(db, "lib")
                lock = threading.Lock()
                active = 0
                peak = 0

                def image_probe(path):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    time.sleep(0.02)
                    with lock:
                        active -= 1
                    return {"kind": "image", "path": path.name}

                result = probe_pending_assets(
                    db,
                    "lib",
                    None,
                    image_probe_fn=image_probe,
                    workers=2,
                    batch_size=4,
                )
                self.assertEqual(result.completed, 8)
                self.assertGreaterEqual(peak, 2)
                self.assertLessEqual(peak, 2)
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM analysis_runs WHERE status='completed'"
                    ).fetchone()[0],
                    8,
                )

    def test_retry_only_reprocesses_failed_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new("RGB", (8, 8)).save(root / "good.png")
            Image.new("RGB", (8, 8)).save(root / "retry.png")
            with self.open_catalog(root) as db:
                scan_library(db, "lib")
                calls = []

                def first_probe(path):
                    calls.append(path.name)
                    if path.name == "retry.png":
                        raise OSError("temporary failure")
                    return {"kind": "image"}

                first = probe_pending_assets(
                    db,
                    "lib",
                    None,
                    image_probe_fn=first_probe,
                    workers=1,
                )
                second = probe_pending_assets(
                    db,
                    "lib",
                    None,
                    image_probe_fn=lambda path: calls.append(path.name)
                    or {"kind": "image"},
                    workers=1,
                )
                self.assertEqual((first.completed, first.failed), (1, 1))
                self.assertEqual((second.completed, second.failed), (1, 0))
                self.assertEqual(calls.count("good.png"), 1)
                self.assertEqual(calls.count("retry.png"), 2)
                self.assertEqual(
                    db.connection.execute(
                        "SELECT count(*) FROM analysis_runs WHERE status='completed'"
                    ).fetchone()[0],
                    2,
                )

    @staticmethod
    def open_catalog(root: Path) -> CatalogDatabase:
        db = CatalogDatabase.open(root / "catalog.sqlite")
        db.migrate()
        register_library(db, "lib", "测试库", "local", str(root))
        return db


class ImageProbeTests(unittest.TestCase):
    def test_png_metadata_is_read_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "园区平面图.png"
            Image.new("RGBA", (320, 180), (255, 0, 0, 128)).save(path)
            result = probe_image(path)
            self.assertEqual(result["kind"], "image")
            self.assertEqual((result["width"], result["height"]), (320, 180))
            self.assertEqual(result["format"], "PNG")
            self.assertEqual(result["mode"], "RGBA")
            self.assertTrue(result["has_alpha"])

    def test_invalid_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "损坏图片.png"
            path.write_bytes(b"not-an-image")
            with self.assertRaises(OSError):
                probe_image(path)


if __name__ == "__main__":
    unittest.main()
