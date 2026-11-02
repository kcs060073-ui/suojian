import unittest

from scripts.plan_brief_runner import merge_split_outputs


class PlanBriefRunnerTests(unittest.TestCase):
    def test_merge_keeps_storyboard_shots_and_packaging_decisions_separate(self):
        brief, plan = merge_split_outputs(
            {
                "keywords": ["不应使用"],
                "punchLines": [],
                "title": {"text": "分镜标题", "durationSec": 1},
                "broll": [{"anchorText": "园区", "query": "厂房"}],
            },
            {"speed": 1.25, "titles": [{"text": "分镜标题"}], "inserts": []},
            {
                "keywords": ["重点词"],
                "punchLines": ["包装金句"],
                "title": {"text": "包装标题", "durationSec": 2},
                "broll": [{"anchorText": "不应使用", "query": "错误镜头"}],
            },
            {"speed": 1.25, "titles": [{"text": "包装标题"}], "inserts": []},
        )

        self.assertEqual(brief["broll"], [{"anchorText": "园区", "query": "厂房"}])
        self.assertEqual(brief["keywords"], ["重点词"])
        self.assertEqual(brief["punchLines"], ["包装金句"])
        self.assertEqual(brief["title"]["text"], "包装标题")
        self.assertEqual(plan["titles"], [{"text": "包装标题"}])


if __name__ == "__main__":
    unittest.main()
