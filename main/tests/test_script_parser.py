import unittest

from suo_jian.script_parser import ScriptParser, SlotMatcher


class ScriptParserTest(unittest.TestCase):
    def test_preserves_numeric_unit_phrase(self):
        blocks = ScriptParser().parse("项目位于天津武清开发区，园区总面积约十五万平方米，周边拥有京津高速等多条交通干线。")
        texts = [block["text"] for block in blocks]
        self.assertTrue(any("十五万平方米" in text for text in texts))
        self.assertFalse(any(text == "万平方米" for text in texts))

    def test_does_not_invent_punctuation(self):
        script = "嘉兴园区：国资、可租可售、无二房东。层高11米，一层承重5吨。"
        slots = [{"subtitle": {"preferred_chars": 5}} for _ in range(6)]
        blocks = ScriptParser().parse(script, {"edit_slots": slots, "adaptation": "strict"})
        self.assertEqual("".join(block["text"] for block in blocks), script)

    def test_strict_mode_targets_slot_count_without_breaking_units(self):
        slots = [{"subtitle": {"preferred_chars": 6}} for _ in range(6)]
        blocks = ScriptParser().parse("项目位于天津武清开发区，园区总面积约十五万平方米，欢迎企业入驻。", {"edit_slots": slots, "adaptation": "strict"})
        self.assertEqual(len(blocks), 6)
        joined = "".join(block["text"] for block in blocks)
        self.assertIn("十五万平方米", joined)

    def test_weighted_match(self):
        slots = [{"slot_id": "S01", "time": {"duration": 2}, "subtitle": {"original_text": "地铁直达", "preferred_chars": 4, "max_chars": 8}, "semantic_role": "transportation", "rhythm": {"beat_sync": False}}]
        blocks = [{"id": "N01", "text": "高速快速通达", "chars": 6, "semantic_role": "transportation", "estimated_speech_duration": 1.4}]
        match = SlotMatcher().match(slots, blocks)[0]
        self.assertGreater(match["fit_score"], 0.75)

    def test_smart_mode_does_not_repeat_short_script(self):
        slots = [{"slot_id": f"S{i:02d}", "time": {"duration": 2}, "subtitle": {"original_text": "卖点", "preferred_chars": 4, "max_chars": 8}, "semantic_role": "selling_point", "rhythm": {"beat_sync": False}} for i in range(1, 10)]
        blocks = [{"id": f"N{i:02d}", "text": f"卖点{i}", "chars": 3, "semantic_role": "selling_point", "estimated_speech_duration": 1} for i in range(1, 5)]
        matches = SlotMatcher().match(slots, blocks, "smart")
        self.assertEqual(len(matches), 4)
        self.assertEqual(len({match["replacement_text"] for match in matches}), 4)


if __name__ == "__main__":
    unittest.main()
