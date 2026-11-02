import json
import tempfile
import unittest
import io
from contextlib import redirect_stderr
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.packaging_plan import confirm_plan
from media_catalog.reviews import approve_review
from media_catalog.scene_cards import add_scene_card, set_audio_policy, set_mix_form, set_picture_lock
from media_catalog.tasks import add_asset, set_status
from media_catalog.cli import build_parser, main


class Task5ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        (self.media / "digital.mp4").write_bytes(b"digital")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO libraries(library_id,name,kind,mount_path,online,created_at,updated_at) VALUES('lib','lib','local',?,1,datetime('now'),datetime('now'))", (str(self.media),))
            conn.execute("INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) VALUES('digital','video',7,'q',datetime('now'))")
            conn.execute("INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,state) VALUES('digital','lib','digital.mp4',1,'available')")
            conn.execute(
                "INSERT INTO tasks(task_id,title,task_type,status,brief_json) "
                "VALUES('t','t','digital_human','draft',?)",
                (json.dumps({"text": "locked", "subtype": "zhaoshang"}, ensure_ascii=False),),
            )
            conn.execute("INSERT INTO task_assets(task_id,asset_id,role) VALUES('t','digital','digital_human')")
            conn.execute("INSERT INTO scene_cards(card_id,task_id,idx,narrative_start,narrative_end,asset_id,mix_form,confirm_status) VALUES('t#1','t',1,0,3,'digital','数字人全屏',1)")
            conn.execute("INSERT INTO packaging_plans(plan_id,task_id,route,plan_name,status) VALUES('p','t','数字人混剪','p','draft')")
            conn.execute(
                "INSERT INTO packaging_plan_items(plan_id,slot,asset_ref) "
                "VALUES('p','subtitle','sub-modern-clean-v1')"
            )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_migration_adds_provenance_tables_and_columns(self):
        tables = {r[0] for r in self.db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("review_approvals", tables)
        self.assertIn("task_gate_events", tables)
        picture_columns = {r[1] for r in self.db.connection.execute("PRAGMA table_info(picture_lock)")}
        snapshot_columns = {r[1] for r in self.db.connection.execute("PRAGMA table_info(render_snapshots)")}
        self.assertIn("selection_digest", picture_columns)
        self.assertTrue({"producer", "input_digest"}.issubset(snapshot_columns))

    def test_picture_lock_requires_brief_gate_and_asset_change_invalidates_it(self):
        with self.assertRaisesRegex(RuntimeError, "BRIEF_GATE_MISSING"):
            set_picture_lock(self.db, "t")
        set_status(self.db, "t", "brief_locked")
        locked = set_picture_lock(self.db, "t")
        self.assertTrue(locked["selection_digest"])
        add_asset(self.db, "t", "digital", "digital_human")
        changed = self.db.connection.execute("SELECT changed_since_lock FROM picture_lock WHERE task_id='t'").fetchone()[0]
        self.assertEqual(changed, 1)

    def test_picture_lock_rejects_any_unconfirmed_scene(self):
        set_status(self.db, "t", "brief_locked")
        self.db.connection.execute(
            "UPDATE scene_cards SET confirm_status=0 WHERE task_id='t'"
        )
        self.db.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "SCENE_CONFIRMATION_REQUIRED"):
            set_picture_lock(self.db, "t")

    def test_set_mix_form_rejects_locked_opaque_card_and_directs_to_rough_cut(self):
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO picture_lock(task_id,lock_version) VALUES('t','v1')"
            )
            conn.execute(
                "UPDATE scene_cards SET card_id='opaque-card' WHERE card_id='t#1'"
            )

        with self.assertRaisesRegex(RuntimeError, "IMPACT_PREVIEW_REQUIRED|rough_cut"):
            set_mix_form(self.db, "opaque-card", "画中画")
        self.assertEqual(
            self.db.connection.execute(
                "SELECT mix_form FROM scene_cards WHERE card_id='opaque-card'"
            ).fetchone()[0],
            "数字人全屏",
        )

    def test_set_audio_policy_rejects_locked_opaque_card_and_directs_to_rough_cut(self):
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO picture_lock(task_id,lock_version) VALUES('t','v1')"
            )
            conn.execute(
                "UPDATE scene_cards SET card_id='opaque-card' WHERE card_id='t#1'"
            )

        with self.assertRaisesRegex(RuntimeError, "IMPACT_PREVIEW_REQUIRED|rough_cut"):
            set_audio_policy(self.db, "opaque-card", "duck")
        self.assertIsNone(
            self.db.connection.execute(
                "SELECT audio_policy_draft FROM scene_cards WHERE card_id='opaque-card'"
            ).fetchone()[0]
        )

    def test_cli_scene_card_write_cannot_bypass_picture_lock(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO picture_lock(task_id,lock_version) VALUES('t','v1')")
        self.db.close()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main([
                "--db", str(self.root / "catalog.sqlite"),
                "scene-card", "set-audio", "--card", "t#1", "--audio", "duck",
            ])
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.assertEqual(code, 1)
        self.assertRegex(stderr.getvalue(), "IMPACT_PREVIEW_REQUIRED|预览|rough_cut")
        self.assertIsNone(
            self.db.connection.execute(
                "SELECT audio_policy_draft FROM scene_cards WHERE card_id='t#1'"
            ).fetchone()[0]
        )

    def test_add_scene_card_update_cannot_bypass_picture_lock(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO picture_lock(task_id,lock_version) VALUES('t','v1')")
        before = dict(self.db.connection.execute(
            "SELECT * FROM scene_cards WHERE card_id='t#1'"
        ).fetchone())

        with self.assertRaisesRegex(RuntimeError, "IMPACT_PREVIEW_REQUIRED|rough_cut"):
            add_scene_card(self.db, "t", 1, narrative_text="legacy overwrite")

        self.assertEqual(
            dict(self.db.connection.execute(
                "SELECT * FROM scene_cards WHERE card_id='t#1'"
            ).fetchone()),
            before,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM rough_cut_edits WHERE task_id='t'"
            ).fetchone()[0],
            0,
        )

    def test_cli_scene_card_add_update_cannot_bypass_picture_lock(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO picture_lock(task_id,lock_version) VALUES('t','v1')")
        before = dict(self.db.connection.execute(
            "SELECT * FROM scene_cards WHERE card_id='t#1'"
        ).fetchone())
        self.db.close()
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            code = main([
                "--db", str(self.root / "catalog.sqlite"),
                "scene-card", "add", "--task", "t", "--idx", "1",
                "--narrative", "legacy overwrite",
            ])

        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.assertEqual(code, 1)
        self.assertRegex(stderr.getvalue(), "IMPACT_PREVIEW_REQUIRED|预览|rough_cut")
        self.assertEqual(
            dict(self.db.connection.execute(
                "SELECT * FROM scene_cards WHERE card_id='t#1'"
            ).fetchone()),
            before,
        )

    def test_packaging_confirmation_requires_unchanged_picture_lock_and_records_gate(self):
        set_status(self.db, "t", "brief_locked")
        set_picture_lock(self.db, "t")
        self.db.connection.execute("UPDATE picture_lock SET changed_since_lock=1 WHERE task_id='t'")
        self.db.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "PICTURE_LOCK_CHANGED"):
            confirm_plan(self.db, "p")
        set_picture_lock(self.db, "t")
        confirm_plan(self.db, "p")
        gates = [r[0] for r in self.db.connection.execute("SELECT gate_type FROM task_gate_events WHERE task_id='t' ORDER BY rowid")]
        self.assertEqual(gates, ["brief", "picture_lock", "picture_lock", "packaging"])

    def test_review_approval_only_accepts_trusted_bound_candidate(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO qa_reports(report_id,task_id,video_path,status,report_json) VALUES('qa','t',?,'passed',?)", (str(self.root/'out.mp4'), json.dumps({'status':'passed'})))
            conn.execute("INSERT INTO render_snapshots(snapshot_id,task_id,stage,params_json,output_path,producer,input_digest) VALUES('mix','t','mix','{}',?,'manual','digest')", (str(self.root/'out.mp4'),))
            conn.execute("INSERT INTO render_snapshots(snapshot_id,task_id,stage,params_json,output_path,parent_snapshot,producer,input_digest) VALUES('qa-snap','t','qa',?,?,'mix','manual','digest')", (json.dumps({'qa_report_id':'qa'}), str(self.root/'out.mp4')))
        with self.assertRaisesRegex(RuntimeError, "UNTRUSTED_REVIEW_CANDIDATE"):
            approve_review(self.db, "t", "qa-snap", "qa", "Peter", "watched")

    def test_cli_exposes_review_approve_but_not_snapshot_add_or_publish_build(self):
        parser = build_parser()
        args = parser.parse_args([
            "review", "approve", "--task", "t", "--snapshot", "s",
            "--qa", "q", "--by", "Peter", "--watch-session", "watch-1",
            "--notes", "watched",
        ])
        self.assertEqual(args.reviewed_by, "Peter")
        for argv in (("snapshot", "add"), ("publish", "build")):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(list(argv))

    def test_scene_card_partial_narrative_update_cannot_reverse_existing_window(self):
        before = dict(self.db.connection.execute(
            "SELECT * FROM scene_cards WHERE card_id='t#1'"
        ).fetchone())
        with self.assertRaisesRegex(ValueError, "叙事.*顺序|结束时间"):
            add_scene_card(self.db, "t", 1, narrative_start=4.0)
        self.assertEqual(
            dict(self.db.connection.execute(
                "SELECT * FROM scene_cards WHERE card_id='t#1'"
            ).fetchone()),
            before,
        )

    def test_scene_card_rejects_half_source_window_before_write(self):
        before = dict(self.db.connection.execute(
            "SELECT * FROM scene_cards WHERE card_id='t#1'"
        ).fetchone())
        with self.assertRaisesRegex(ValueError, "源.*同时|完整"):
            add_scene_card(self.db, "t", 1, enter_tc=2.0)
        self.assertEqual(
            dict(self.db.connection.execute(
                "SELECT * FROM scene_cards WHERE card_id='t#1'"
            ).fetchone()),
            before,
        )


if __name__ == "__main__":
    unittest.main()
