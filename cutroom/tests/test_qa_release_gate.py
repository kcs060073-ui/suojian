import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog.qa import QADetectionError, _check_video_spec, run_qa


class VideoSpecificationGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = Path(self.tmp.name) / "candidate.mp4"
        self.video.write_bytes(b"candidate")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def valid_payload():
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30000/1001",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "100.5"},
        }

    def probe(self, payload):
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )
        with patch("media_catalog.qa.subprocess.run", return_value=completed):
            return _check_video_spec(Path("ffprobe"), self.video)

    def test_accepts_h264_vertical_hd_audio_and_fractional_average_fps(self):
        issues, spec = self.probe(self.valid_payload())

        self.assertFalse([item for item in issues if item["severity"] == "blocking"])
        self.assertAlmostEqual(spec["fps"], 30000 / 1001)
        self.assertEqual(spec["codec"], "h264")
        self.assertEqual(spec["duration"], 100.5)
        self.assertTrue(any(item["check"] == "video_evidence" and item["ok"]
                            for item in issues))

    def test_falls_back_to_fractional_r_frame_rate_when_average_is_zero(self):
        payload = self.valid_payload()
        payload["streams"][0]["avg_frame_rate"] = "0/0"
        payload["streams"][0]["r_frame_rate"] = "25/1"

        issues, spec = self.probe(payload)

        self.assertFalse([item for item in issues if item["severity"] == "blocking"])
        self.assertEqual(spec["fps"], 25.0)

    def test_rejects_each_required_video_specification(self):
        cases = []

        payload = self.valid_payload()
        payload["streams"] = [payload["streams"][1]]
        cases.append(("video_stream", payload))

        payload = self.valid_payload()
        payload["streams"][0]["codec_name"] = "vp9"
        cases.append(("video_codec", payload))

        payload = self.valid_payload()
        payload["streams"][0]["height"] = 1700
        cases.append(("aspect_ratio", payload))

        payload = self.valid_payload()
        payload["streams"][0]["width"] = 720
        payload["streams"][0]["height"] = 1280
        cases.append(("resolution", payload))

        payload = self.valid_payload()
        payload["streams"][0]["avg_frame_rate"] = "24000/1001"
        payload["streams"][0]["r_frame_rate"] = "24000/1001"
        cases.append(("frame_rate", payload))

        payload = self.valid_payload()
        payload["streams"] = [payload["streams"][0]]
        cases.append(("audio_stream", payload))

        for expected_check, candidate in cases:
            with self.subTest(check=expected_check):
                issues, _spec = self.probe(candidate)
                self.assertTrue(any(
                    item["check"] == expected_check
                    and item["severity"] == "blocking"
                    and item["ok"] is False
                    for item in issues
                ))

    def test_rejects_missing_or_zero_denominator_frame_rates(self):
        for avg, raw in ((None, None), ("0/0", "30/0")):
            payload = self.valid_payload()
            payload["streams"][0].pop("avg_frame_rate", None)
            payload["streams"][0].pop("r_frame_rate", None)
            if avg is not None:
                payload["streams"][0]["avg_frame_rate"] = avg
            if raw is not None:
                payload["streams"][0]["r_frame_rate"] = raw
            with self.subTest(avg=avg, raw=raw):
                issues, _spec = self.probe(payload)
                self.assertTrue(any(item["check"] == "frame_rate"
                                    and item["severity"] == "blocking"
                                    for item in issues))

    def test_rejects_non_square_sample_aspect_ratio(self):
        payload = self.valid_payload()
        payload["streams"][0]["sample_aspect_ratio"] = "2:1"

        issues, _spec = self.probe(payload)

        self.assertTrue(any(item["check"] == "aspect_ratio"
                            and item["severity"] == "blocking"
                            for item in issues))

    def test_rejects_vertical_coded_video_rotated_to_landscape(self):
        payload = self.valid_payload()
        payload["streams"][0]["side_data_list"] = [{"rotation": 90}]

        issues, spec = self.probe(payload)

        self.assertEqual(spec["rotation"], 90)
        self.assertTrue(any(item["check"] == "aspect_ratio"
                            and item["severity"] == "blocking"
                            for item in issues))
        self.assertTrue(any(item["check"] == "resolution"
                            and item["severity"] == "blocking"
                            for item in issues))

    def test_accepts_landscape_coded_video_rotated_to_vertical(self):
        payload = self.valid_payload()
        payload["streams"][0]["width"] = 1920
        payload["streams"][0]["height"] = 1080
        payload["streams"][0]["display_aspect_ratio"] = "16:9"
        payload["streams"][0]["tags"] = {"rotate": "-90"}

        issues, spec = self.probe(payload)

        self.assertFalse([item for item in issues if item["severity"] == "blocking"])
        self.assertEqual(spec["rotation"], 270)
        self.assertEqual(spec["display_width"], 1080)
        self.assertEqual(spec["display_height"], 1920)
        self.assertAlmostEqual(spec["aspect_ratio"], 9 / 16)

    def test_invalid_sar_rotation_or_dar_is_blocking(self):
        cases = []

        payload = self.valid_payload()
        payload["streams"][0]["sample_aspect_ratio"] = "1:0"
        cases.append(("pixel_aspect_ratio", payload))

        payload = self.valid_payload()
        payload["streams"][0]["side_data_list"] = [{"rotation": "not-a-number"}]
        cases.append(("rotation", payload))

        payload = self.valid_payload()
        payload["streams"][0]["display_aspect_ratio"] = "9:0"
        cases.append(("display_aspect_ratio", payload))

        payload = self.valid_payload()
        payload["streams"][0]["display_aspect_ratio"] = "16:9"
        cases.append(("display_aspect_ratio", payload))

        for expected_check, candidate in cases:
            with self.subTest(check=expected_check):
                issues, _spec = self.probe(candidate)
                self.assertTrue(any(
                    item["check"] == expected_check
                    and item["severity"] == "blocking"
                    for item in issues
                ))

    def test_ffprobe_process_and_json_failures_are_detection_errors(self):
        bad_process = SimpleNamespace(returncode=1, stdout=b"", stderr=b"damaged")
        with patch("media_catalog.qa.subprocess.run", return_value=bad_process):
            with self.assertRaises(QADetectionError):
                _check_video_spec(Path("ffprobe"), self.video)

        bad_json = SimpleNamespace(returncode=0, stdout=b"not-json", stderr=b"")
        with patch("media_catalog.qa.subprocess.run", return_value=bad_json):
            with self.assertRaises(QADetectionError):
                _check_video_spec(Path("ffprobe"), self.video)


class ReleaseEvidenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "candidate.mp4"
        self.video.write_bytes(b"candidate")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.db.connection.execute(
            "INSERT INTO tasks(task_id,title) VALUES('task-evidence','QA evidence')"
        )
        self.db.connection.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_run_qa_appends_candidate_subtitle_and_loudness_evidence(self):
        info = lambda check: {
            "check": check, "severity": "info", "ok": True, "detail": "ok",
        }
        with (
            patch("media_catalog.qa._check_dependencies", return_value=[info("dependency")]),
            patch("media_catalog.qa._check_repeated_shots", return_value=[info("repeat")]),
            patch("media_catalog.qa._check_packaging_gate", return_value=[info("packaging")]),
            patch("media_catalog.qa._check_privacy", return_value=[info("privacy")]),
            patch("media_catalog.qa.find_ffmpeg", return_value=Path("ffmpeg")),
            patch("media_catalog.qa.find_ffprobe", return_value=Path("ffprobe")),
            patch("media_catalog.qa._check_video_spec", return_value=(
                [info("video_evidence")],
                {"width": 1080, "height": 1920, "duration": 10.0},
            )),
            patch("media_catalog.qa._check_black", return_value=[info("black_frame")]),
            patch("media_catalog.qa._check_freeze", return_value=[info("freeze")]),
            patch("media_catalog.qa._check_silence", return_value=[info("silence")]),
            patch("media_catalog.qa._check_clipping", return_value=[info("clipping")]),
            patch("media_catalog.qa._run_ff", return_value="I: -16.0 LUFS\nPeak: -2.0 dBFS"),
            patch("media_catalog.qa.collect_qa_evidence", return_value=[
                {"check": "material_scope", "severity": "blocking", "ok": False,
                 "detail": "out of scope"},
            ]),
        ):
            report = run_qa(
                self.db,
                "task-evidence",
                str(self.video),
                evidence={"dependency_snapshot": {}, "subtitle_path": None},
            )

        checks = {item["check"] for item in report["issues"]}
        self.assertEqual(report["status"], "blocked")
        self.assertIn("material_scope", checks)
        self.assertIn("subtitle_evidence", checks)
        self.assertIn("loudness_integrated", checks)

if __name__ == "__main__":
    unittest.main()
