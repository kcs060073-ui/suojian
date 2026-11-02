import unittest
from suo_jian.blueprint.schema import build_blueprint

class BlueprintTest(unittest.TestCase):
    def test_contract(self):
        result = build_blueprint({"duration": 2, "fps": 30, "file_name": "x.mp4"}, [{"motion": {"type": "static"}}], {"available": False}, [], {"available": True}, [])
        self.assertEqual(result["schema_version"], "0.9")
        self.assertIn("shots", result)
        self.assertIn("sound_effects", result)
        self.assertIn("stickers", result)
        self.assertFalse(result["draft_generation"]["approved"])

    def test_styles_and_slots(self):
        result = build_blueprint(
            {"duration": 2, "fps": 30, "file_name": "x.mp4"},
            [{"id": 1, "start": 0, "end": 2, "duration": 2, "motion": {"type": "static"}}],
            {"available": False, "timestamps": []},
            [{"start": 0, "end": 2, "text": "园区总面积十五万平方米"}],
            {"available": True}, [], {},
        )
        self.assertIn("subtitle_style_01", result["styles"])
        self.assertEqual(result["edit_slots"][0]["semantic_role"], "area")
        self.assertEqual(result["subtitles"][0]["style_id"], "subtitle_style_01")

if __name__ == "__main__": unittest.main()
