import tempfile
import unittest
from pathlib import Path

from suo_jian.integrations.cutroom.mapper import make_ad_storyboard, make_ad_task_request
from suo_jian.jianying.cutroom_import import _subtitle_cues


class CutroomIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.draft = {
            "script": {"text": "嘉兴园区欢迎您"},
            "timeline": [
                {"time": {"start": 0, "duration": 2}, "subtitle": {"text": "嘉兴园区"}},
                {"time": {"start": 2, "duration": 1}, "subtitle": {"text": "欢迎您"}},
            ],
        }

    def test_task_request_matches_cutroom_ad_route(self):
        request = make_ad_task_request(self.draft, title="演示", purpose="招商",
                                       primary_library_id="suojian-jiaxing-park")
        self.assertEqual(request["route"], "ad_mix")
        self.assertEqual(request["target_duration_seconds"], 3)
        self.assertEqual(request["primary_library_id"], "suojian-jiaxing-park")

    def test_storyboard_requires_exact_locked_script(self):
        plan = make_ad_storyboard(self.draft, "嘉兴园区欢迎您", "9:16")
        self.assertEqual(len(plan["plan"]["segments"]), 2)
        with self.assertRaises(ValueError):
            make_ad_storyboard(self.draft, "改过的文案", "9:16")

    def test_storyboard_uses_explicit_visual_queries(self):
        guide = [{"query": "一楼厂房", "prompt": "一楼实景"},
                 {"query": "园区门口", "prompt": "入口实景"}]
        plan = make_ad_storyboard(self.draft, "嘉兴园区欢迎您", "9:16", guide)
        self.assertEqual(plan["plan"]["segments"][0]["query"], "一楼厂房")
        self.assertEqual(plan["plan"]["segments"][1]["prompt"], "入口实景")

    def test_srt_cues_keep_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subtitles.srt"
            path.write_text("1\n00:00:00,500 --> 00:00:01,750\n嘉兴园区\n", encoding="utf-8")
            cues = _subtitle_cues(path)
        self.assertEqual(cues[0]["time"], {"start": 0.5, "duration": 1.25})


if __name__ == "__main__":
    unittest.main()
