import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog.publish import PACKAGE_FILES, build_publish_package, list_publish
from media_catalog.reviews import reject_review
from tests.review_helpers import approve_review
from media_catalog.snapshots import file_content_signature, record_render_review_candidate


class PublishPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.deliver_root = self.root / "deliverables" / "publish"
        self.video = self.root / "candidate.mp4"
        self.video.write_bytes(b"reviewed-video")
        self.subtitle = self.root / "candidate.ass"
        self.subtitle.write_text("[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,字幕\n", encoding="utf-8")
        self.bgm = self.root / "music.wav"
        self.bgm.write_bytes(b"real-bgm-stem")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,title,status) VALUES('task-1','task','draft')"
            )
        self.qa_report = {
            "report_id": "qa-release",
            "task_id": "task-1",
            "video_path": str(self.video.resolve()),
            "status": "passed",
            "issue_count": 1,
            "blocking_count": 0,
            "issues": [
                {"check": "video_evidence", "severity": "info", "ok": True,
                 "detail": "视频规格通过"}
            ],
            "video_spec": {"duration": 100.0},
        }
        self._insert_qa_report(self.qa_report)
        job = {
            "output_path": str(self.video.resolve()),
            "dependency_digest": "dependency-digest",
            "dependency_snapshot": {
                "files": [
                    {"kind": "ass", "path": str(self.subtitle.resolve()), **file_content_signature(self.subtitle)},
                    {"kind": "bgm", "path": str(self.bgm.resolve()), **file_content_signature(self.bgm)},
                ],
                "scene_cards": [
                    {"scene_card_id": "scene-1", "start": 0.0, "end": 1.0, "asset_id": "asset-1"}
                ],
            },
            "plan_path": str(self.root / "render.plan.json"),
            "ass_path": None,
            "packaging": {"plan_id": "plan-1"},
        }
        self.candidate = record_render_review_candidate(
            self.db, "task-1", job, self.qa_report
        )
        self.approval = approve_review(
            self.db,
            "task-1",
            self.candidate["qa_snapshot_id"],
            self.qa_report["report_id"],
            "Peter",
            "完整观看通过",
        )
        self.ffmpeg_timestamps = []

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _insert_qa_report(self, report):
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO qa_reports(
                  report_id, task_id, video_path, status, issue_count,
                  blocking_count, report_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["report_id"],
                    report["task_id"],
                    report["video_path"],
                    report["status"],
                    report.get("issue_count", len(report.get("issues", []))),
                    report.get("blocking_count", 0),
                    json.dumps(report, ensure_ascii=False),
                ),
            )

    def fake_ffmpeg(self, command, **_kwargs):
        timestamp = command[command.index("-ss") + 1]
        self.ffmpeg_timestamps.append(float(timestamp))
        Path(command[-1]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def build(self, *, task_id="task-1", video_path=None, qa_report=None, **kwargs):
        tags = kwargs.pop("tags", ["园区"])
        with (
            patch("media_catalog.publish.DELIVER_ROOT", self.deliver_root),
            patch("media_catalog.publish.find_ffmpeg", return_value=Path("ffmpeg")),
            patch("media_catalog.publish.subprocess.run", side_effect=self.fake_ffmpeg),
        ):
            return build_publish_package(
                self.db,
                task_id,
                "视频号",
                video_path=video_path or self.video,
                qa_report=self.qa_report if qa_report is None else qa_report,
                title="园区更新",
                description="普通简介",
                tags=tags,
                **kwargs,
            )

    @staticmethod
    def read_check(package):
        return json.loads(
            Path(package["package_dir"], "publish-check.json").read_text("utf-8")
        )

    def assert_rejected_before_write(self, code, **kwargs):
        with self.assertRaisesRegex(ValueError, code):
            self.build(**kwargs)
        self.assertFalse((self.deliver_root / "task-1").exists())
        self.assertEqual(self.ffmpeg_timestamps, [])

    def test_builds_fixed_seven_file_release_package_and_cover_percentages(self):
        package = self.build()
        out_dir = self.deliver_root / "task-1"

        self.assertEqual(
            {path.name for path in out_dir.iterdir()},
            {
                "video-final.mp4",
                "cover-01.jpg",
                "cover-02.jpg",
                "cover-03.jpg",
                "publish-copy.md",
                "publish-check.json",
                "qa-report.md",
            },
        )
        self.assertEqual((out_dir / "video-final.mp4").read_bytes(), b"reviewed-video")
        self.assertEqual(self.ffmpeg_timestamps, [15.0, 50.0, 85.0])
        self.assertEqual(package["package_dir"], str(out_dir))
        self.assertEqual(len(package["files"]), 7)
        check = json.loads((out_dir / "publish-check.json").read_text("utf-8"))
        self.assertEqual(check["qa_report_id"], "qa-release")
        self.assertEqual(check["status"], "cleared")
        self.assertIn("视频规格通过", (out_dir / "qa-report.md").read_text("utf-8"))
        count = self.db.connection.execute(
            "SELECT COUNT(*) FROM publish_records WHERE task_id='task-1'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_selected_cover_and_tags_are_bound_in_manifest_and_record(self):
        package = self.build(cover_choice="candidate-2", tags=["招商", "产业"])
        manifest = self.read_check(package)

        self.assertEqual(Path(package["cover_path"]).name, "cover-02.jpg")
        self.assertEqual(manifest["cover"]["mode"], "candidate")
        self.assertEqual(manifest["cover"]["choice"], "candidate-2")
        self.assertEqual(manifest["tags"], ["招商", "产业"])
        row = self.db.connection.execute(
            "SELECT cover_path,tags FROM publish_records WHERE record_id=?",
            (package["record_id"],),
        ).fetchone()
        self.assertEqual(Path(row["cover_path"]).name, "cover-02.jpg")
        self.assertEqual(json.loads(row["tags"]), ["招商", "产业"])

    def test_pending_cover_is_explicit_and_external_cover_is_copied(self):
        pending = self.build(cover_choice="pending")
        marker = Path(pending["package_dir"], "cover-pending.json")
        self.assertTrue(marker.is_file())
        self.assertEqual(Path(pending["cover_path"]), marker)
        self.assertEqual(self.read_check(pending)["cover"]["mode"], "pending")

        external = self.root / "approved-cover.png"
        external.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 24 + b"IEND\xaeB`\x82"
        )
        copied = self.build(
            cover_choice="external", external_cover_path=external,
        )
        copied_path = Path(copied["cover_path"])
        self.assertEqual(copied_path.name, "cover-external.png")
        self.assertEqual(copied_path.read_bytes(), external.read_bytes())
        self.assertEqual(self.read_check(copied)["cover"]["mode"], "external")

    def test_external_cover_is_validated_before_staging(self):
        invalid = self.root / "not-an-image.png"
        invalid.write_bytes(b"plain text")

        with self.assertRaisesRegex(ValueError, "EXTERNAL_COVER_INVALID"):
            self.build(cover_choice="external", external_cover_path=invalid)

        self.assertEqual(self.ffmpeg_timestamps, [])
        self.assertFalse(self.deliver_root.exists())

    def test_cover_choice_and_tags_participate_in_reuse_identity(self):
        first = self.build(cover_choice="candidate-1", tags=["招商"])
        second = self.build(cover_choice="candidate-2", tags=["招商"])
        third = self.build(cover_choice="candidate-2", tags=["产业"])
        repeated = self.build(cover_choice="candidate-2", tags=["产业"])

        self.assertNotEqual(first["record_id"], second["record_id"])
        self.assertNotEqual(second["record_id"], third["record_id"])
        self.assertEqual(repeated["record_id"], third["record_id"])
        self.assertTrue(repeated["reused"])

    def test_editable_delivery_copies_available_components_and_marks_missing(self):
        package = self.build(delivery_type="editable")
        manifest = json.loads(
            Path(package["package_dir"], "delivery-manifest.json").read_text("utf-8")
        )

        self.assertEqual(manifest["components"]["subtitles"]["status"], "included")
        self.assertEqual(manifest["components"]["bgm_stem"]["status"], "included")
        self.assertEqual(manifest["components"]["sfx_stem"]["status"], "not_available")
        self.assertTrue(Path(package["package_dir"], "subtitles.ass").is_file())
        self.assertTrue(Path(package["package_dir"], "audio-bgm.wav").is_file())
        self.assertTrue(Path(package["package_dir"], "material-timecodes.json").is_file())
        self.assertFalse(Path(package["package_dir"], "audio-sfx.wav").exists())
        self.assertIn("delivery-manifest.json", self.read_check(package)["files"])

    def test_editable_delivery_rejects_component_outside_trusted_roots(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.ass"
            outside.write_text("outside", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "EDITABLE_COMPONENT_UNTRUSTED"):
                self.build(
                    delivery_type="editable",
                    component_overrides={"subtitles": str(outside)},
                )

    def test_editable_delivery_type_participates_in_reuse_identity(self):
        standard = self.build(delivery_type="standard")
        editable = self.build(delivery_type="editable")

        self.assertNotEqual(standard["record_id"], editable["record_id"])
        self.assertEqual(editable["delivery_type"], "editable")

    def test_editable_delivery_reuse_verifies_dynamic_component_signatures(self):
        package = self.build(delivery_type="editable")
        Path(package["package_dir"], "audio-bgm.wav").write_bytes(b"tampered")

        with self.assertRaisesRegex(RuntimeError, "PUBLISH_PACKAGE_CHANGED"):
            self.build(delivery_type="editable")

    def test_blocked_copy_can_be_corrected_in_new_package_and_same_request_reuses(self):
        with (
            patch("media_catalog.publish.DELIVER_ROOT", self.deliver_root),
            patch("media_catalog.publish.find_ffmpeg", return_value=Path("ffmpeg")),
            patch("media_catalog.publish.subprocess.run", side_effect=self.fake_ffmpeg),
        ):
            first = build_publish_package(self.db, "task-1", "test", title="100%", video_path=self.video, qa_report=self.qa_report)
            old_check = Path(first["package_dir"]) / "publish-check.json"
            old_bytes = old_check.read_bytes()
            second = build_publish_package(self.db, "task-1", "test", title="normal", video_path=self.video, qa_report=self.qa_report)
            repeated = build_publish_package(self.db, "task-1", "test", title="normal", video_path=self.video, qa_report=self.qa_report)
        self.assertEqual(first["status"], "blocked")
        self.assertEqual(second["status"], "cleared")
        self.assertNotEqual(first["package_dir"], second["package_dir"])
        self.assertEqual(old_check.read_bytes(), old_bytes)
        self.assertEqual(repeated["record_id"], second["record_id"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(len(self.ffmpeg_timestamps), 6)
        self.assertEqual(len(list_publish(self.db, "task-1")), 2)

    def test_list_publish_exposes_the_actual_seven_file_package_to_the_workbench(self):
        built = self.build()

        records = list_publish(self.db, "task-1")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_id"], built["record_id"])
        self.assertEqual(Path(records[0]["package_dir"]), self.deliver_root / "task-1")
        self.assertEqual(
            [Path(item).name for item in records[0]["files"]],
            list(PACKAGE_FILES),
        )
        self.assertTrue(all(Path(item).is_file() for item in records[0]["files"]))

    def test_publish_record_binds_exact_approved_candidate(self):
        package = self.build()
        row = self.db.connection.execute(
            "SELECT candidate_id,package_status FROM publish_records WHERE record_id=?",
            (package["record_id"],),
        ).fetchone()
        self.assertEqual(row["candidate_id"], self.candidate["candidate_id"])
        self.assertEqual(row["package_status"], "current")

        listed = list_publish(self.db, "task-1")[0]
        self.assertEqual(listed["candidate_id"], self.candidate["candidate_id"])
        self.assertEqual(listed["candidate_label"], "candidate-v1")
        self.assertEqual(listed["package_status"], "current")
        self.assertTrue(listed["is_current"])

    def test_caller_qa_dictionary_is_only_a_report_id_lookup_key(self):
        package = self.build(qa_report={
            "report_id": "qa-release",
            "task_id": "forged-task",
            "video_path": str(self.root / "forged.mp4"),
            "status": "blocked",
            "blocking_count": 99,
            "video_spec": {"duration": 1.0},
        })
        self.assertEqual(package["qa_report_id"], "qa-release")
        self.assertEqual(self.ffmpeg_timestamps, [15.0, 50.0, 85.0])
        self.assertIn(
            "视频规格通过",
            (self.deliver_root / "task-1" / "qa-report.md").read_text("utf-8"),
        )

    def test_unregistered_qa_report_is_rejected(self):
        forged = {**self.qa_report, "report_id": "qa-forged"}
        self.assert_rejected_before_write("QA_REPORT_NOT_FOUND", qa_report=forged)

    def test_database_status_not_caller_status_controls_release(self):
        self.db.connection.execute(
            "UPDATE qa_reports SET status='blocked' WHERE report_id='qa-release'"
        )
        self.db.connection.commit()
        self.assert_rejected_before_write(
            "QA_NOT_PASSED", qa_report={**self.qa_report, "status": "passed"}
        )

    def test_database_blocking_count_controls_release(self):
        self.db.connection.execute(
            "UPDATE qa_reports SET blocking_count=1 WHERE report_id='qa-release'"
        )
        self.db.connection.commit()
        self.assert_rejected_before_write("QA_NOT_PASSED")

    def test_report_json_identity_mismatch_is_rejected(self):
        forged_json = {**self.qa_report, "report_id": "qa-other"}
        self.db.connection.execute(
            "UPDATE qa_reports SET report_json=? WHERE report_id='qa-release'",
            (json.dumps(forged_json),),
        )
        self.db.connection.commit()
        self.assert_rejected_before_write("QA_REPORT_INVALID")

    def test_missing_trusted_candidate_is_rejected(self):
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM review_approvals")
            connection.execute("DELETE FROM candidate_versions")
            connection.execute("DELETE FROM render_snapshots")
        self.assert_rejected_before_write("TRUSTED_CANDIDATE_REQUIRED")

    def test_non_assemble_candidate_is_rejected(self):
        self.db.connection.execute(
            "UPDATE render_snapshots SET producer='manual' "
            "WHERE snapshot_id=?",
            (self.candidate["qa_snapshot_id"],),
        )
        self.db.connection.commit()
        self.assert_rejected_before_write("TRUSTED_CANDIDATE_REQUIRED")

    def test_missing_review_approval_is_rejected(self):
        self.db.connection.execute("DELETE FROM review_approvals")
        self.db.connection.commit()
        self.assert_rejected_before_write("REVIEW_APPROVAL_REQUIRED")

    def test_approval_for_wrong_snapshot_is_rejected(self):
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM review_approvals")
            connection.execute(
                """
                INSERT INTO review_approvals(
                  approval_id,task_id,snapshot_id,qa_report_id,decision,reviewed_by
                ) VALUES('wrong-snapshot','task-1',?,?,'approved','Peter')
                """,
                (self.candidate["mix_snapshot_id"], self.qa_report["report_id"]),
            )
        self.assert_rejected_before_write("REVIEW_APPROVAL_REQUIRED")

    def test_approval_for_wrong_qa_is_rejected(self):
        other = {**self.qa_report, "report_id": "qa-other"}
        self._insert_qa_report(other)
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM review_approvals")
            connection.execute(
                """
                INSERT INTO review_approvals(
                  approval_id,task_id,snapshot_id,qa_report_id,decision,reviewed_by
                ) VALUES('wrong-qa','task-1',?,'qa-other','approved','Peter')
                """,
                (self.candidate["qa_snapshot_id"],),
            )
        self.assert_rejected_before_write("REVIEW_APPROVAL_REQUIRED")

    def test_changed_video_after_review_is_rejected(self):
        self.video.write_bytes(b"changed-after-review")
        self.assert_rejected_before_write("CANDIDATE_VIDEO_CHANGED")

    def test_different_video_path_is_rejected_even_with_forged_caller_dict(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"other")
        forged = {**self.qa_report, "video_path": str(other.resolve())}
        self.assert_rejected_before_write(
            "QA_VIDEO_MISMATCH", video_path=other, qa_report=forged
        )

    def test_existing_task_package_is_never_overwritten(self):
        out_dir = self.deliver_root / "task-1"
        out_dir.mkdir(parents=True)
        sentinel = out_dir / "video-final.mp4"
        sentinel.write_bytes(b"existing")

        with self.assertRaises(FileExistsError):
            self.build()

        self.assertEqual(sentinel.read_bytes(), b"existing")
        self.assertEqual(self.ffmpeg_timestamps, [])

    def test_ffmpeg_failure_rolls_back_new_files_and_database_record(self):
        calls = 0

        def fail_second(command, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"decode failed")
            Path(command[-1]).write_bytes(b"jpeg")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with (
            patch("media_catalog.publish.DELIVER_ROOT", self.deliver_root),
            patch("media_catalog.publish.find_ffmpeg", return_value=Path("ffmpeg")),
            patch("media_catalog.publish.subprocess.run", side_effect=fail_second),
        ):
            with self.assertRaisesRegex(RuntimeError, "COVER_EXTRACTION_FAILED"):
                build_publish_package(
                    self.db, "task-1", "视频号",
                    video_path=self.video, qa_report=self.qa_report,
                )

        self.assertFalse((self.deliver_root / "task-1").exists())
        self.assertFalse(list(self.deliver_root.glob(".task-1.part-*")))
        count = self.db.connection.execute(
            "SELECT COUNT(*) FROM publish_records WHERE task_id='task-1'"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_database_insert_failure_removes_completed_files(self):
        self.db.connection.execute(
            """
            CREATE TRIGGER fail_publish BEFORE INSERT ON publish_records
            BEGIN SELECT RAISE(ABORT, 'forced publish insert failure'); END
            """
        )
        self.db.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.build()

        self.assertFalse((self.deliver_root / "task-1").exists())
        self.assertFalse(list(self.deliver_root.glob(".task-1.part-*")))

    def test_task_directory_escape_is_rejected(self):
        with patch("media_catalog.publish.DELIVER_ROOT", self.deliver_root):
            with self.assertRaisesRegex(ValueError, "INVALID_TASK"):
                build_publish_package(
                    self.db, "../escape", "视频号",
                    video_path=self.video, qa_report=self.qa_report,
                )
        self.assertFalse((self.root / "escape").exists())

    def test_rejection_after_staging_prevents_publish_record_and_final_package(self):
        called = False

        def reject_before_finalization():
            nonlocal called
            called = True
            reject_review(
                self.db,
                "task-1",
                self.candidate["qa_snapshot_id"],
                self.qa_report["report_id"],
                "Peter",
                "发现问题",
            )

        with patch.object(
            __import__("media_catalog.publish", fromlist=["build_publish_package"]),
            "_before_publish_finalize_hook",
            reject_before_finalization,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "REVIEW_APPROVAL_REQUIRED"):
                self.build()

        self.assertTrue(called)
        self.assertFalse((self.deliver_root / "task-1").exists())
        self.assertFalse(list(self.deliver_root.glob(".task-1.part-*")))
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM publish_records WHERE task_id='task-1'"
            ).fetchone()[0],
            0,
        )
    def test_publish_rejects_caller_owned_transaction_before_staging_or_rollback(self):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,title,status) VALUES('caller-marker','marker','draft')"
            )
            try:
                self.build()
            except Exception as error:
                self.assertIsInstance(error, ValueError)
                self.assertIn("ACTIVE_TRANSACTION_UNSUPPORTED", str(error))
            else:
                self.fail("publish must fail before touching a caller-owned transaction")
            self.assertTrue(self.db.connection.in_transaction)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE task_id='caller-marker'"
                ).fetchone()[0],
                1,
            )
            self.assertFalse((self.deliver_root / "task-1").exists())
            self.assertFalse(list(self.deliver_root.glob(".task-1.part-*")))
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_id='caller-marker'"
            ).fetchone()[0],
            1,
        )

if __name__ == "__main__":
    unittest.main()
