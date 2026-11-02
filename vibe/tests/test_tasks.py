"""Offline tool tests; real media verification, no new service/API/package install."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from workbench.tasks import run_tool
from workbench.paths import PYTHON as VENV, VENDOR

class TasksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); cls.root=Path(cls.temp.name)
        cls.project=cls.root/'project'; cls.project.mkdir()
        cls.context={'root':cls.root,'output':cls.root/'outputs','project':cls.project,'vendor':VENDOR,'python':sys.executable}
        cls.media=cls.root/'test.mp4'; cls.audio=cls.root/'voice.wav'
        if not shutil.which('ffmpeg'): raise unittest.SkipTest('ffmpeg unavailable')
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=180x320:rate=30:duration=2','-f','lavfi','-i','sine=frequency=440:duration=2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(cls.media)],check=True)
        subprocess.run(['ffmpeg','-v','error','-i',str(cls.media),'-vn',str(cls.audio)],check=True)
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def run_tool(self,action,payload=None,context=None): return run_tool(action,payload,context or self.context)
    def test_catalog_reads_exact_local_cards_and_variants(self):
        if not VENDOR.exists(): self.skipTest('vendor absent')
        result=self.run_tool('catalog')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['cards']),124)
        self.assertEqual(sum(len(c['styles']) for c in result['cards']),150)
        self.assertTrue(all(c['sourceAvailable'] for c in result['cards']))
        self.assertIn('bilingual-subtitles',result['excluded'])
    def test_research_snapshot_hash_and_unverified_facts(self):
        text='厂房层高 8 米。设备重量为 20 吨。所有数字需要现场确认。'
        result=self.run_tool('research',{'text':text})
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['sources'][0]['sha256'],hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result['facts'][0]['status'],'needs_review')
        self.assertTrue(Path(result['sources'][0]['snapshot']).is_file())
    def test_storyboard_keeps_source_caption_frame_boundaries(self):
        result=self.run_tool('storyboard',{'paragraphs':[{'label':'设备','text':'设备进得来吗？'}],'subtitles':[{'text':'设备进得来吗？','shot':1,'from':5,'to':65}]})
        self.assertEqual((result['shots'][0]['from'],result['shots'][0]['to']),(5,65))
        self.assertEqual(result['constraints']['presenterPolicy'],'selected-version')
        self.assertEqual(result['timing'],'source-frame-exact')
    def test_new_storyboard_does_not_reuse_unrelated_existing_caption_times(self):
        folder=self.project/'src'; folder.mkdir(exist_ok=True)
        file=folder/'full-narration-subs.json'; file.write_text('[{"shot":1,"from":500,"to":800,"text":"旧口播"}]',encoding='utf-8')
        result=self.run_tool('storyboard',{'text':'一个新的例子。'})
        self.assertEqual(result['shots'][0]['from'],0)
        self.assertEqual(result['timing'],'estimated-needs-audio-alignment')
        file.unlink()
    def test_audio_measures_actual_encoded_media(self):
        result=self.run_tool('audio_analyze',{'path':str(self.media),'cuts':[15]})
        self.assertEqual(result['status'],'completed',result)
        self.assertIn('input_i',result['loudness'])
        self.assertGreater(len(result['beat']['rms']),10)
        self.assertFalse(result['beat']['verified'])
    def test_tts_existing_copies_without_changing_source(self):
        before=hashlib.sha256(self.audio.read_bytes()).hexdigest()
        result=self.run_tool('tts',{'engine':'existing','path':str(self.audio)})
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['artifacts'][0]['sha256'],before)
        self.assertEqual(hashlib.sha256(self.audio.read_bytes()).hexdigest(),before)
    def test_qc_decodes_and_produces_measured_frames(self):
        result=self.run_tool('qc',{'path':str(self.media)})
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(result['decodePassed'])
        self.assertEqual(len(result['frameMetrics']),4)
        self.assertIn('max_volume',result['volumeDb'])
        self.assertTrue(any(a['name']=='contact-sheet.jpg' for a in result['artifacts']))
    def test_capture_import_preserves_bytes(self):
        result=self.run_tool('capture',{'path':str(self.media)})
        self.assertEqual(result['artifacts'][0]['sha256'],hashlib.sha256(self.media.read_bytes()).hexdigest())
    def test_publish_prepare_never_publishes(self):
        result=self.run_tool('publish_prepare',{'video':str(self.media),'platforms':['youtube'],'title':'设备能进来吗'})
        self.assertEqual(result['status'],'completed')
        self.assertFalse(result['published'])
        self.assertTrue(result['dryRunOnly'])
        self.assertNotIn('UPLOAD_POST_USER',Path(result['artifacts'][0]['path']).read_text(encoding='utf-8'))
    def test_package_excludes_secrets_and_dependencies(self):
        (self.project/'main.tsx').write_text('export const hello=1;',encoding='utf-8')
        (self.project/'.env').write_text('SECRET=neverinclude',encoding='utf-8')
        (self.project/'node_modules').mkdir(exist_ok=True); (self.project/'node_modules/x').write_text('skip')
        result=self.run_tool('package')
        with zipfile.ZipFile(result['artifacts'][0]['path']) as z:
            self.assertIn('project/main.tsx',z.namelist())
            self.assertNotIn('project/.env',z.namelist())
            self.assertNotIn('project/node_modules/x',z.namelist())
            self.assertEqual(json.loads(z.read('manifest.json'))['files'][0]['sha256'],hashlib.sha256((self.project/'main.tsx').read_bytes()).hexdigest())
    def test_export_requires_clean_plate(self):
        result=self.run_tool('export_draft',{'video':str(self.media)})
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['artifacts'])
    def test_export_real_native_text_audio_and_volume_envelope(self):
        if not VENV.exists(): self.skipTest('existing pyJianYingDraft runtime absent')
        context={**self.context,'python':str(VENV)}
        result=self.run_tool('export_draft',{'clean_plate':str(self.media),'fps':30,'shots':[{'from':0,'to':29},{'from':30,'to':59}],
                'subtitles':[{'text':'设备进得来吗','from':5,'to':45}],
                'voice':[{'path':str(self.audio),'from':0,'to':14,'sourceFrom':15,'speed':2}],
                'bgm':[{'path':str(self.audio),'volume':.2,'envelope':[{'frame':0,'volume':0},{'frame':15,'volume':.2},{'frame':59,'volume':0}]}],
                'sfx':[{'path':str(self.audio),'start':.5,'duration':.3,'volume':.1}]},context)
        self.assertEqual(result['status'],'completed',result)
        draft=json.loads(Path(result['artifacts'][0]['path']).read_text(encoding='utf-8'))
        self.assertEqual(len(draft['tracks']),5)
        self.assertEqual(len(draft['tracks'][0]['segments']),2)
        self.assertEqual(result['nativeTextCount'],1)
        self.assertEqual(draft['tracks'][0]['segments'][0]['target_timerange']['duration'],1000000)
        voice=next(t for t in draft['tracks'] if t.get('name')=='人声')['segments'][0]
        self.assertEqual(voice['source_timerange']['start'],500000)
        self.assertEqual(voice['source_timerange']['duration'],1000000)
        self.assertEqual(voice['target_timerange']['duration'],500000)
        self.assertEqual(voice['speed'],2)
        self.assertTrue(any(t['type']=='text' for t in draft['tracks']))
        self.assertTrue(all(Path(m['path']).is_file() for key in ('videos','audios') for m in draft['materials'][key]))
        self.assertTrue(Path(result['artifacts'][1]['path']).is_file())
    def test_vendor_commands_do_not_start_service(self):
        if not VENDOR.exists(): self.skipTest('vendor absent')
        result=self.run_tool('vendor_workbench')
        self.assertEqual(result['status'],'completed',result)
        self.assertFalse(result['startedServices'])
        self.assertGreater(len(result['commands']),3)
    def test_upstream_selfcheck_executes_and_reports_unlisted_literal(self):
        script=VENDOR/'anything2explainer-main/template/scripts/selfcheck.py'
        if not script.exists(): self.skipTest('vendor selfcheck absent')
        fixture=self.root/'staticcheck'; group=fixture/'src/shots/G1'; group.mkdir(parents=True)
        (fixture/'script').mkdir()
        (fixture/'分镜表.md').write_text('| SC01 | 0–119 | 已知画面 |\n',encoding='utf-8')
        (fixture/'script/timeline.json').write_text('{"chapters":[{"from":0}]}',encoding='utf-8')
        (group/'index.ts').write_text("export const shots=[{id: 'SC01', from: 0, to: 119}];",encoding='utf-8')
        (group/'SC01.tsx').write_text("export const View=()=> <GlitchIn title='Unverified 2030'/>;",encoding='utf-8')
        result=subprocess.run([sys.executable,str(script),'--root',str(fixture)],capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONIOENCODING':'utf-8'},timeout=20)
        self.assertEqual(result.returncode,0)  # upstream reports problems without a failing exit code
        self.assertIn('problems: 1',result.stdout)
        self.assertIn('Unverified 2030',result.stdout)
    def test_output_escape_rejected(self):
        result=self.run_tool('research',{'text':'test'},{**self.context,'output':self.root.parent/'not-workbench'})
        self.assertEqual(result['status'],'failed')
        self.assertIn('within',result['warnings'][0])

if __name__=='__main__': unittest.main()
