import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


try:
    from scripts.prepare_workbench_acceptance import backup_acceptance_database
except ModuleNotFoundError:
    backup_acceptance_database = None

try:
    from scripts.smoke_workbench import SmokeFailure, validate_workbench_snapshot
except ModuleNotFoundError:
    SmokeFailure = RuntimeError
    validate_workbench_snapshot = None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PrepareWorkbenchAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def create_database(self, path: Path, value: str) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence(value) VALUES(?)", (value,))
        connection.commit()
        return connection

    def read_values(self, path: Path) -> list[str]:
        with closing(sqlite3.connect(path)) as connection:
            return [row[0] for row in connection.execute("SELECT value FROM evidence")]

    def require_subject(self):
        self.assertIsNotNone(
            backup_acceptance_database,
            "prepare_workbench_acceptance must expose backup_acceptance_database",
        )

    def test_online_backup_captures_wal_rows_without_changing_source_file(self):
        self.require_subject()
        source = self.root / "source.sqlite"
        destination = self.root / "acceptance" / "copy.sqlite"
        source_connection = self.create_database(source, "real-task")
        try:
            source_hash_before = sha256(source)
            result = backup_acceptance_database(source, destination)
            source_hash_after = sha256(source)
        finally:
            source_connection.close()

        self.assertEqual(self.read_values(destination), ["real-task"])
        self.assertEqual(source_hash_after, source_hash_before)
        self.assertEqual(result.destination, destination.resolve())
        self.assertIsNone(result.previous_backup)
        self.assertEqual(result.integrity_check, "ok")

    def test_existing_destination_is_refused_without_replace_and_left_unchanged(self):
        self.require_subject()
        source = self.root / "source.sqlite"
        destination = self.root / "copy.sqlite"
        source_connection = self.create_database(source, "new")
        destination_connection = self.create_database(destination, "old")
        source_connection.close()
        destination_connection.close()
        destination_hash_before = sha256(destination)

        with self.assertRaisesRegex(FileExistsError, "--replace"):
            backup_acceptance_database(source, destination)

        self.assertEqual(sha256(destination), destination_hash_before)
        self.assertEqual(self.read_values(destination), ["old"])

    def test_replace_preserves_previous_destination_before_installing_fresh_copy(self):
        self.require_subject()
        source = self.root / "source.sqlite"
        destination = self.root / "copy.sqlite"
        source_connection = self.create_database(source, "new")
        destination_connection = self.create_database(destination, "old")
        source_connection.close()
        destination_connection.close()
        source_hash_before = sha256(source)

        result = backup_acceptance_database(source, destination, replace=True)

        self.assertEqual(sha256(source), source_hash_before)
        self.assertEqual(self.read_values(destination), ["new"])
        self.assertIsNotNone(result.previous_backup)
        self.assertTrue(result.previous_backup.is_file())
        self.assertEqual(self.read_values(result.previous_backup), ["old"])
        self.assertNotEqual(result.previous_backup, destination)


class WorkbenchSmokeValidationTests(unittest.TestCase):
    def valid_payloads(self):
        stages = [
            "create_preflight",
            "brief_lock",
            "material_sync",
            "ai_storyboard",
            "rough_cut",
            "picture_order",
            "picture_lock",
            "packaging_confirm",
            "fine_render",
            "auto_qa",
            "full_review",
            "publish_package",
        ]
        task = {
            "taskId": "p2-xinjiang-v2",
            "title": "新疆数字人 v2",
            "route": "digital_human",
        }
        bootstrap = {
            "tasks": [task],
            "workflowStages": [
                {"key": key, "label": f"stage-{index}"}
                for index, key in enumerate(stages)
            ],
            "routes": [
                {"key": "digital_human", "availability": "available"},
                {"key": "park_video", "availability": "coming_later"},
                {"key": "ad_mix", "availability": "coming_later"},
            ],
            "settingsCapabilities": [
                {"capability": "general_text", "configured": True}
            ],
        }
        detail = {
            "task": {"task_id": "p2-xinjiang-v2"},
            "workflow": {
                "gates": {
                    "brief_locked": True,
                    "picture_lock": True,
                    "packaging_confirmed": True,
                    "qa_passed": True,
                    "human_reviewed": True,
                },
                "stages": [
                    {"stage_key": key, "status": "completed"} for key in stages
                ],
            },
            "reviewCandidate": {
                "mix_snapshot_id": "mix-1",
                "qa_snapshot_id": "qa-1",
                "qa_report_id": "report-1",
                "producer": "assemble-v1",
            },
            "watchSession": {"status": "completed", "can_approve": True},
            "publishPackages": [
                {
                    "package_complete": True,
                    "files": [
                        "video-final.mp4",
                        "cover-01.jpg",
                        "cover-02.jpg",
                        "cover-03.jpg",
                        "publish-copy.md",
                        "publish-check.json",
                        "qa-report.md",
                    ],
                }
            ],
        }
        return bootstrap, {"tasks": [task]}, detail

    def require_subject(self):
        self.assertIsNotNone(
            validate_workbench_snapshot,
            "smoke_workbench must expose validate_workbench_snapshot",
        )

    def test_complete_real_projection_passes_all_snapshot_checks(self):
        self.require_subject()
        bootstrap, tasks, detail = self.valid_payloads()

        checks = validate_workbench_snapshot(
            bootstrap,
            tasks,
            detail,
            task_id="p2-xinjiang-v2",
        )

        self.assertEqual(
            set(checks),
            {
                "task_present",
                "board_list_same_source",
                "workflow_twelve_steps",
                "four_gates_and_qa",
                "trusted_review_candidate",
                "full_watch",
                "seven_file_publish_package",
                "paid_video_generation_disabled",
                "responses_contain_no_credentials",
            },
        )
        self.assertTrue(all(checks.values()))

    def test_snapshot_validation_rejects_list_drift_gate_bypass_and_credentials(self):
        self.require_subject()
        bootstrap, tasks, detail = self.valid_payloads()
        tasks["tasks"] = []
        detail["workflow"]["gates"]["human_reviewed"] = False
        bootstrap["leakedSecret"] = "must-not-appear"

        with self.assertRaises(SmokeFailure) as raised:
            validate_workbench_snapshot(
                bootstrap,
                tasks,
                detail,
                task_id="p2-xinjiang-v2",
            )

        message = str(raised.exception)
        self.assertIn("看板/列表", message)
        self.assertIn("完整审片", message)
        self.assertIn("凭据", message)


if __name__ == "__main__":
    unittest.main()
