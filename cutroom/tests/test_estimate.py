import unittest

from media_catalog.estimate import estimate_p0_bytes


class EstimateTests(unittest.TestCase):
    def test_estimate_includes_twenty_percent_headroom(self):
        self.assertEqual(estimate_p0_bytes(10), 98_304)

    def test_negative_file_count_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不能小于 0"):
            estimate_p0_bytes(-1)


if __name__ == "__main__":
    unittest.main()
