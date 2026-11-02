import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog import packaging_plan
from media_catalog.reference_editing import bind_reference_policy, motion_windows
from media_catalog.qa import _check_video_spec, _check_repeated_shots

class WorkflowRepairTests(unittest.TestCase):
    def test_template_roundtrip_clone_keeps_frozen_rules_without_reference_video(self):
        from tests.test_packaging_composition import PackagingCompositionTests
        fixture=PackagingCompositionTests()
        fixture.setUp()
        try:
            plan=fixture._complete_draft()
            policy={'schema':1,'reference_source':'missing-original.mp4','reference_sha256':'abc','shots':[{'duration':2,'motion':{'type':'pan_left','confidence':.7}}],
                    'subtitle':{'max_lines':1,'primary_hex':'#FFE820'},'emphasis':{'enabled':True},'task_id':'old','plan_id':plan}
            saved=packaging_plan.save_personal_template(fixture.db,plan,'complete',reference_policy=policy)
            fixture._create_task('task-2',lock=True)
            cloned=packaging_plan.clone_plan(fixture.db,saved['template_id'],task_id='task-2')
            restored=packaging_plan.reference_policy_for_plan(fixture.db,cloned)
            self.assertEqual(restored['subtitle']['primary_hex'],'#FFE820')
            self.assertNotIn('task_id',restored)
            path=bind_reference_policy(fixture.root,'task-2',cloned,restored)
            self.assertTrue(Path(path).is_file())
            cloned_again=packaging_plan.clone_plan(fixture.db,cloned)
            self.assertEqual(packaging_plan.reference_policy_for_plan(fixture.db,cloned_again),restored)
        finally:
            fixture.tearDown()

    def test_landscape_checks_intended_canvas_and_still_rejects_portrait(self):
        payload={'streams':[{'codec_type':'video','codec_name':'h264','width':1920,'height':1080,'avg_frame_rate':'30/1'}, {'codec_type':'audio'}],'format':{'duration':'4'}}
        def run(ratio):
            with patch('media_catalog.qa.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(payload).encode(),stderr=b'')):
                return _check_video_spec(Path('ffprobe'),Path('test.mp4'),expected_ratio=ratio)[0]
        self.assertFalse([r for r in run('16:9') if r['severity']=='blocking'])
        self.assertTrue([r for r in run('9:16') if r['check']=='aspect_ratio' and not r['ok']])

    def test_repeated_check_uses_final_clips_and_reports_real_loop(self):
        plan={'segments':[{'src':'a.mp4'},{'src':'b.mp4'}], 'referenceMatch':{'shots':[{'shot':2,'substitution':'exact','short_material_loop':True}]}}
        result=_check_repeated_shots(None,'task',plan)
        self.assertTrue(result[0]['ok'])
        self.assertEqual(result[1]['check'],'short_material_loop')
        plan['segments'].append({'src':'a.mp4'})
        self.assertFalse(_check_repeated_shots(None,'task',plan)[0]['ok'])

    def test_windows_include_middle_end_and_are_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); asset=root/'a.mp4';asset.write_bytes(b'fixture')
            probe={'duration':12,'type':'static'}
            with patch('media_catalog.reference_editing.detect_camera_motion',return_value=probe) as detect:
                rows=motion_windows(root,asset,4,probe)
                self.assertEqual([x[0] for x in rows],[0,4,8])
                motion_windows(root,asset,4,probe)
                self.assertEqual(detect.call_count,3)
                asset.write_bytes(b'changed')
                motion_windows(root,asset,4,probe)
                self.assertEqual(detect.call_count,6)


    def test_long_material_stays_in_opening_unless_segment_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);asset=root/'long.mp4';asset.write_bytes(b'fixture')
            probe={'duration':1200,'type':'static'}
            with patch('media_catalog.reference_editing.detect_camera_motion',return_value=probe):
                self.assertTrue(all(start+4<=12 for start,_ in motion_windows(root,asset,4,probe)))
                rows=motion_windows(root,asset,4,probe,allowed_start=100,allowed_end=110)
                self.assertEqual([start for start,_ in rows],[100,103,106])
                self.assertEqual(motion_windows(root,asset,4,probe,allowed_start=100,allowed_end=102),[])

if __name__=='__main__':unittest.main()
