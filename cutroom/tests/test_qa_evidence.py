import json
import tempfile
import unittest
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.fingerprint import full_sha256, quick_fingerprint
from media_catalog.libraries import register_library
from media_catalog.qa_evidence import (
    check_loudness,
    check_subtitle_evidence,
    collect_qa_evidence,
)


FFMPEG_EBUR128_MINUS_31 = """
[Parsed_ebur128_0] Summary:
  Integrated loudness:
    I:         -31.0 LUFS
  True peak:
    Peak:       -2.0 dBFS
"""


class QAEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ass_path = self.root / "captions.ass"
        self.ass_path.write_text(
            """[Script Info]
PlayResX: 1080
PlayResY: 1920
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Source Han Serif SC Heavy,93,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,40,40,560,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:09.00,0:00:11.00,Default,,0,0,0,,越界字幕
""",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def assertIssue(self, issues, check, severity):
        self.assertTrue(
            any(item["check"] == check and item["severity"] == severity for item in issues),
            issues,
        )

    def test_subtitle_outside_video_range_blocks(self):
        issues = check_subtitle_evidence(
            self.ass_path, video_duration=10.0, frame_size=(1080, 1920)
        )
        self.assertIssue(issues, "subtitle_timing", "blocking")

    def test_missing_required_subtitle_evidence_does_not_pass(self):
        issues = check_subtitle_evidence(
            None, video_duration=10.0, frame_size=(1080, 1920)
        )
        self.assertIssue(issues, "subtitle_evidence", "blocking")

    def test_valid_ass_proves_structure_timing_lines_and_safe_area(self):
        value = self.ass_path.read_text(encoding="utf-8").replace(
            "0:00:09.00,0:00:11.00", "0:00:01.00,0:00:03.00"
        )
        self.ass_path.write_text(value, encoding="utf-8")

        issues = check_subtitle_evidence(
            self.ass_path, video_duration=10.0, frame_size=(1080, 1920)
        )

        self.assertFalse([item for item in issues if item["severity"] == "blocking"])
        self.assertIssue(issues, "subtitle_evidence", "info")

    def test_loudness_parses_ebur128_and_blocks_outside_policy(self):
        issues = check_loudness(
            FFMPEG_EBUR128_MINUS_31, target_lufs=-16, tolerance_lu=4
        )
        self.assertEqual(issues[0]["severity"], "blocking")
        self.assertFalse(issues[0]["ok"])

    def test_unreadable_loudness_is_unknown_not_passed(self):
        issues = check_loudness("", target_lufs=-16, tolerance_lu=4)
        self.assertFalse(issues[0]["ok"])
        self.assertEqual(issues[0]["severity"], "blocking")


class QAProvenanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.primary = self.root / "primary"
        self.other = self.root / "other"
        self.primary.mkdir()
        self.other.mkdir()
        self.primary_file = self.primary / "primary.mp4"
        self.other_file = self.other / "other.mp4"
        self.primary_file.write_bytes(b"primary-v1")
        self.other_file.write_bytes(b"other-v1")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        register_library(
            self.db, "primary", "本次素材", "local", str(self.primary),
            purpose="task_specific",
        )
        register_library(
            self.db, "other", "其他项目素材", "local", str(self.other),
            purpose="task_specific",
        )
        self.db.connection.execute(
            "INSERT INTO tasks(task_id,title) VALUES('task-1','证据测试')"
        )
        for asset_id, path, library_id in (
            ("asset-primary", self.primary_file, "primary"),
            ("asset-other", self.other_file, "other"),
        ):
            stat = path.stat()
            self.db.connection.execute(
                """
                INSERT INTO assets(
                  asset_id,media_kind,size_bytes,quick_fingerprint,full_sha256,created_at
                ) VALUES(?, 'video', ?, ?, ?, datetime('now'))
                """,
                (asset_id, stat.st_size, quick_fingerprint(path), full_sha256(path)),
            )
            self.db.connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id,library_id,relative_path,mtime_ns,state
                ) VALUES(?,?,?,?, 'available')
                """,
                (asset_id, library_id, path.name, stat.st_mtime_ns),
            )
        self.db.connection.commit()
        with self.db.transaction() as connection:
            with self.db.task_source_write_authorization("task-1"):
                connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id,task_id,library_id,role,relative_path,
                      source_mode,active,priority
                    ) VALUES('scope-primary','task-1','primary','primary','',
                             'primary',1,0)
                    """
                )
        summary = {
            "library_id": "primary",
            "relative_path": "",
            "source_mode": "primary",
            "assets": [{
                "asset_id": "asset-primary",
                "relative_path": self.primary_file.name,
                "size_bytes": self.primary_file.stat().st_size,
                "quick_fingerprint": quick_fingerprint(self.primary_file),
            }],
        }
        self.db.connection.execute(
            """
            INSERT INTO task_source_snapshots(
              snapshot_id,task_id,scope_id,summary_json,asset_count,total_size_bytes
            ) VALUES('snapshot-primary','task-1','scope-primary',?,1,?)
            """,
            (json.dumps(summary), self.primary_file.stat().st_size),
        )
        self.db.connection.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def assertIssue(self, issues, check, severity):
        self.assertTrue(
            any(item["check"] == check and item["severity"] == severity for item in issues),
            issues,
        )

    @staticmethod
    def _packaging():
        return {"items": [{"slot": "subtitle", "license": "commercial-approved"}]}

    def _asset_input(self, asset_id, path, library_id):
        return {
            "asset_id": asset_id,
            "path": str(path),
            "library_id": library_id,
            "quick_fingerprint": quick_fingerprint(path),
            "sha256": full_sha256(path),
        }

    def test_changed_fingerprint_and_unknown_authorization_block(self):
        frozen = self._asset_input("asset-primary", self.primary_file, "primary")
        self.primary_file.write_bytes(b"primary-v2-changed")
        issues = collect_qa_evidence(self.db, "task-1", {
            "dependency_snapshot": {
                "assets": [frozen],
                "packaging": self._packaging(),
            },
            "shot_authorization": {},
        })
        self.assertIssue(issues, "material_fingerprint", "blocking")
        self.assertIssue(issues, "material_authorization", "blocking")

    def test_other_library_source_cannot_satisfy_task_provenance(self):
        frozen = self._asset_input("asset-other", self.other_file, "other")
        issues = collect_qa_evidence(self.db, "task-1", {
            "dependency_snapshot": {
                "assets": [frozen],
                "packaging": self._packaging(),
            },
            "shot_authorization": {"asset-other": "approved"},
        })
        self.assertIssue(issues, "material_scope", "blocking")

    def test_frozen_in_scope_authorized_material_and_packaging_pass(self):
        frozen = self._asset_input("asset-primary", self.primary_file, "primary")
        issues = collect_qa_evidence(self.db, "task-1", {
            "dependency_snapshot": {
                "assets": [frozen],
                "packaging": self._packaging(),
            },
            "shot_authorization": {"asset-primary": "approved"},
        })

        self.assertFalse([item for item in issues if item["severity"] == "blocking"])


if __name__ == "__main__":
    unittest.main()
