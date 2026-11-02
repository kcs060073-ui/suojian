"""Offline regressions for review F17/F18; transport alone is substituted."""
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from media_catalog.credentials import encode_provider_credentials
from media_catalog.database import CatalogDatabase
from media_catalog.model_profile_probe import ModelProfileProbeError, create_tts_voice_preview, probe_model_profile
from media_catalog.model_profiles import (create_model_profile, create_provider_account,
    get_model_profile, mark_profile_check, resolve_task_profile_snapshots, resolve_task_tts_voice)
from media_catalog.tts_voice_catalog import get_provider_voice, replace_provider_voices


class ModelRegressionTests(unittest.TestCase):
    def setUp(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        self.db = CatalogDatabase(Path(':memory:'), connection)
        self.db.migrate()
        self.addCleanup(self.db.close)
        create_provider_account(self.db, account_id='tts', name='TTS', provider='doubao_tts',
            base_url='https://example.invalid/api/v1/tts', credential_ref='fake-tts',
            settings={'app_id': 'fake', 'tts_resource_id': 'seed-tts-2.0'})
        create_model_profile(self.db, profile_id='tts', capability='tts', display_name='TTS',
            account_id='tts', model='seed-tts-2.0', settings={'protocol': 'volcengine_tts',
                'voice': 'voice', 'resource_id': 'seed-tts-2.0'})
        replace_provider_voices(self.db, 'tts', 'seed-tts-2.0',
            [{'voiceId': 'voice', 'displayName': 'Voice', 'resourceId': 'seed-tts-2.0'}])
        mark_profile_check(self.db, 'tts', available=True)

    def _preview_failure(self, patcher, expected_code, *, v3=False):
        with patcher, self.assertRaises(ModelProfileProbeError) as caught:
            create_tts_voice_preview(self.db, 'tts', 'voice', Path('unused-preview'),
                secret_reader=lambda _: encode_provider_credentials('fake', None, 'fake-v3') if v3 else 'fake')
        voice = get_provider_voice(self.db, 'tts', 'seed-tts-2.0', 'voice')
        self.assertEqual(voice['lastTestErrorCode'], expected_code)
        self.assertEqual(caught.exception.code, expected_code)
        snapshot = resolve_task_profile_snapshots(self.db, {'tts': 'tts'}, required_capabilities=('tts',))['tts']
        if expected_code == 'TTS_VOICE_TEST_FAILED':
            self.assertFalse(voice['selectable'])
            with self.assertRaisesRegex(ValueError, 'VOICE_NOT_SELECTABLE'):
                resolve_task_tts_voice(self.db, snapshot, 'voice')
        else:
            self.assertTrue(voice['selectable'])
            self.assertEqual(resolve_task_tts_voice(self.db, snapshot, 'voice')['voiceId'], 'voice')

    def test_voice_specific_legacy_rejection_disables_task_selection(self):
        self._preview_failure(patch('media_catalog.model_profile_probe._request_json',
            return_value=('application/json', b'{"code":3001,"message":"voice not supported"}')),
            'TTS_VOICE_TEST_FAILED')

    def test_voice_specific_v3_rejection_disables_task_selection(self):
        response = io.BytesIO(b'data: {"code":45000000,"message":"speaker not found"}\n\n')
        self._preview_failure(patch('media_catalog.model_profile_probe.urlopen', return_value=response),
            'TTS_VOICE_TEST_FAILED', v3=True)

    def test_auth_network_and_service_failures_do_not_disable_voice(self):
        for error, code in [(HTTPError('https://example.invalid', 403, 'Forbidden', {}, io.BytesIO(b'')),
                             'MODEL_AUTH_FAILED'), (URLError('offline'), 'MODEL_NETWORK_UNREACHABLE')]:
            with self.subTest(code=code):
                self._preview_failure(patch('media_catalog.model_profile_probe._request_json', side_effect=error), code)
        for message, code in [('resource not granted for speaker', 'MODEL_RESOURCE_NOT_GRANTED'),
                              ('unauthorized API key', 'MODEL_AUTH_FAILED'),
                              ('service unavailable', 'MODEL_PROVIDER_REJECTED')]:
            with self.subTest(message=message):
                self._preview_failure(patch('media_catalog.model_profile_probe._request_json',
                    return_value=('application/json', json.dumps({'code': 3001, 'message': message}).encode())), code)

    def test_malformed_recheck_revokes_previous_availability(self):
        create_provider_account(self.db, account_id='text', name='Text', provider='openai_compatible',
            base_url='https://example.invalid/v1', credential_ref='fake-text')
        for capability, payloads in [('planning', [[], None, 42, {'choices': None},
                {'choices': [None]}, {'choices': [{'message': []}]}, {'choices': [{'message': {'content': None}}]}]),
                ('broll', [[], None, {'data': None}, {'data': [None]}, {'data': [{'id': []}]}]),
                ('tts', [[], None, {'code': [], 'data': 'x'}])]:
            profile_id = capability if capability == 'tts' else 'bad-' + capability
            if capability != 'tts':
                create_model_profile(self.db, profile_id=profile_id, capability=capability,
                    display_name=capability, account_id='text', model='fake')
            for payload in payloads:
                with self.subTest(capability=capability, payload=payload):
                    mark_profile_check(self.db, profile_id, available=True)
                    with patch('media_catalog.model_profile_probe._request_json',
                            return_value=('application/json', json.dumps(payload).encode())):
                        with self.assertRaises(ModelProfileProbeError) as caught:
                            probe_model_profile(self.db, profile_id, secret_reader=lambda _: 'fake')
                    self.assertEqual(caught.exception.code, 'MODEL_PROTOCOL_MISMATCH')
                    result = get_model_profile(self.db, profile_id)
                    self.assertEqual(result['availabilityStatus'], 'unavailable')
                    self.assertEqual(result['lastErrorCode'], 'MODEL_PROTOCOL_MISMATCH')
                    with self.assertRaises(ValueError):
                        resolve_task_profile_snapshots(self.db, {capability: profile_id}, required_capabilities=(capability,))
