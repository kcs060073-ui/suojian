import unittest

from suo_jian.blueprint import create_draft_blueprint, enrich_blueprint
from suo_jian.jianying import JianyingBuilder


def reference():
    blueprint = {
        "schema_version": "0.4",
        "source": {"file_name": "reference.mp4", "duration": 4, "width": 720, "height": 1280, "fps": 30},
        "shots": [{"id": 1, "start": 0, "end": 4, "duration": 4, "motion": {"type": "static"}}],
        "subtitles": [{"start": 0, "end": 2, "text": "项目位于北京"}, {"start": 2, "end": 4, "text": "获取园区详情"}],
        "beats": {"available": False, "timestamps": []},
        "creative_analysis": {},
    }
    return enrich_blueprint(blueprint)


class DraftBlueprintTest(unittest.TestCase):
    def test_analysis_and_generation_blueprints_are_separate(self):
        source = reference()
        draft = create_draft_blueprint(source, "项目位于天津，立即预约参观。", "strict")
        self.assertEqual(draft["schema_version"], "draft-0.1")
        self.assertEqual(draft["draft_generation"]["status"], "preview")
        self.assertNotIn("timeline", source)

    def test_builder_requires_approval(self):
        with self.assertRaises(PermissionError):
            JianyingBuilder().build(reference(), {"draft_generation": {"approved": False}}, "missing.mp4", "missing")


if __name__ == "__main__":
    unittest.main()
