"""Regression cases for owned images and release QA warning semantics."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog.material_authorization import (
    confirm_material_authorization,
    confirm_self_produced_material,
    shot_authorization,
)
from media_catalog.qa import _check_freeze, _image_scene_intervals
from media_catalog.qa_evidence import RELEASE_QA_CHECKS, qa_evidence_complete


class SelfProducedMaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase.open(Path(self.tmp.name) / "catalog.sqlite")
        self.db.migrate()
        with self.db.transaction() as connection:
            for task_id in ("first", "second"):
                connection.execute(
                    "INSERT INTO tasks(task_id,title) VALUES(?,?)",
                    (task_id, task_id),
                )
            connection.execute(
                """INSERT INTO assets(
                    asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                ) VALUES('photo','image',100,'fingerprint-a',datetime('now'))"""
            )
            for task_id in ("first", "second"):
                connection.execute(
                    """INSERT INTO scene_cards(
                        card_id,task_id,idx,asset_id,confirm_status,created_at,updated_at
                    ) VALUES(?,?,0,'photo','confirmed',datetime('now'),datetime('now'))""",
                    (f"card-{task_id}", task_id),
                )
                connection.execute(
                    """INSERT INTO picture_lock(
                        task_id,lock_version,locked_at,changed_since_lock,selection_digest
                    ) VALUES(?,1,datetime('now'),0,?)""",
                    (task_id, f"digest-{task_id}"),
                )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_self_produced_approval_reuses_exact_asset_on_another_task(self):
        confirm_self_produced_material(self.db, "first", "reviewer")
        self.assertEqual(shot_authorization(self.db, "second"), {"photo": "approved"})

    def test_changed_asset_fingerprint_invalidates_reusable_approval(self):
        confirm_self_produced_material(self.db, "first", "reviewer")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE assets SET quick_fingerprint='fingerprint-b' WHERE asset_id='photo'"
            )
        self.assertEqual(shot_authorization(self.db, "second"), {})

    def test_existing_task_attestation_does_not_become_reusable(self):
        confirm_material_authorization(self.db, "first", "reviewer")
        self.assertEqual(shot_authorization(self.db, "first"), {"photo": "approved"})
        self.assertEqual(shot_authorization(self.db, "second"), {})

    def test_candidate_image_intervals_use_scene_times_and_asset_kind(self):
        evidence = {"dependency_snapshot": {"scene_cards": [
            {"asset_id": "photo", "start": 2.79, "end": 8.67},
        ]}}
        self.assertEqual(_image_scene_intervals(self.db, evidence), [(2.79, 8.67)])


class ImageAwareQaTests(unittest.TestCase):
    FREEZE_LOG = "\n".join((
        "lavfi.freezedetect.freeze_start: 3.166667",
        "lavfi.freezedetect.freeze_duration: 5.166667",
        "lavfi.freezedetect.freeze_end: 8.333333",
    ))

    def test_still_photo_interval_does_not_report_playback_freeze(self):
        with patch("media_catalog.qa._run_ff", return_value=self.FREEZE_LOG):
            issues = _check_freeze(
                Path("ffmpeg"), Path("candidate.mp4"),
                {"noise": 0.003, "min_dur": 2, "warn_above": 3.0},
                image_intervals=[(2.79, 8.67)],
            )
        self.assertEqual(issues[0]["ok"], True)

    def test_frozen_video_outside_photo_interval_still_warns(self):
        with patch("media_catalog.qa._run_ff", return_value=self.FREEZE_LOG):
            issues = _check_freeze(
                Path("ffmpeg"), Path("candidate.mp4"),
                {"noise": 0.003, "min_dur": 2, "warn_above": 3.0},
                image_intervals=[(0.0, 2.79)],
            )
        self.assertEqual(issues[0]["check"], "freeze")
        self.assertEqual(issues[0]["severity"], "warn")

    def test_present_warnings_do_not_fail_release_evidence(self):
        issues = [
            {"check": check, "ok": True, "severity": "info"}
            for check in RELEASE_QA_CHECKS
        ]
        for issue in issues:
            if issue["check"] in {"freeze", "clipping"}:
                issue.update(ok=False, severity="warn")
        self.assertTrue(qa_evidence_complete({"issues": issues}))
        self.assertFalse(qa_evidence_complete({
            "issues": [issue for issue in issues if issue["check"] != "freeze"]
        }))


if __name__ == "__main__":
    unittest.main()
