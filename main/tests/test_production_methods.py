import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from suo_jian.ui.production_methods import ACTIVE, register_production_methods, summarize


class ProductionMethodsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'uploads').mkdir()
        (self.root / 'uploads/1234abcd_reference.mp4').write_bytes(b'test')
        self.app = Flask(__name__)
        with patch.dict('os.environ', {'SUOJIAN_REA_CLI': sys.executable}):
            register_production_methods(self.app, self.root)
        self.client = self.app.test_client()
        self.addCleanup(ACTIVE.discard, '1234abcd')

    def test_uncertain_source_stays_unconfirmed_and_ae_is_a_recommendation(self):
        data = {'shots': [{'id':'shot_001','start':0,'end':2,
                          'base':{'label':'ai_video','label_zh':'AI 生成视频',
                                  'confidence':0.6,'uncertain':'实拍还是 AI 待确认','evidence':['低噪声']},
                          'layers':[{'kind':'subtitle'}]}]}
        row = summarize(data)['shots'][0]
        self.assertTrue(row['needs_review'])
        self.assertIn('待复核', row['recommendations'][0])
        self.assertIn('AE', row['recommendations'][1])
        self.assertEqual(row['evidence'], ['低噪声'])

    def test_job_routes_reject_cross_site_and_missing_media(self):
        path = '/api/analyze/1234abcd/methods'
        self.assertEqual(self.client.get(path).json['status'], 'idle')
        self.assertEqual(self.client.post(path, headers={'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/analyze/deadbeef/methods').status_code,400)

    def test_sidecar_accepts_only_the_named_workbench_origin(self):
        app = Flask('sidecar-test')
        register_production_methods(app, self.root, ('http://127.0.0.1:8788',))
        client = app.test_client()
        response = client.get('/api/analyze/1234abcd/methods', base_url='http://127.0.0.1:8790',
                              headers={'Origin':'http://127.0.0.1:8788'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'],'http://127.0.0.1:8788')
        response = client.post('/api/analyze/1234abcd/methods', base_url='http://127.0.0.1:8790',
                               headers={'Origin':'https://other.example'})
        self.assertEqual(response.status_code,403)

    def test_repeated_start_does_not_launch_duplicate_workers(self):
        path = '/api/analyze/1234abcd/methods'
        with patch('suo_jian.ui.production_methods.threading.Thread') as worker:
            self.assertEqual(self.client.post(path).status_code,202)
            self.assertEqual(self.client.post(path).json['status'],'running')
            self.assertEqual(worker.call_count,1)

    def test_project_local_cli_and_model_cache_work_without_original_workspace(self):
        cli = self.root / '.rea-venv' / ('Scripts/rea-video.exe' if os.name == 'nt' else 'bin/rea-video')
        cli.parent.mkdir(parents=True)
        cli.touch()
        folder = self.root / 'work/method-analysis/1234abcd'

        def complete(*args, **kwargs):
            (folder / 'breakdown.json').write_text(json.dumps({
                'summary': {'shots': 1}, 'shots': [{'id': 'shot_001', 'start': 0, 'end': 1}]}))

        app = Flask('portable-methods')
        with patch.dict(os.environ, {}, clear=True):
            register_production_methods(app, self.root)
            with patch('suo_jian.ui.production_methods.threading.Thread',
                       side_effect=lambda target, daemon: SimpleNamespace(start=target)), \
                 patch('suo_jian.ui.production_methods.subprocess.run', side_effect=complete) as run:
                self.assertEqual(app.test_client().post('/api/analyze/1234abcd/methods').status_code, 202)
                self.assertEqual(run.call_args.args[0][0], str(cli))
                self.assertIn('--no-vlm', run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs['env']['REA_VIDEO_MODELS'],
                                 str(self.root / 'model_cache/rea-video'))

    def test_saved_analysis_is_available_without_launching_worker(self):
        folder = self.root / 'work/method-analysis/1234abcd'
        folder.mkdir(parents=True)
        (folder / 'breakdown.json').write_text(json.dumps({'summary':{'shots':1},'shots':[
            {'id':'shot_001','start':0,'end':2,'base':{'label':'live_action','label_zh':'实拍'},'vlm':None}]}))
        with patch('suo_jian.ui.production_methods.threading.Thread') as worker:
            result = self.client.post('/api/analyze/1234abcd/methods').json
        self.assertEqual(result['shots'][0]['label'],'实拍')
        worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
