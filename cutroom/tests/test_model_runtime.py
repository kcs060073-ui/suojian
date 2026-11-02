import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.model_profiles import (
    bind_task_profiles,
    create_model_profile,
    create_provider_account,
    mark_profile_check,
)
from media_catalog.model_runtime import task_model_environment
from media_catalog.tasks import create_task


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase.open(Path(self.tmp.name) / "catalog.sqlite")
        self.db.migrate()
        create_task(self.db, "task-runtime", "runtime", "digital_human", brief={})
        create_provider_account(
            self.db,
            account_id="gateway-1",
            name="本机 New API",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/gateway-1",
            settings={"channel_id": 23},
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _profile(self, capability, model, **settings):
        profile_id = f"profile-{capability}"
        create_model_profile(
            self.db,
            profile_id=profile_id,
            capability=capability,
            display_name=profile_id,
            account_id="gateway-1",
            model=model,
            settings={"protocol": "openai", **settings},
        )
        mark_profile_check(
            self.db,
            profile_id,
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        return profile_id

    def test_general_text_uses_frozen_model_and_pinned_channel_token(self):
        profile_id = self._profile("general_text", "doubao-frozen")
        bind_task_profiles(
            self.db,
            "task-runtime",
            {"general_text": profile_id},
            required_capabilities=("general_text",),
        )

        environment, snapshot = task_model_environment(
            self.db,
            "task-runtime",
            "general_text",
            base_environment={"KEEP": "yes"},
            secret_reader=lambda reference: (
                "sk-gateway" if reference == "cutroom/provider/gateway-1" else None
            ),
        )

        self.assertEqual(environment["ARK_API_KEY"], "sk-gateway-23")
        self.assertEqual(environment["ARK_MODEL"], "doubao-frozen")
        self.assertEqual(environment["ARK_BASE_URL"], "http://127.0.0.1:3000/v1")
        self.assertEqual(environment["KEEP"], "yes")
        self.assertEqual(snapshot["profileId"], profile_id)

    def test_each_split_text_capability_uses_its_own_frozen_model(self):
        selections = {}
        for capability in ("copywriting", "storyboard", "packaging"):
            selections[capability] = self._profile(
                capability, f"model-{capability}"
            )
        bind_task_profiles(
            self.db,
            "task-runtime",
            selections,
            required_capabilities=("copywriting", "storyboard", "packaging"),
        )

        for capability in ("copywriting", "storyboard", "packaging"):
            environment, snapshot = task_model_environment(
                self.db,
                "task-runtime",
                capability,
                base_environment={},
                secret_reader=lambda _reference: "sk-gateway",
            )
            self.assertEqual(environment["ARK_MODEL"], f"model-{capability}")
            self.assertEqual(snapshot["capability"], capability)

    def test_storyboard_and_packaging_reuse_one_planning_binding(self):
        profile_id = self._profile("planning", "glm-5.3")
        bind_task_profiles(
            self.db,
            "task-runtime",
            {"planning": profile_id},
            required_capabilities=("planning",),
        )

        results = {}
        for usage in ("copywriting", "storyboard", "packaging"):
            environment, snapshot = task_model_environment(
                self.db,
                "task-runtime",
                usage,
                base_environment={},
                secret_reader=lambda _reference: "planning-key",
            )
            results[usage] = (environment, snapshot)

        for environment, snapshot in results.values():
            self.assertEqual(environment["ARK_MODEL"], "glm-5.3")
            self.assertEqual(snapshot["capability"], "planning")
            self.assertEqual(snapshot["profileId"], profile_id)

    def test_exact_legacy_text_binding_wins_over_planning_fallback(self):
        planning_id = self._profile("planning", "planning-model")
        storyboard_id = self._profile("storyboard", "legacy-storyboard-model")
        bind_task_profiles(
            self.db,
            "task-runtime",
            {"planning": planning_id, "storyboard": storyboard_id},
            required_capabilities=("planning", "storyboard"),
        )

        environment, snapshot = task_model_environment(
            self.db,
            "task-runtime",
            "storyboard",
            base_environment={},
            secret_reader=lambda _reference: "test-key",
        )

        self.assertEqual(environment["ARK_MODEL"], "legacy-storyboard-model")
        self.assertEqual(snapshot["capability"], "storyboard")

    def test_direct_provider_uses_exact_api_key_without_channel_suffix(self):
        create_provider_account(
            self.db,
            account_id="ark-direct",
            name="火山方舟直连",
            provider="volcengine_ark",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            credential_ref="cutroom/provider/ark-direct",
            settings={"preset": "volcengine_ark", "protocol": "openai"},
        )
        create_model_profile(
            self.db,
            profile_id="storyboard-direct",
            capability="storyboard",
            display_name="方舟分镜",
            account_id="ark-direct",
            model="ep-direct",
            settings={"protocol": "openai"},
        )
        mark_profile_check(
            self.db,
            "storyboard-direct",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        bind_task_profiles(
            self.db,
            "task-runtime",
            {"storyboard": "storyboard-direct"},
            required_capabilities=("storyboard",),
        )

        environment, _snapshot = task_model_environment(
            self.db,
            "task-runtime",
            "storyboard",
            base_environment={},
            secret_reader=lambda _reference: "ark-direct-key",
        )

        self.assertEqual(environment["ARK_API_KEY"], "ark-direct-key")
        self.assertEqual(
            environment["ARK_BASE_URL"],
            "https://ark.cn-beijing.volces.com/api/v3",
        )

    def test_version_four_api_root_is_preserved_for_runtime_calls(self):
        create_provider_account(
            self.db,
            account_id="zhipu-v4",
            name="智谱直连",
            provider="openai_compatible",
            base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
            credential_ref="cutroom/provider/zhipu-v4",
            settings={"preset": "openai_compatible", "protocol": "openai"},
        )
        create_model_profile(
            self.db,
            profile_id="zhipu-planning",
            capability="storyboard",
            display_name="智谱策划",
            account_id="zhipu-v4",
            model="glm-5.3",
            settings={"protocol": "openai"},
        )
        mark_profile_check(
            self.db,
            "zhipu-planning",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        bind_task_profiles(
            self.db,
            "task-runtime",
            {"storyboard": "zhipu-planning"},
            required_capabilities=("storyboard",),
        )

        environment, _snapshot = task_model_environment(
            self.db,
            "task-runtime",
            "storyboard",
            base_environment={},
            secret_reader=lambda _reference: "zhipu-key",
        )

        self.assertEqual(
            environment["ARK_BASE_URL"],
            "https://open.bigmodel.cn/api/paas/v4",
        )

    def test_new_api_without_optional_channel_uses_raw_token(self):
        create_provider_account(
            self.db,
            account_id="gateway-unpinned",
            name="New API 自动路由",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/gateway-unpinned",
            settings={"preset": "new_api", "protocol": "openai"},
        )
        create_model_profile(
            self.db,
            profile_id="storyboard-unpinned",
            capability="storyboard",
            display_name="网关自动路由分镜",
            account_id="gateway-unpinned",
            model="model-auto",
            settings={"protocol": "openai"},
        )
        mark_profile_check(
            self.db,
            "storyboard-unpinned",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        bind_task_profiles(
            self.db,
            "task-runtime",
            {"storyboard": "storyboard-unpinned"},
            required_capabilities=("storyboard",),
        )

        environment, _snapshot = task_model_environment(
            self.db,
            "task-runtime",
            "storyboard",
            base_environment={},
            secret_reader=lambda _reference: "sk-gateway",
        )

        self.assertEqual(environment["ARK_API_KEY"], "sk-gateway")

    def test_tts_uses_openai_speech_endpoint_and_frozen_voice(self):
        profile_id = self._profile("tts", "doubao-tts", voice="zh_female")
        bind_task_profiles(
            self.db,
            "task-runtime",
            {"tts": profile_id},
            required_capabilities=("tts",),
        )

        environment, _snapshot = task_model_environment(
            self.db,
            "task-runtime",
            "tts",
            base_environment={},
            secret_reader=lambda _reference: "sk-gateway",
        )

        self.assertEqual(environment["TTS_PROTOCOL"], "openai")
        self.assertEqual(
            environment["TTS_ENDPOINT"],
            "http://127.0.0.1:3000/v1/audio/speech",
        )
        self.assertEqual(environment["TTS_ACCESS_TOKEN"], "sk-gateway-23")
        self.assertEqual(environment["TTS_MODEL"], "doubao-tts")
        self.assertEqual(environment["TTS_VOICE_TYPE"], "zh_female")
        self.assertNotIn("TTS_APPID", environment)

    def test_direct_doubao_tts_uses_frozen_app_id_and_exact_access_token(self):
        from media_catalog.credentials import encode_provider_credentials

        create_provider_account(
            self.db,
            account_id="doubao-tts-direct",
            name="豆包 TTS 直连",
            provider="doubao_tts",
            base_url="https://openspeech.bytedance.com/api/v1/tts",
            credential_ref="cutroom/provider/doubao-tts-direct",
            settings={
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "app_id": "appid-frozen",
                "cluster": "volcano_tts",
            },
        )
        create_model_profile(
            self.db,
            profile_id="tts-direct",
            capability="tts",
            display_name="豆包配音",
            account_id="doubao-tts-direct",
            model="doubao-tts",
            settings={"protocol": "volcengine_tts", "voice": "voice-frozen"},
        )
        mark_profile_check(
            self.db,
            "tts-direct",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        bind_task_profiles(
            self.db,
            "task-runtime",
            {"tts": "tts-direct"},
            required_capabilities=("tts",),
        )

        environment, _snapshot = task_model_environment(
            self.db,
            "task-runtime",
            "tts",
            base_environment={},
            secret_reader=lambda _reference: encode_provider_credentials(
                "doubao-access-token",
                "cloud-sk-not-for-runtime",
            ),
        )

        self.assertEqual(environment["TTS_PROTOCOL"], "volcengine")
        self.assertEqual(
            environment["TTS_ENDPOINT"],
            "https://openspeech.bytedance.com/api/v1/tts",
        )
        self.assertEqual(environment["TTS_APPID"], "appid-frozen")
        self.assertEqual(environment["TTS_ACCESS_TOKEN"], "doubao-access-token")
        self.assertEqual(environment["TTS_CLUSTER"], "volcano_tts")
        self.assertEqual(environment["TTS_VOICE_TYPE"], "voice-frozen")

    def test_doubao_api_key_uses_v3_and_frozen_resource(self):
        from media_catalog.credentials import encode_provider_credentials

        create_provider_account(
            self.db,
            account_id="doubao-v3",
            name="豆包新版 TTS",
            provider="doubao_tts",
            base_url="https://openspeech.bytedance.com/api/v1/tts",
            credential_ref="cutroom/provider/doubao-v3",
            settings={"preset": "doubao_tts", "protocol": "volcengine_tts"},
        )
        create_model_profile(
            self.db,
            profile_id="tts-v3",
            capability="tts",
            display_name="豆包新版配音",
            account_id="doubao-v3",
            model="seed-tts-1.0",
            settings={
                "protocol": "volcengine_tts",
                "resource_id": "seed-tts-1.0",
                "voice": "voice-v3",
            },
        )
        mark_profile_check(
            self.db, "tts-v3", available=True,
            checked_at=datetime.now(timezone.utc),
        )
        bind_task_profiles(
            self.db, "task-runtime", {"tts": "tts-v3"},
            required_capabilities=("tts",),
        )

        environment, _snapshot = task_model_environment(
            self.db,
            "task-runtime",
            "tts",
            base_environment={},
            secret_reader=lambda _reference: encode_provider_credentials(
                None, "catalog-sk", "speech-api-key"
            ),
        )

        self.assertEqual(environment["TTS_PROTOCOL"], "volcengine_v3")
        self.assertEqual(
            environment["TTS_ENDPOINT"],
            "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
        )
        self.assertEqual(environment["TTS_API_KEY"], "speech-api-key")
        self.assertEqual(environment["TTS_RESOURCE_ID"], "seed-tts-1.0")
        self.assertEqual(environment["TTS_VOICE_TYPE"], "voice-v3")
        self.assertNotIn("TTS_ACCESS_TOKEN", environment)
        self.assertNotIn("TTS_APPID", environment)


if __name__ == "__main__":
    unittest.main()
