import json
import tempfile
import unittest
from pathlib import Path

import suo_jian.ui.app as app_module
from suo_jian.blueprint import enrich_blueprint


class DraftApiTest(unittest.TestCase):
    def test_preview_endpoint_writes_separate_draft_blueprint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_uploads, old_results = app_module.UPLOADS, app_module.RESULTS
            app_module.UPLOADS, app_module.RESULTS = root / "uploads", root / "results"
            app_module.UPLOADS.mkdir(); app_module.RESULTS.mkdir()
            try:
                job = "abcddcba"
                result_dir = app_module.RESULTS / job
                result_dir.mkdir()
                reference = enrich_blueprint({
                    "schema_version": "0.4",
                    "source": {"file_name": "reference.mp4", "duration": 2, "width": 720, "height": 1280, "fps": 30},
                    "shots": [{"id": 1, "start": 0, "end": 2, "duration": 2, "motion": {"type": "static"}}],
                    "subtitles": [{"start": 0, "end": 2, "text": "园区招商"}],
                    "beats": {"available": False, "timestamps": []},
                    "creative_analysis": {},
                })
                (result_dir / "edit_blueprint.json").write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
                (app_module.UPLOADS / f"{job}_reference.mp4").write_bytes(b"placeholder")
                response = app_module.app.test_client().post(f"/api/draft/preview/{job}", json={"script": "项目位于天津武清开发区。", "adaptation": "smart"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["draft_blueprint"]["draft_generation"]["status"], "preview")
                self.assertTrue((result_dir / "draft_blueprint.json").exists())
            finally:
                app_module.UPLOADS, app_module.RESULTS = old_uploads, old_results


if __name__ == "__main__":
    unittest.main()
