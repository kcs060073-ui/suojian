"""P3-M2 重复检测测试：感知哈希确定性、汉明距离、分组、报告落库。

哈希/分组是纯 Python 实现，用 PIL 合成帧即可独立验证，不依赖真实 ffmpeg。
run_dedup 落库用 patch 替换 scan_videos，避免抽帧耗时。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from media_catalog.database import CatalogDatabase
from media_catalog.dedup import (
    ahash,
    dhash,
    find_groups,
    hamming,
    list_reports,
    run_dedup,
)


def _solid(color, size=(8, 8)):
    img = Image.new("RGB", size, color)
    return img


def _two_tone(left, right, size=(16, 16)):
    """左半区一种颜色、右半区另一种颜色的图，纹理化以保留 ahash 区分度。"""
    img = Image.new("RGB", size, left)
    for y in range(size[1]):
        for x in range(size[0] // 2, size[0]):
            img.putpixel((x, y), right)
    return img


class PerceptualHashTest(unittest.TestCase):
    def test_hashes_are_deterministic(self):
        img = _solid((200, 30, 30))
        self.assertEqual(ahash(img), ahash(img))
        self.assertEqual(dhash(img), dhash(img))

    def test_similar_images_close_in_hamming(self):
        a = _solid((120, 120, 120))
        b = _solid((125, 125, 125))  # 几乎相同
        self.assertLessEqual(hamming(ahash(a), ahash(b)), 4)
        self.assertLessEqual(hamming(dhash(a), dhash(b)), 4)

    def test_different_images_far_in_hamming(self):
        # 纯色图对 ahash 无区分度（mean 二值化全同），必须用有纹理图验证"不同图差异大"
        a = _two_tone((0, 0, 0), (255, 255, 255))
        b = _solid((128, 128, 128))
        self.assertGreater(hamming(ahash(a), ahash(b)), 20)

    def test_hamming_bitcount(self):
        self.assertEqual(hamming(0b0000, 0b0000), 0)
        self.assertEqual(hamming(0b0000, 0b1111), 4)
        self.assertEqual(hamming(0b1010, 0b0101), 4)


class FindGroupsTest(unittest.TestCase):
    def test_groups_near_duplicates(self):
        # 两组近重复（组内 hamming<=2）+ 一个孤立样本（与任何样本 >2）
        samples = [
            {"src": "a.mp4", "frame": 0, "ahash": 0b00000000},
            {"src": "a.mp4", "frame": 1, "ahash": 0b00000011},
            {"src": "b.mp4", "frame": 0, "ahash": 0b11110000},
            {"src": "b.mp4", "frame": 1, "ahash": 0b11110011},
            {"src": "iso.mp4", "frame": 0, "ahash": 0b01010101},
        ]
        groups = find_groups(samples, threshold=2)
        sizes = sorted(len(g) for g in groups)
        # 两组各 2 个成员
        self.assertEqual(sizes, [2, 2])
        srcs = sorted(sorted(set(m["src"] for m in g)) for g in groups)
        self.assertEqual(srcs, [["a.mp4"], ["b.mp4"]])

    def test_no_groups_when_isolated(self):
        samples = [
            {"src": "a.mp4", "frame": 0, "ahash": 0b00000000},
            {"src": "b.mp4", "frame": 0, "ahash": 0b11111111},
        ]
        self.assertEqual(find_groups(samples, threshold=2), [])


class RunDedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_run_dedup_persists_report(self):
        fake_result = {
            "video_count": 2,
            "frame_count": 24,
            "group_count": 1,
            "threshold": 10,
            "groups": [{"size": 2, "members": [{"src": "a.mp4", "frame": 0, "t_rel": 0.0}]}],
        }
        with patch("media_catalog.dedup.scan_videos", return_value=fake_result) as mock:
            report = run_dedup(self.db, "task-1", ["a.mp4", "b.mp4"], scope="park")
        mock.assert_called_once()
        self.assertTrue(report["report_id"].startswith("dedup-"))
        self.assertEqual(report["group_count"], 1)
        reports = list_reports(self.db)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["task_id"], "task-1")
        self.assertEqual(reports[0]["scope"], "park")

    def test_list_reports_filters_by_task(self):
        fake = {"video_count": 1, "frame_count": 12, "group_count": 0, "threshold": 10, "groups": []}
        with patch("media_catalog.dedup.scan_videos", return_value=fake):
            run_dedup(self.db, "task-1", ["a.mp4"], scope="x")
            run_dedup(self.db, "task-2", ["b.mp4"], scope="x")
        self.assertEqual(len(list_reports(self.db, task_id="task-1")), 1)


if __name__ == "__main__":
    unittest.main()
