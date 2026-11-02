import unittest

from media_catalog.models import JobStatus, MediaKind, ScanSummary


class ModelTests(unittest.TestCase):
    def test_enums_and_summary_are_stable(self):
        self.assertEqual(JobStatus.INTERRUPTED.value, "interrupted")
        self.assertEqual(MediaKind.VIDEO.value, "video")
        self.assertEqual(
            ScanSummary(discovered=3, completed=2, failed=1).remaining,
            0,
        )


if __name__ == "__main__":
    unittest.main()
