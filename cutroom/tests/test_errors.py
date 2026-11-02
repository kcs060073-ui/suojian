import sqlite3
import unittest

from media_catalog.errors import classify_error
from media_catalog.digital_workflow import DigitalTaskPreflightError
from media_catalog.jobs import HeavyJobBusy
from media_catalog.scanner import LibraryOffline
from media_catalog.tools import ToolNotFound


class ErrorContractTests(unittest.TestCase):
    def test_digital_preflight_failure_is_actionable_and_retryable(self):
        error = DigitalTaskPreflightError(
            {
                "blocking": [
                    {
                        "code": "FINAL_SCRIPT_REQUIRED",
                        "message": "必须提供最终口播文案",
                    }
                ]
            }
        )
        info = classify_error(error)
        self.assertEqual(info.code, "DIGITAL_TASK_PREFLIGHT_FAILED")
        self.assertTrue(info.retryable)
        self.assertIn("预检", info.next_action)

    def test_offline_library_is_retryable(self):
        info = classify_error(LibraryOffline("素材目录当前不可访问"))
        self.assertEqual(info.code, "LIBRARY_OFFLINE")
        self.assertTrue(info.retryable)
        self.assertEqual(info.next_action, "连接本机硬盘或移动硬盘后告诉我继续。")
        self.assertNotIn("NAS", info.next_action)

    def test_busy_job_is_retryable(self):
        info = classify_error(HeavyJobBusy("已有重任务正在运行"))
        self.assertEqual(info.code, "HEAVY_JOB_BUSY")
        self.assertTrue(info.retryable)

    def test_missing_tool_and_database_error_have_stable_codes(self):
        self.assertEqual(
            classify_error(ToolNotFound("找不到 ffprobe")).code,
            "TOOL_NOT_FOUND",
        )
        self.assertEqual(
            classify_error(sqlite3.DatabaseError("bad db")).code,
            "DATABASE_ERROR",
        )


if __name__ == "__main__":
    unittest.main()
