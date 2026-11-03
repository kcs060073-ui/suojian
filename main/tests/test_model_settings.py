import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from flask import Flask
from suo_jian.ui.model_settings import register_model_settings


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'SUOJIAN_VIBE_ROOT': self.tmp.name})
        self.env.start(); self.addCleanup(self.env.stop)
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'suo_jian/ui/templates'))
        register_model_settings(app)
        self.client = app.test_client()
        self.data = {'accounts': [{'accountId': 'a', 'name': 'A', 'provider': 'openai', 'credentialRef': 'do-not-expose'}],
                     'profiles': [{'profileId': 'p', 'displayName': 'P', 'capability': 'planning', 'enabled': True, 'model': 'test'}]}
        self.adapter = patch('suo_jian.ui.model_settings.call_local_adapter', return_value=self.data)
        self.adapter.start(); self.addCleanup(self.adapter.stop)

    def test_list_is_redacted(self):
        response = self.client.get('/api/model-settings')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('credentialRef', str(response.json))

    def test_binding_keeps_only_reference(self):
        response = self.client.post('/api/model-settings/vibecraft', json={'profileId': 'p'})
        self.assertEqual(response.status_code, 200)
        path = Path(self.tmp.name) / 'work/director/shared-profile.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'profileId': 'p'})
        self.assertEqual(self.client.post('/api/model-settings/vibecraft', json={'profileId': 'bad'}).status_code, 400)

    def test_rejects_cross_origin_and_insecure_endpoint(self):
        self.assertEqual(self.client.post('/api/model-settings/accounts', json={}, headers={'Origin':'https://example.com'}).status_code, 403)
        self.assertEqual(self.client.post('/api/model-settings/accounts', json={'baseUrl':'http://example.com'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
