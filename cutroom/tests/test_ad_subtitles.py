import tempfile
import unittest
from pathlib import Path

from media_catalog.ad_subtitles import layout_ad_subtitles
from media_catalog.ad_actions import _write_ad_qa_subtitles


class AdSubtitleDisplayTests(unittest.TestCase):
    def test_render_payload_and_qa_share_display_text_without_retiming(self):
        raw = [{"text": "层高11到12米，一层承重5吨，", "start": 1.25, "end": 4.5}]
        preset = {"id": "test", "max_chars_per_line": 11, "max_lines": 2}
        laid_out = layout_ad_subtitles(raw, preset, "16:9")
        self.assertEqual(laid_out[0]["displayText"], "层高11到12米\n一层承重5吨")
        self.assertEqual(laid_out[0]["text"], raw[0]["text"])
        self.assertEqual(laid_out[0]["start"], 1.25)
        self.assertEqual(laid_out[0]["end"], 4.5)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "qa.ass"
            _write_ad_qa_subtitles(target, laid_out, preset)
            text = target.read_text(encoding="utf-8")
            self.assertIn(r"层高11到12米\N一层承重5吨", text)
            self.assertIn("0:00:01.25,0:00:04.50", text)

    def test_punctuation_only_fails_before_render(self):
        with self.assertRaisesRegex(RuntimeError, "AD_SUBTITLE_EMPTY"):
            layout_ad_subtitles([{"text": "，，。"}], {}, "16:9")


if __name__ == "__main__":
    unittest.main()
