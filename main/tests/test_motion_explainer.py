import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from suo_jian.ui import motion_explainer as motion
from suo_jian.ui.app import app


class MotionExplainerTests(unittest.TestCase):
    def test_render_props_reject_remote_media_and_oversized_labels(self):
        self.assertEqual(motion.validate_props(motion.TEXT_DEFAULTS), motion.TEXT_DEFAULTS)
        for invalid in ({**motion.TEXT_DEFAULTS, "assetBase": "https://example.com"},
                        {**motion.TEXT_DEFAULTS, "height": "1" * 10},
                        {**motion.TEXT_DEFAULTS, "title": "bad\nlabel"}):
            with self.assertRaises(ValueError):
                motion.validate_props(invalid)

    def test_save_round_trip_and_cross_origin_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(motion, "folder", return_value=Path(temp)):
            client = app.test_client()
            value = {**motion.TEXT_DEFAULTS, "title": "园区选址观察"}
            self.assertEqual(client.post("/api/motion/a89d53f9/save", json=value,
                headers={"Origin": "https://other.example"}).status_code, 403)
            self.assertFalse((Path(temp) / "project.json").exists())
            self.assertEqual(client.post("/api/motion/a89d53f9/save", json=value).status_code, 200)
            self.assertEqual(client.get("/api/motion/a89d53f9/project").json["props"], value)

    def test_restart_reports_interrupted_export_and_hides_private_files(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(motion, "folder", return_value=Path(temp)):
            (Path(temp) / "state.json").write_text(json.dumps({"status": "rendering"}))
            client = app.test_client()
            self.assertEqual(client.get("/api/motion/a89d53f9/project").json["state"]["status"], "interrupted")
            self.assertEqual(client.get("/api/motion/a89d53f9/files/state.json").status_code, 404)

    def test_overlapping_render_does_not_start_second_worker(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(motion, "folder", return_value=Path(temp)), patch.object(motion.threading, "Thread") as worker:
            motion.LOCK.acquire()
            try:
                result = app.test_client().post("/api/motion/a89d53f9/render", json=motion.TEXT_DEFAULTS)
                self.assertEqual(result.status_code, 409)
                worker.assert_not_called()
            finally:
                motion.LOCK.release()


if __name__ == "__main__":
    unittest.main()
