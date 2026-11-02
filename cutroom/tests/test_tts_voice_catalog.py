import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.model_profiles import create_model_profile, create_provider_account
from media_catalog.credentials import encode_provider_credentials
from media_catalog.model_profiles import update_provider_account_settings
from media_catalog.tts_voice_catalog import (
    import_legacy_tts_voices,
    list_provider_voices,
    mark_voice_catalog_failure,
    mark_voice_test_result,
    replace_provider_voices,
    refresh_provider_voice_catalog,
    voice_catalog_state,
)
from media_catalog.volcengine_voice_catalog import VoiceCatalogError


class TtsVoiceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase.open(Path(self.tmp.name) / "catalog.sqlite")
        self.db.migrate()
        create_provider_account(
            self.db,
            account_id="doubao",
            name="豆包配音",
            provider="doubao_tts",
            base_url="https://openspeech.bytedance.com/api/v1/tts",
            credential_ref="cutroom/provider/doubao",
            settings={
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "tts_resource_id": "seed-tts-2.0",
            },
        )
        self.now = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
        self.later = self.now + timedelta(minutes=5)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_successful_refresh_replaces_only_one_account_resource(self):
        replace_provider_voices(
            self.db,
            "doubao",
            "seed-tts-2.0",
            [{
                "voiceId": "voice-a",
                "displayName": "阳光青年 2.0",
                "resourceId": "seed-tts-2.0",
                "gender": "male",
                "languages": ["zh-CN"],
            }],
            observed_at=self.now,
        )
        replace_provider_voices(
            self.db,
            "doubao",
            "seed-tts-2.0",
            [{
                "voiceId": "voice-b",
                "displayName": "灿灿 2.0",
                "resourceId": "seed-tts-2.0",
                "gender": "female",
                "languages": ["zh-CN"],
            }],
            observed_at=self.later,
        )

        voices = list_provider_voices(
            self.db, "doubao", "seed-tts-2.0"
        )
        self.assertEqual(
            [(voice["voiceId"], voice["selectable"]) for voice in voices],
            [("voice-a", False), ("voice-b", True)],
        )
        self.assertEqual(
            voice_catalog_state(self.db, "doubao", "seed-tts-2.0")["status"],
            "success",
        )

    def test_failed_refresh_keeps_last_successful_catalog(self):
        replace_provider_voices(
            self.db,
            "doubao",
            "seed-tts-2.0",
            [{
                "voiceId": "voice-a",
                "displayName": "阳光青年 2.0",
                "resourceId": "seed-tts-2.0",
            }],
            observed_at=self.now,
        )
        mark_voice_catalog_failure(
            self.db,
            "doubao",
            "seed-tts-2.0",
            "VOICE_CATALOG_AUTH_FAILED",
            "无权限",
        )

        voices = list_provider_voices(
            self.db, "doubao", "seed-tts-2.0", selectable_only=True
        )
        self.assertEqual([voice["voiceId"] for voice in voices], ["voice-a"])
        state = voice_catalog_state(
            self.db, "doubao", "seed-tts-2.0"
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["lastErrorCode"], "VOICE_CATALOG_AUTH_FAILED")
        self.assertIsNotNone(state["lastSuccessAt"])

    def test_voice_specific_failure_hides_only_that_voice_until_refresh(self):
        voices = [
            {
                "voiceId": "voice-specific-failure",
                "displayName": "单音色失败",
                "resourceId": "seed-tts-2.0",
            },
            {
                "voiceId": "voice-auth-failure",
                "displayName": "鉴权故障时保留",
                "resourceId": "seed-tts-2.0",
            },
        ]
        replace_provider_voices(
            self.db,
            "doubao",
            "seed-tts-2.0",
            voices,
            observed_at=self.now,
        )

        mark_voice_test_result(
            self.db,
            "doubao",
            "seed-tts-2.0",
            "voice-specific-failure",
            available=False,
            error_code="TTS_VOICE_TEST_FAILED",
            error_message="TTS 没有返回有效音频",
        )
        mark_voice_test_result(
            self.db,
            "doubao",
            "seed-tts-2.0",
            "voice-auth-failure",
            available=False,
            error_code="MODEL_AUTH_FAILED",
            error_message="API Key 无效或没有访问权限",
        )

        before_refresh = {
            voice["voiceId"]: voice
            for voice in list_provider_voices(
                self.db, "doubao", "seed-tts-2.0"
            )
        }
        self.assertFalse(before_refresh["voice-specific-failure"]["selectable"])
        self.assertTrue(before_refresh["voice-auth-failure"]["selectable"])

        replace_provider_voices(
            self.db,
            "doubao",
            "seed-tts-2.0",
            voices,
            observed_at=self.later,
        )
        after_refresh = {
            voice["voiceId"]: voice
            for voice in list_provider_voices(
                self.db, "doubao", "seed-tts-2.0"
            )
        }
        self.assertTrue(after_refresh["voice-specific-failure"]["selectable"])

    def test_legacy_voice_import_is_idempotent_and_preserves_task_binding(self):
        create_model_profile(
            self.db,
            profile_id="legacy-tts",
            capability="tts",
            display_name="旧配音",
            account_id="doubao",
            model="seed-tts-2.0",
            settings={
                "protocol": "volcengine_tts",
                "resource_id": "seed-tts-2.0",
                "voice": "legacy-voice",
            },
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE model_profiles SET availability_status='available' "
                "WHERE profile_id='legacy-tts'"
            )
            connection.execute(
                "INSERT INTO tasks(task_id,title,task_type) "
                "VALUES('legacy-task','旧任务','ad_mix')"
            )
            connection.execute(
                "INSERT INTO task_model_bindings(" 
                "task_id,capability,profile_id,snapshot_json,snapshot_digest" 
                ") VALUES('legacy-task','tts','legacy-tts','{\"voice\":\"frozen\"}','digest')"
            )
        before = self.db.connection.execute(
            "SELECT snapshot_json,snapshot_digest FROM task_model_bindings "
            "WHERE task_id='legacy-task' AND capability='tts'"
        ).fetchone()

        first = import_legacy_tts_voices(self.db)
        second = import_legacy_tts_voices(self.db)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        voices = list_provider_voices(
            self.db, "doubao", "seed-tts-2.0"
        )
        self.assertEqual(len(voices), 1)
        self.assertEqual(voices[0]["voiceId"], "legacy-voice")
        self.assertEqual(voices[0]["displayName"], "原有音色")
        self.assertEqual(voices[0]["source"], "legacy_saved")
        self.assertTrue(voices[0]["selectable"])
        profile_settings = json.loads(
            self.db.connection.execute(
                "SELECT settings_json FROM model_profiles "
                "WHERE profile_id='legacy-tts'"
            ).fetchone()[0]
        )
        self.assertEqual(profile_settings["voice"], "legacy-voice")
        self.assertEqual(profile_settings["default_voice_id"], "legacy-voice")
        after = self.db.connection.execute(
            "SELECT snapshot_json,snapshot_digest FROM task_model_bindings "
            "WHERE task_id='legacy-task' AND capability='tts'"
        ).fetchone()
        self.assertEqual(tuple(after), tuple(before))

    def test_refresh_reads_ak_sk_and_resource_then_persists_normalized_voices(self):
        account = update_provider_account_settings(
            self.db,
            "doubao",
            {
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "access_key_id": "AKLT-local",
                "tts_resource_id": "seed-tts-2.0",
            },
        )
        captured = []

        def fetcher(access_key_id, secret_access_key, resource_id):
            captured.append((access_key_id, secret_access_key, resource_id))
            return [{
                "voiceId": "voice-a",
                "displayName": "阳光青年 2.0",
                "resourceId": resource_id,
            }]

        result = refresh_provider_voice_catalog(
            self.db,
            account["accountId"],
            secret_reader=lambda _reference: encode_provider_credentials(
                "tts-token", "cloud-sk"
            ),
            fetcher=fetcher,
        )

        self.assertEqual(
            captured,
            [("AKLT-local", "cloud-sk", "seed-tts-2.0")],
        )
        self.assertEqual(result["state"]["status"], "success")
        self.assertEqual(result["voices"][0]["voiceId"], "voice-a")

    def test_refresh_rejects_missing_catalog_credentials_without_leaking_token(self):
        with self.assertRaises(VoiceCatalogError) as raised:
            refresh_provider_voice_catalog(
                self.db,
                "doubao",
                secret_reader=lambda _reference: encode_provider_credentials(
                    "tts-token-never-return", None
                ),
                fetcher=lambda *_: self.fail("fetcher must not run"),
            )

        self.assertEqual(
            raised.exception.code,
            "VOICE_CATALOG_CREDENTIALS_MISSING",
        )
        self.assertNotIn("tts-token-never-return", str(raised.exception))

    def test_refresh_failure_keeps_cache_and_records_only_safe_error(self):
        update_provider_account_settings(
            self.db,
            "doubao",
            {
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "access_key_id": "AKLT-private",
                "tts_resource_id": "seed-tts-2.0",
            },
        )
        replace_provider_voices(
            self.db,
            "doubao",
            "seed-tts-2.0",
            [{
                "voiceId": "cached",
                "displayName": "缓存音色",
                "resourceId": "seed-tts-2.0",
            }],
        )

        def fail(*_args):
            raise VoiceCatalogError(
                "VOICE_CATALOG_AUTH_FAILED",
                "无权读取音色目录，请检查 AK/SK 与 IAM 权限。",
            )

        with self.assertRaises(VoiceCatalogError):
            refresh_provider_voice_catalog(
                self.db,
                "doubao",
                secret_reader=lambda _reference: encode_provider_credentials(
                    "tts-token-private", "cloud-sk-private"
                ),
                fetcher=fail,
            )

        self.assertEqual(
            [
                voice["voiceId"]
                for voice in list_provider_voices(
                    self.db,
                    "doubao",
                    "seed-tts-2.0",
                    selectable_only=True,
                )
            ],
            ["cached"],
        )
        state = voice_catalog_state(
            self.db, "doubao", "seed-tts-2.0"
        )
        serialized = json.dumps(state, ensure_ascii=False)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["lastErrorCode"], "VOICE_CATALOG_AUTH_FAILED")
        self.assertNotIn("AKLT-private", serialized)
        self.assertNotIn("tts-token-private", serialized)
        self.assertNotIn("cloud-sk-private", serialized)

    def test_refresh_rejects_resource_mismatch_transactionally(self):
        update_provider_account_settings(
            self.db,
            "doubao",
            {
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "access_key_id": "AKLT-local",
                "tts_resource_id": "seed-tts-2.0",
            },
        )

        with self.assertRaises(VoiceCatalogError) as raised:
            refresh_provider_voice_catalog(
                self.db,
                "doubao",
                secret_reader=lambda _reference: encode_provider_credentials(
                    "tts-token", "cloud-sk"
                ),
                fetcher=lambda *_: [{
                    "voiceId": "wrong-resource",
                    "displayName": "错误版本",
                    "resourceId": "seed-tts-1.0",
                }],
            )

        self.assertEqual(
            raised.exception.code,
            "VOICE_CATALOG_INVALID_RESPONSE",
        )
        self.assertEqual(
            list_provider_voices(self.db, "doubao", "seed-tts-2.0"),
            [],
        )
        self.assertEqual(
            voice_catalog_state(
                self.db, "doubao", "seed-tts-2.0"
            )["status"],
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
