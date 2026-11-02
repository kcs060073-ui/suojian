"""Publishing retries must retain the same authority checks as first publication."""
from pathlib import Path
import unittest
from unittest.mock import patch

from media_catalog import publish
from media_catalog.reviews import reject_review
from media_catalog.snapshots import record_render_review_candidate
from tests.review_helpers import approve_review
from tests import test_publish_package as fixtures


class PublishReentryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PublishPackageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.first = self.fixture.build()

    def test_revoked_approval_during_reuse_cannot_return_cleared_package(self):
        fixture = self.fixture
        original = publish.file_content_signature
        revoked = False

        def signature(path):
            nonlocal revoked
            if Path(path).name == 'cover-01.jpg' and not revoked:
                revoked = True
                reject_review(fixture.db, 'task-1', fixture.candidate['qa_snapshot_id'],
                    fixture.qa_report['report_id'], 'review-test', 'New issue found')
            return original(path)

        with patch.object(publish, 'file_content_signature', side_effect=signature):
            with self.assertRaisesRegex(ValueError, 'REVIEW_APPROVAL_REQUIRED'):
                fixture.build()
        self.assertTrue(revoked)
        self.assertTrue(Path(self.first['package_dir']).is_dir())
        self.assertEqual(fixture.db.connection.execute('SELECT COUNT(*) FROM publish_records').fetchone()[0], 1)

    def test_changed_publish_record_during_reuse_is_rejected(self):
        fixture = self.fixture
        original = publish.file_content_signature
        changed = False

        def signature(path):
            nonlocal changed
            if Path(path).name == 'cover-01.jpg' and not changed:
                changed = True
                with fixture.db.transaction() as connection:
                    connection.execute('UPDATE publish_records SET status=? WHERE record_id=?',
                        ('blocked', self.first['record_id']))
            return original(path)

        with patch.object(publish, 'file_content_signature', side_effect=signature):
            with self.assertRaisesRegex(RuntimeError, 'PUBLISH_PACKAGE_BINDING_CHANGED'):
                fixture.build()
        self.assertTrue(changed)

    def test_changed_candidate_bytes_during_reuse_are_rejected(self):
        fixture = self.fixture
        original = publish.file_content_signature
        changed = False

        def signature(path):
            nonlocal changed
            if Path(path).name == 'cover-01.jpg' and not changed:
                changed = True
                fixture.video.write_bytes(b'changed-after-initial-validation')
            return original(path)

        with patch.object(publish, 'file_content_signature', side_effect=signature):
            with self.assertRaisesRegex(ValueError, 'CANDIDATE_VIDEO_CHANGED'):
                fixture.build()
        self.assertTrue(changed)

    def test_old_package_cannot_be_reused_for_new_candidate(self):
        fixture = self.fixture
        new_video = fixture.root / "candidate-v2.mp4"
        new_video.write_bytes(b"reviewed-video-v2")
        new_qa = {
            **fixture.qa_report,
            "report_id": "qa-release-v2",
            "video_path": str(new_video.resolve()),
        }
        fixture._insert_qa_report(new_qa)
        new_candidate = record_render_review_candidate(
            fixture.db,
            "task-1",
            {
                "output_path": str(new_video.resolve()),
                "dependency_digest": "dependency-digest-v2",
                "dependency_snapshot": {"files": []},
                "plan_path": str(fixture.root / "render-v2.plan.json"),
                "ass_path": None,
                "packaging": {"plan_id": "plan-2"},
            },
            new_qa,
        )
        approve_review(
            fixture.db,
            "task-1",
            new_candidate["qa_snapshot_id"],
            new_qa["report_id"],
            "Peter",
        )
        new = fixture.build(video_path=new_video, qa_report=new_qa)
        self.assertNotEqual(self.first["record_id"], new["record_id"])
        self.assertNotEqual(self.first["candidate_id"], new["candidate_id"])
        self.assertFalse(new["reused"])
