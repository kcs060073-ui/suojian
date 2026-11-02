import unittest
import os
import subprocess
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from workbench.library import discover, resolve_file, run_demo
from workbench.paths import VENDOR

@unittest.skipUnless(VENDOR.exists(),'local read-only vendor snapshot required')
class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.data=discover(VENDOR)

    def test_full_card_style_catalog_has_source(self):
        self.assertEqual(self.data['stats']['cards'],124)
        self.assertEqual(self.data['stats']['styles'],150)
        self.assertTrue(all(c['sourceAvailable'] for c in self.data['recipes']))
        self.assertEqual(len({c['id'] for c in self.data['recipes']}),124)
        for card in self.data['recipes']:
            self.assertTrue(card['sourceText'])
            self.assertTrue(card['reference'].startswith('/vendor/'))
            self.assertTrue(card['demo'])

    def test_missing_media_is_not_claimed_local(self):
        actual=sum(s['available'] for c in self.data['recipes'] for s in c['styles'])
        self.assertEqual(actual,self.data['stats']['previews'])
        for c in self.data['recipes']:
            for s in c['styles']:
                if not s['available']:
                    self.assertIsNone(s['preview'])
                    self.assertEqual(s['previewStatus'],'local_missing_remote_unverified')

    def test_audio_with_original_attribution(self):
        self.assertEqual(self.data['stats']['bgm'],5)
        self.assertEqual(self.data['stats']['sfx'],149)
        self.assertTrue(any(a['licenseStatus']=='needs_review' for a in self.data['audio']))
        self.assertTrue(all(a['source'] for a in self.data['audio']))
        self.assertGreaterEqual(len(self.data['licenses']),3)

    def test_policy_and_truthful_dependency_status(self):
        self.assertFalse(self.data['policy']['bilingualSubtitles'])
        self.assertTrue(self.data['policy']['presenterFollowsSelectedVersion'])
        for c in self.data['recipes']:
            self.assertEqual(c['available'],not bool(c['missingDependencies']))

    def test_vendor_file_cannot_escape(self):
        for p in ('../runtime-dependencies.json','C:/Windows/win.ini','/etc/passwd','../../../../file'):
            with self.assertRaises(ValueError): resolve_file(VENDOR,p)
        self.assertEqual(resolve_file(VENDOR,'video-shotcraft-main/LICENSE'),VENDOR/'video-shotcraft-main/LICENSE')

    def test_unknown_demo_rejected(self):
        with self.assertRaises(ValueError): run_demo('../anything',VENDOR,Path.cwd())

    @unittest.skipUnless(os.environ.get('VIBE_CAPTURE_SMOKE')=='1','headless browser smoke is opt-in')
    def test_real_url_capture_and_sha(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=b'<html><head><title>capture smoke</title></head><body style="margin:0;background:#123456;color:white"><main style="width:400px;height:300px">Real URL capture</main></body></html>'
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        output=Path.cwd()/'work/capture-smoke.png'
        try:
            result=subprocess.run(['node','workbench/capture.cjs','--url',f'http://127.0.0.1:{server.server_port}/','--output',str(output),'--width','600','--height','400'],capture_output=True,text=True,encoding='utf-8',timeout=60)
            self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads(result.stdout)
            self.assertEqual(data['layout']['title'],'capture smoke')
            self.assertEqual(len(data['sha256']),64)
            self.assertTrue(output.read_bytes().startswith(b'\x89PNG'))
        finally: server.shutdown();server.server_close()

if __name__=='__main__': unittest.main()
