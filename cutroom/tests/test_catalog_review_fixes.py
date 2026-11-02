"""Behavior regressions for review F01/F02/F05/F16/F19/F20."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from PIL import Image
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.scanner import scan_library, confirm_duplicate_candidate
from media_catalog import privacy, cli, dedup
from media_catalog.audio_analysis import _persist as persist_audio
from media_catalog.match import candidate_text_for_asset, rank
from media_catalog.preview import generate_asset_previews, generate_previews
from media_catalog.provenance import canonical_digest, record_gate, latest_gate


class CatalogReviewFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.db = CatalogDatabase.open(self.root / 'catalog.sqlite')
        self.db.migrate()
        register_library(self.db, 'lib', 'test', 'local', str(self.media))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def assets(self, copies=1):
        for i in range(copies):
            (self.media / f'{i}.mp4').write_bytes(b'identical')
        scan_library(self.db, 'lib')
        return [r[0] for r in self.db.connection.execute('SELECT asset_id FROM assets ORDER BY asset_id')]

    def test_f01_merge_preserves_business_references_and_invalidates_lock(self):
        left, right = self.assets(2)
        with self.db.transaction() as c:
            c.execute("INSERT INTO tasks(task_id,title) VALUES('t','test')")
            c.execute("INSERT INTO task_assets(task_id,asset_id,role) VALUES('t',?,'broll')", (right,))
            c.execute("INSERT INTO segments(segment_id,asset_id,start_sec,end_sec) VALUES('seg',?,0,1)", (right,))
            c.execute("INSERT INTO scene_cards(card_id,task_id,idx,asset_id,segment_id) VALUES('card','t',0,?,'seg')", (right,))
            c.execute("INSERT INTO picture_lock(task_id,lock_version) VALUES('t','v1')")
            c.execute("INSERT INTO selection_decisions(asset_id,decision) VALUES(?,'confirmed')", (right,))
            c.execute("INSERT INTO privacy_flags(flag_id,asset_id,flag_type,confirmed) VALUES('flag',?,'face',1)", (right,))
            c.execute("INSERT INTO packaging_assets(asset_id,kind,name) VALUES(?,'bgm','music')", (right,))
            c.execute("INSERT INTO color_params(asset_id,params_json) VALUES(?,'{}')", (right,))
            c.execute("INSERT INTO preview_cache(asset_id,kind,rel_path) VALUES(?,'thumbnail','old.jpg')", (right,))
        confirm_duplicate_candidate(self.db, left, right)
        for table in ('task_assets','segments','scene_cards','selection_decisions','privacy_flags','packaging_assets','color_params','preview_cache'):
            self.assertEqual(self.db.connection.execute(f'SELECT asset_id FROM {table}').fetchone()[0], left, table)
        self.assertEqual(self.db.connection.execute('SELECT changed_since_lock FROM picture_lock').fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute("SELECT status FROM task_stage_states WHERE stage_key='fine_render'").fetchone()[0], 'invalidated')
        self.assertEqual(self.db.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_f01_conflicting_human_decisions_abort_without_loss(self):
        left, right = self.assets(2)
        with self.db.transaction() as c:
            c.execute("INSERT INTO selection_decisions(asset_id,decision) VALUES(?,'confirmed')", (left,))
            c.execute("INSERT INTO selection_decisions(asset_id,decision) VALUES(?,'rejected')", (right,))
        with self.assertRaisesRegex(ValueError, 'MERGE_CONFLICT'):
            confirm_duplicate_candidate(self.db, left, right)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM assets').fetchone()[0], 2)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM selection_decisions').fetchone()[0], 2)

    def test_f01_merge_rebinds_locked_presenter_outside_task_scope(self):
        left, right = self.assets(2)
        with self.db.transaction() as c:
            c.execute("INSERT INTO tasks(task_id,title,task_type,brief_json) VALUES('t','test','digital_human',?)",
                      (json.dumps({'digital_human_source':{'asset_id':right}}),))
            c.execute("INSERT INTO task_source_settings(task_id) VALUES('t')")
            c.execute("INSERT INTO task_assets(task_id,asset_id,role) VALUES('t',?,'digital_human')", (right,))
            c.execute("INSERT INTO scene_cards(card_id,task_id,idx,asset_id) VALUES('card','t',0,?)", (right,))
        confirm_duplicate_candidate(self.db, left, right)
        self.assertEqual(json.loads(self.db.connection.execute('SELECT brief_json FROM tasks').fetchone()[0])['digital_human_source']['asset_id'], left)
        self.assertEqual(self.db.connection.execute('SELECT asset_id FROM scene_cards').fetchone()[0], left)

    def test_f01_same_content_identity_migration_preserves_valid_brief_approval(self):
        left, right = self.assets(2)
        brief = {'script_text':'unchanged script', 'digital_human_source':{'asset_id':right}}
        with self.db.transaction() as c:
            c.execute("INSERT INTO tasks(task_id,title,task_type,brief_json) VALUES('t','test','digital_human',?)", (json.dumps(brief),))
            original = record_gate(c, 't', 'brief', canonical_digest(brief), brief)
        confirm_duplicate_candidate(self.db, left, right)
        current = json.loads(self.db.connection.execute('SELECT brief_json FROM tasks').fetchone()[0])
        gate = latest_gate(self.db,'t','brief')
        self.assertEqual(gate['gate_digest'], canonical_digest(current))
        self.assertNotEqual(gate['event_id'], original)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM task_gate_events').fetchone()[0], 2)

    def test_f01_identity_migration_never_approves_already_changed_brief(self):
        left, right = self.assets(2)
        brief = {'script_text':'unapproved edit','digital_human_source':{'asset_id':right}}
        with self.db.transaction() as c:
            c.execute("INSERT INTO tasks(task_id,title,task_type,brief_json) VALUES('t','test','digital_human',?)", (json.dumps(brief),))
            original = record_gate(c, 't', 'brief', 'older-brief-digest', {})
        confirm_duplicate_candidate(self.db, left, right)
        self.assertEqual(latest_gate(self.db,'t','brief')['event_id'], original)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM task_gate_events').fetchone()[0], 1)

    def test_f01_conflicting_privacy_decisions_abort_without_loss(self):
        left, right = self.assets(2)
        with self.db.transaction() as c:
            for aid, confirmed in ((left, 1),(right, 0)):
                c.execute("INSERT INTO privacy_flags(flag_id,asset_id,flag_type,notes,confirmed,needs_human_confirm) VALUES(?,?,'face','same hit',?,0)",
                          (aid+':flag',aid,confirmed))
        with self.assertRaisesRegex(ValueError, 'MERGE_CONFLICT'):
            confirm_duplicate_candidate(self.db, left, right)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM privacy_flags').fetchone()[0], 2)

    def test_f01_matching_unique_rows_merge_and_third_candidate_survives(self):
        left, right, other = self.assets(3)
        with self.db.transaction() as c:
            c.execute("INSERT INTO tasks(task_id,title) VALUES('t','test')")
            for aid in (left,right):
                c.execute("INSERT INTO task_assets(task_id,asset_id,role) VALUES('t',?,'broll')", (aid,))
                c.execute("INSERT INTO selection_decisions(asset_id,decision) VALUES(?,'confirmed')", (aid,))
        confirm_duplicate_candidate(self.db, left, right)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM task_assets').fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM selection_decisions').fetchone()[0], 1)
        candidate = self.db.connection.execute('SELECT left_asset_id,right_asset_id FROM duplicate_candidates').fetchone()
        self.assertEqual(tuple(candidate), (left,other))

    def test_f02_changed_copy_splits_identity(self):
        left, right = self.assets(2)
        confirm_duplicate_candidate(self.db, left, right)
        (self.media / '0.mp4').write_bytes(b'changed longer copy')
        scan_library(self.db, 'lib')
        rows = self.db.connection.execute('SELECT relative_path,asset_id,size_bytes FROM asset_locations JOIN assets USING(asset_id) ORDER BY relative_path').fetchall()
        self.assertNotEqual(rows[0]['asset_id'], rows[1]['asset_id'])
        self.assertEqual(rows[1]['asset_id'], left)
        for row in rows:
            self.assertEqual(row['size_bytes'], (self.media / row['relative_path']).stat().st_size)

    def test_f02_touch_unchanged_merged_copy_keeps_identity(self):
        left, right = self.assets(2)
        confirm_duplicate_candidate(self.db, left, right)
        path = self.media / '0.mp4'
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
        scan_library(self.db, 'lib')
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM assets').fetchone()[0], 1)

    def test_f05_changed_content_invalidates_segments_and_human_decisions(self):
        aid = self.assets()[0]
        with self.db.transaction() as c:
            c.execute("INSERT INTO tasks(task_id,title) VALUES('t','test')")
            c.execute("INSERT INTO segments(segment_id,asset_id,start_sec,end_sec) VALUES('seg',?,0,1)", (aid,))
            c.execute("INSERT INTO scene_cards(card_id,task_id,idx,asset_id,segment_id,confirm_status) VALUES('card','t',0,?,'seg',1)", (aid,))
            c.execute("INSERT INTO selection_decisions(asset_id,decision) VALUES(?,'confirmed')", (aid,))
        privacy._persist(self.db, aid, [{'flag_type':'face'}])
        privacy.confirm_flag(self.db, privacy.list_flags(self.db)[0]['flag_id'], True)
        (self.media / '0.mp4').write_bytes(b'new longer content')
        scan_library(self.db, 'lib')
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM segments').fetchone()[0], 0)
        card = self.db.connection.execute('SELECT segment_id,confirm_status FROM scene_cards').fetchone()
        self.assertEqual(tuple(card), (None,0))
        self.assertEqual(self.db.connection.execute('SELECT decision FROM selection_decisions').fetchone()[0], 'candidate')
        self.assertEqual(privacy.list_flags(self.db)[0]['needs_human_confirm'], 1)

    def test_f05_replaced_image_regenerates_preview(self):
        source = self.media / 'image.png'
        Image.new('RGB', (20,20), 'red').save(source)
        scan_library(self.db, 'lib')
        aid = self.db.connection.execute('SELECT asset_id FROM assets').fetchone()[0]
        with self.db.transaction() as c:
            c.execute("INSERT INTO analysis_runs(asset_id,stage,status,updated_at) VALUES(?,'technical_probe','completed',datetime('now'))", (aid,))
        before = generate_asset_previews(self.db, aid, Path('unused'), {'thumbnail'})
        Image.new('RGB', (20,20), 'blue').save(source)
        scan_library(self.db, 'lib')
        with self.db.transaction() as c:
            c.execute("UPDATE analysis_runs SET status='completed' WHERE stage='technical_probe'")
        result = generate_previews(self.db, 'lib', Path('unused'), kinds={'thumbnail'})
        self.assertEqual(result.completed, 1)
        name = self.db.connection.execute('SELECT rel_path FROM preview_cache').fetchone()[0]
        self.assertNotEqual(name, before['thumbnail'])
        with Image.open(self.root / 'cache' / name) as img:
            self.assertGreater(img.getpixel((0,0))[2], 240)

    def test_f05_source_change_during_preview_is_not_persisted(self):
        from media_catalog import preview
        source = self.media / 'image.png'
        Image.new('RGB',(20,20),'red').save(source)
        scan_library(self.db, 'lib')
        aid = self.db.connection.execute('SELECT asset_id FROM assets').fetchone()[0]
        original = preview.make_image_thumbnail
        def mutate(src, out):
            original(src, out)
            Image.new('RGB',(40,40),'blue').save(src)
        with patch.object(preview, 'make_image_thumbnail', side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, 'SOURCE_CHANGED'):
                generate_asset_previews(self.db, aid, Path('unused'), {'thumbnail'})
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM preview_cache').fetchone()[0], 0)

    def test_f16_rescan_preserves_manual_decision_by_hit_not_order(self):
        aid = self.assets()[0]
        a = {'flag_type':'contact', 'notes':'a@example.invalid', 'confidence':0.9}
        b = {'flag_type':'contact', 'notes':'b@example.invalid', 'confidence':0.9}
        privacy._persist(self.db, aid, [a])
        flag_id = privacy.list_flags(self.db)[0]['flag_id']
        privacy.confirm_flag(self.db, flag_id, True)
        privacy._persist(self.db, aid, [b,a])
        flags = {f['notes']: f for f in privacy.list_flags(self.db)}
        self.assertEqual(flags[a['notes']]['flag_id'], flag_id)
        self.assertEqual(flags[a['notes']]['needs_human_confirm'], 0)
        self.assertEqual(flags[b['notes']]['needs_human_confirm'], 1)

    def test_f16_merge_then_rescan_keeps_confirmed_duplicate_hit(self):
        left, right = self.assets(2)
        for aid in (left, right):
            persist_audio(self.db, aid, {'segments':[{'text':'a@example.invalid'}]})
            privacy.scan_privacy(self.db, aid, Path('unused'))
        confirmed_flag = privacy.list_flags(self.db, left)[0]['flag_id']
        privacy.confirm_flag(self.db, confirmed_flag, True)
        confirm_duplicate_candidate(self.db, left, right)
        self.assertEqual(len(privacy.list_flags(self.db, left)), 2)
        privacy.scan_privacy(self.db, left, Path('unused'))
        flags = privacy.list_flags(self.db, left)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['flag_id'], confirmed_flag)
        self.assertEqual(flags[0]['needs_human_confirm'], 0)
        self.assertEqual(flags[0]['confirmed'], 1)

    def test_f16_batch_continues_and_reports_failure(self):
        assets = self.assets(2)
        with self.db.transaction() as c:
            for aid in assets:
                c.execute("INSERT INTO analysis_runs(asset_id,stage,status,updated_at) VALUES(?,'technical_probe','completed',datetime('now'))", (aid,))
        args = argparse.Namespace(priv_command='scan',asset_id=None,library='lib',json=True)
        def scan(db, aid, ff):
            if aid == assets[0]:
                raise RuntimeError('broken input')
            privacy._persist(db, aid, [{'flag_type':'face'}])
        output = io.StringIO()
        with patch.object(cli, '_ffmpeg_path', return_value=Path('unused')), patch.object(cli, 'scan_privacy', side_effect=scan), contextlib.redirect_stdout(output):
            code = cli._privacy(self.db, args)
        result = json.loads(output.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(result['ok'])
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['videos_scanned'], 1)
        self.assertEqual(len(privacy.list_flags(self.db)), 1)

    def test_f16_real_scan_a_then_add_b_and_rescan(self):
        a = self.assets()[0]
        persist_audio(self.db, a, {'segments':[{'text':'a@example.invalid'}]})
        with patch.object(privacy, '_visual_flags', return_value=[]):
            privacy.scan_privacy(self.db, a, Path('unused'))
            old = privacy.list_flags(self.db)[0]['flag_id']
            privacy.confirm_flag(self.db, old, True)
            (self.media / '1.mp4').write_bytes(b'new video')
            scan_library(self.db, 'lib')
            with self.db.transaction() as c:
                for row in c.execute('SELECT asset_id FROM assets').fetchall():
                    c.execute("INSERT INTO analysis_runs(asset_id,stage,status,updated_at) VALUES(?,'technical_probe','completed',datetime('now'))", (row[0],))
                    if row[0] != a:
                        persist_audio(self.db, row[0], {'segments':[{'text':'b@example.invalid'}]})
            output = io.StringIO()
            args = argparse.Namespace(priv_command='scan', asset_id=None, library='lib', json=True)
            with patch.object(cli, '_ffmpeg_path', return_value=Path('unused')), contextlib.redirect_stdout(output):
                self.assertEqual(cli._privacy(self.db, args), 0)
        self.assertEqual(json.loads(output.getvalue())['videos_scanned'], 2)
        self.assertEqual(len(privacy.list_flags(self.db)), 2)
        self.assertEqual(self.db.connection.execute('SELECT confirmed FROM privacy_flags WHERE flag_id=?', (old,)).fetchone()[0], 1)

    def test_f19_real_audio_analysis_contract_and_stale_exclusion(self):
        aid = self.assets()[0]
        persist_audio(self.db, aid, {'segments':[{'text':'新疆数据中心'}]})
        text = candidate_text_for_asset(self.db, aid)
        self.assertEqual(rank('新疆数据中心',[{'text':text}])[0]['score'], 1)
        with self.db.transaction() as c:
            c.execute("UPDATE analysis_runs SET status='stale'")
        self.assertNotIn('新疆', candidate_text_for_asset(self.db, aid))

    def test_f20_uniform_sampling_uses_real_pts_and_configured_ffmpeg(self):
        # The process boundary emits the requested frame and its decoded PTS.
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            if '-show_entries' in cmd:
                return subprocess.CompletedProcess(cmd,0,b'{"streams":[{"duration":"12","start_time":"0"}]}',b'')
            at = float(cmd[cmd.index('-ss')+1]) if '-ss' in cmd else 0
            Image.new('RGB',(8,8),'red').save(cmd[-1])
            return __import__('subprocess').CompletedProcess(cmd,0,b'',f'pts_time:{at + 0.02}'.encode())
        with patch.object(dedup.subprocess, 'run', side_effect=run), patch('media_catalog.tools.find_ffprobe', return_value=Path('probe')):
            samples = dedup.video_hashes(Path('fake.mp4'), Path('configured-ffmpeg'), n=3, tmp=self.root/'frames')
        self.assertEqual([round(s['t_rel'],3) for s in samples], [0.168,0.502,0.835])
        self.assertTrue(all(c[0]=='configured-ffmpeg' for c in calls if '-show_entries' not in c))

    def test_f20_real_ffmpeg_finds_shared_late_frames(self):
        from media_catalog.tools import find_ffmpeg, ToolNotFound
        try:
            ff = find_ffmpeg(os.environ)
        except ToolNotFound as error:
            self.skipTest(str(error))
        samples = []
        for i, first_box in enumerate(('x=0:y=0:w=32:h=64', 'x=0:y=0:w=64:h=32')):
            video = self.root / f'sample{i}.mp4'
            subprocess.run([str(ff), '-y', '-hide_banner', '-loglevel', 'error', '-f','lavfi',
                '-i','color=black:s=64x64:r=10:d=6', '-vf',
                f"drawbox={first_box}:color=white:t=fill:enable='lt(t,3)',drawbox=x=32:y=0:w=32:h=64:color=white:t=fill:enable='gte(t,3)'",
                '-c:v','libx264','-pix_fmt','yuv420p',str(video)], check=True, capture_output=True, timeout=60)
            samples.append(dedup.video_hashes(video, ff, n=6, tmp=self.root/f'frames{i}'))
        self.assertEqual(len(samples[0]), 6)
        self.assertNotEqual(samples[0][0]['ahash'], samples[1][0]['ahash'])
        for a, b in zip(samples[0][3:], samples[1][3:]):
            self.assertEqual(a['ahash'], b['ahash'])
            self.assertGreaterEqual(a['t_sec'], 3)
        self.assertGreater(samples[0][-1]['t_rel'], 0.9)

    def test_f20_nonzero_start_time_returns_media_relative_positions(self):
        from media_catalog.tools import find_ffmpeg, ToolNotFound
        try:
            ff = find_ffmpeg(os.environ)
        except ToolNotFound as error:
            self.skipTest(str(error))
        video = self.root / 'offset.mp4'
        subprocess.run([str(ff),'-y','-hide_banner','-loglevel','error','-f','lavfi',
            '-i','testsrc2=s=64x64:r=10:d=3','-vf','setpts=PTS+10/TB',
            '-c:v','libx264','-pix_fmt','yuv420p',str(video)], check=True,capture_output=True,timeout=60)
        samples = dedup.video_hashes(video, ff, n=3, tmp=self.root/'offset-frames')
        self.assertEqual([round(s['t_sec'],1) for s in samples], [0.5,1.5,2.5])
        self.assertTrue(all(0 <= s['t_rel'] <= 1 for s in samples))


if __name__ == '__main__':
    unittest.main()
