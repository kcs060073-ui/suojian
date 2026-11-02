import unittest

from suo_jian.blueprint.retime import extend_for_speech


class RetimeTest(unittest.TestCase):
    def test_extends_without_changing_copy(self):
        draft = {"timeline": [
            {"time": {"start": 0, "end": 1, "duration": 1},
             "subtitle": {"text": "第一句"},
             "fit": {"estimated_speech_duration": 2, "warnings": ["预计需要 2.0s，当前 Slot 只有 1.0s"]}},
            {"time": {"start": 1, "end": 2, "duration": 1},
             "subtitle": {"text": "第二句"},
             "fit": {"estimated_speech_duration": 2, "warnings": []}},
        ]}
        result = extend_for_speech(draft, target_seconds=6)
        self.assertEqual(result["timeline"][-1]["time"]["end"], 6)
        self.assertEqual("".join(x["subtitle"]["text"] for x in result["timeline"]), "第一句第二句")
        self.assertEqual(draft["timeline"][0]["time"]["end"], 1)
        self.assertEqual(result["timeline"][0]["fit"]["warnings"], [])


if __name__ == "__main__":
    unittest.main()
