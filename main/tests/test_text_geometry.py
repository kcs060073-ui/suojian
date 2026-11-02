import unittest
from suo_jian.jianying.text_geometry import native_font_size, native_border_width, prepare_export_plan


class TextGeometryTests(unittest.TestCase):
    def test_relative_size_is_stable_when_resolution_doubles(self):
        self.assertAlmostEqual(native_font_size(96,1920,1080),native_font_size(192,3840,2160))
        self.assertAlmostEqual(native_font_size(96,1920,1080),8.888888889)

    def test_small_caption_is_not_forced_to_large_minimum(self):
        self.assertLess(native_font_size(24,1920,1080),4)
        with self.assertRaises(ValueError):native_font_size(96,0,1080)

    def test_stroke_follows_font_ratio_and_export_revision_changes_identity(self):
        self.assertAlmostEqual(native_border_width(6,96),native_border_width(12,192))
        plan={'texts':[]}
        revised=prepare_export_plan(plan)
        self.assertNotIn('text_layout_version',plan)
        self.assertIn('text_layout_version',revised)

if __name__=='__main__':unittest.main()
