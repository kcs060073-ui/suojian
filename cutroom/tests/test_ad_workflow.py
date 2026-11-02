import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from media_catalog.ad_workflow import create_ad_task, preflight_ad_task
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.model_profiles import (
    create_model_profile,
    create_provider_account,
    get_task_profile_binding,
    mark_profile_check,
)
from media_catalog.tts_voice_catalog import replace_provider_voices
from media_catalog.task_sources import get_task_sources
from media_catalog.tasks import get_task
from workbench.api import WorkbenchAPI


class AdWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.primary = self.root / "event-footage"
        self.generic = self.root / "generic-city"
        self.primary.mkdir()
        self.generic.mkdir()
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        register_library(
            self.db,
            "event-footage",
            "活动实拍",
            "local",
            str(self.primary),
            purpose="task_specific",
        )
        register_library(
            self.db,
            "generic-city",
            "城市通用镜头",
            "local",
            str(self.generic),
            purpose="generic",
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def request(self, **changes):
        data = {
            "route": "ad_mix",
            "title": "招商推介会信息流",
            "final_script": "三十秒讲清楚活动价值。",
            "primary_library_id": "event-footage",
            "selected_generic_ids": ["generic-city"],
            "include_default_generic": False,
            "purpose": "抖音信息流获客",
            "target_duration_seconds": 30,
            "aspect_ratio": "9:16",
            "voice_mode": "ai_tts",
        }
        data.update(changes)
        return data

    def configure_models(self, *, tts_available=True):
        create_provider_account(
            self.db,
            account_id="gateway",
            name="New API",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/gateway",
            settings={"channel_id": 7},
        )
        for profile_id, capability, model, settings, available in (
            ("text-profile", "general_text", "deepseek-v4", {"protocol": "openai_chat"}, True),
            ("tts-profile", "tts", "tts-1", {"protocol": "openai_speech", "voice": "voice-a"}, tts_available),
            ("video-profile", "video_generation", "seedance-frozen", {"protocol": "doubao_video"}, True),
        ):
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=profile_id,
                account_id="gateway",
                model=model,
                settings=settings,
                is_default=True,
            )
            mark_profile_check(
                self.db,
                profile_id,
                available=available,
                checked_at=datetime.now(timezone.utc),
            )

    def configure_doubao_voice_models(self):
        create_provider_account(
            self.db,
            account_id="doubao-task-voice",
            name="豆包任务音色",
            provider="doubao_tts",
            base_url="https://openspeech.bytedance.com/api/v1/tts",
            credential_ref="cutroom/provider/doubao-task-voice",
            settings={
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "app_id": "app-id",
                "tts_resource_id": "seed-tts-2.0",
            },
        )
        replace_provider_voices(
            self.db,
            "doubao-task-voice",
            "seed-tts-2.0",
            [
                {
                    "voiceId": "voice-common",
                    "displayName": "常用音色",
                    "resourceId": "seed-tts-2.0",
                },
                {
                    "voiceId": "voice-task",
                    "displayName": "本条视频音色",
                    "resourceId": "seed-tts-2.0",
                },
            ],
        )
        create_model_profile(
            self.db,
            profile_id="doubao-task-profile",
            capability="tts",
            display_name="豆包任务配音",
            account_id="doubao-task-voice",
            model="seed-tts-2.0",
            settings={
                "protocol": "volcengine_tts",
                "resource_id": "seed-tts-2.0",
                "default_voice_id": "voice-common",
                "voice": "voice-common",
            },
        )
        mark_profile_check(
            self.db,
            "doubao-task-profile",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        create_provider_account(
            self.db,
            account_id="text-for-doubao-voice",
            name="豆包任务文案",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            credential_ref="cutroom/provider/text-for-doubao-voice",
            settings={"preset": "openai_compatible", "protocol": "openai"},
        )
        create_model_profile(
            self.db,
            profile_id="text-for-doubao-voice",
            capability="general_text",
            display_name="豆包任务文案",
            account_id="text-for-doubao-voice",
            model="glm-5.3",
            settings={"protocol": "openai"},
        )
        mark_profile_check(
            self.db,
            "text-for-doubao-voice",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        return {
            "general_text": "text-for-doubao-voice",
            "tts": "doubao-task-profile",
        }

    def test_preflight_accepts_ad_inputs_without_digital_human_mp4(self):
        result = preflight_ad_task(self.db, self.request(), self.root)
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["next_action"], "create_task")
        self.assertEqual(result["route"], "ad_mix")
        self.assertEqual(
            [item["key"] for item in result["confirmations"]],
            ["brief", "picture", "packaging", "full_review"],
        )
        self.assertNotIn("digital_human_mp4", str(result))

    def test_preflight_rejects_offline_primary_and_invalid_ad_settings(self):
        self.primary.rmdir()
        result = preflight_ad_task(
            self.db,
            self.request(aspect_ratio="1:1", voice_mode="upload"),
            self.root,
        )
        codes = {item["code"] for item in result["blocking"]}
        self.assertEqual(
            codes,
            {"AD_ASPECT_RATIO_INVALID", "AD_VOICE_MODE_INVALID", "PRIMARY_LIBRARY_OFFLINE"},
        )
        self.assertEqual(result["next_action"], "fix_blocking_items")

    def test_create_persists_script_brief_sources_and_first_stage(self):
        created = create_ad_task(self.db, self.request(), self.root)
        task_id = created["task_id"]
        task = get_task(self.db, task_id)
        self.assertEqual(task["task_type"], "ad_mix")
        self.assertEqual(task["brief"]["route"], "ad_mix")
        self.assertEqual(task["brief"]["aspect_ratio"], "9:16")
        self.assertEqual(task["brief"]["voice_mode"], "ai_tts")
        self.assertEqual(task["brief"]["purpose"], "抖音信息流获客")
        script_path = Path(created["input_dir"]) / "final-script.txt"
        self.assertEqual(script_path.read_text(encoding="utf-8"), "三十秒讲清楚活动价值。")
        sources = get_task_sources(self.db, task_id)
        self.assertEqual(sources["primary"]["library_id"], "event-footage")
        self.assertEqual(
            [item["library_id"] for item in sources["selected_generic"]],
            ["generic-city"],
        )
        self.assertEqual(created["workflow"]["current_stage"], "brief_lock")

    def test_create_freezes_selected_text_and_tts_profiles(self):
        self.configure_models()
        created = create_ad_task(
            self.db,
            self.request(
                model_profile_ids={
                    "general_text": "text-profile",
                    "tts": "tts-profile",
                    "video_generation": "video-profile",
                }
            ),
            self.root,
        )
        text = get_task_profile_binding(
            self.db, created["task_id"], "general_text"
        )
        tts = get_task_profile_binding(self.db, created["task_id"], "tts")
        self.assertEqual(text["model"], "deepseek-v4")
        self.assertEqual(tts["profileSettings"]["voice"], "voice-a")
        self.assertEqual(text["channelId"], 7)
        video = get_task_profile_binding(
            self.db, created["task_id"], "video_generation"
        )
        self.assertEqual(video["model"], "seedance-frozen")

    def test_create_freezes_planning_tts_and_optional_broll_profiles(self):
        create_provider_account(
            self.db,
            account_id="three-role",
            name="广告三类模型",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            credential_ref="cutroom/provider/three-role",
            settings={"preset": "openai_compatible", "protocol": "openai"},
        )
        definitions = (
            ("planning-profile", "planning", "glm-5.3", {"protocol": "openai"}),
            ("tts-role-profile", "tts", "tts-test", {"protocol": "openai_speech", "voice": "voice-test"}),
            ("broll-profile", "broll", "seedance-test", {"protocol": "doubao_video"}),
        )
        for profile_id, capability, model, settings in definitions:
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=profile_id,
                account_id="three-role",
                model=model,
                settings=settings,
            )
            mark_profile_check(
                self.db,
                profile_id,
                available=True,
                checked_at=datetime.now(timezone.utc),
            )

        created = create_ad_task(
            self.db,
            self.request(
                model_profile_ids={
                    "planning": "planning-profile",
                    "tts": "tts-role-profile",
                    "broll": "broll-profile",
                }
            ),
            self.root,
        )

        self.assertEqual(
            get_task_profile_binding(self.db, created["task_id"], "planning")["model"],
            "glm-5.3",
        )
        self.assertEqual(
            get_task_profile_binding(self.db, created["task_id"], "tts")["model"],
            "tts-test",
        )
        self.assertEqual(
            get_task_profile_binding(self.db, created["task_id"], "broll")["model"],
            "seedance-test",
        )

    def test_create_freezes_independent_text_stage_profiles_and_tts(self):
        create_provider_account(
            self.db,
            account_id="split-gateway",
            name="拆分 New API",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/split-gateway",
            settings={"channel_id": 18},
        )
        selections = {}
        definitions = (
            ("copywriting", "ad-copy-model", {"protocol": "openai_chat"}),
            ("storyboard", "ad-storyboard-model", {"protocol": "openai_chat"}),
            ("packaging", "ad-packaging-model", {"protocol": "openai_chat"}),
            ("tts", "ad-tts-model", {"protocol": "openai_speech", "voice": "voice-b"}),
        )
        for capability, model, settings in definitions:
            profile_id = f"ad-{capability}"
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=profile_id,
                account_id="split-gateway",
                model=model,
                settings=settings,
            )
            mark_profile_check(
                self.db,
                profile_id,
                available=True,
                checked_at=datetime.now(timezone.utc),
            )
            selections[capability] = profile_id

        created = create_ad_task(
            self.db,
            self.request(model_profile_ids=selections),
            self.root,
        )

        for capability, model, _settings in definitions:
            binding = get_task_profile_binding(
                self.db, created["task_id"], capability
            )
            self.assertEqual(binding["model"], model)

    def test_ad_creation_only_requires_storyboard_and_tts_models(self):
        create_provider_account(
            self.db,
            account_id="required-gateway",
            name="广告必需模型 New API",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/required-gateway",
            settings={"channel_id": 20},
        )
        selections = {}
        for capability, settings in (
            ("storyboard", {"protocol": "openai_chat"}),
            ("tts", {"protocol": "openai_speech", "voice": "voice-c"}),
        ):
            profile_id = f"required-{capability}"
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=profile_id,
                account_id="required-gateway",
                model=f"required-model-{capability}",
                settings=settings,
            )
            mark_profile_check(
                self.db,
                profile_id,
                available=True,
                checked_at=datetime.now(timezone.utc),
            )
            selections[capability] = profile_id

        created = create_ad_task(
            self.db,
            self.request(model_profile_ids=selections),
            self.root,
        )
        self.assertIsNotNone(
            get_task_profile_binding(self.db, created["task_id"], "storyboard")
        )
        self.assertIsNone(
            get_task_profile_binding(self.db, created["task_id"], "packaging")
        )

    def test_preflight_blocks_selected_unavailable_profile(self):
        self.configure_models(tts_available=False)
        result = preflight_ad_task(
            self.db,
            self.request(
                model_profile_ids={
                    "general_text": "text-profile",
                    "tts": "tts-profile",
                }
            ),
            self.root,
        )
        self.assertIn(
            "MODEL_PROFILE_UNAVAILABLE",
            {item["code"] for item in result["blocking"]},
        )

    def test_agent_mode_ignores_text_profile_but_freezes_tts_and_video_profiles(self):
        self.configure_models()
        mark_profile_check(
            self.db,
            "text-profile",
            available=False,
            checked_at=datetime.now(timezone.utc),
        )
        request = self.request(
            orchestration_mode="agent",
            model_profile_ids={
                "general_text": "text-profile",
                "tts": "tts-profile",
                "video_generation": "video-profile",
            },
        )
        preflight = preflight_ad_task(self.db, request, self.root)
        self.assertNotIn(
            "MODEL_PROFILE_UNAVAILABLE",
            {item["code"] for item in preflight["blocking"]},
        )

        created = create_ad_task(self.db, request, self.root)
        self.assertEqual(created["task"]["orchestration_mode"], "agent")
        self.assertIsNone(
            get_task_profile_binding(self.db, created["task_id"], "general_text")
        )
        self.assertEqual(
            get_task_profile_binding(self.db, created["task_id"], "tts")["model"],
            "tts-1",
        )
        self.assertEqual(
            get_task_profile_binding(
                self.db, created["task_id"], "video_generation"
            )["model"],
            "seedance-frozen",
        )

    def test_doubao_task_requires_catalog_voice_and_freezes_exact_selection(self):
        selections = self.configure_doubao_voice_models()
        missing = preflight_ad_task(
            self.db,
            self.request(model_profile_ids=selections),
            self.root,
        )
        self.assertIn(
            "TTS_VOICE_REQUIRED",
            {item["code"] for item in missing["blocking"]},
        )
        forged = preflight_ad_task(
            self.db,
            self.request(
                model_profile_ids=selections,
                tts_voice_id="not-in-catalog",
            ),
            self.root,
        )
        self.assertIn(
            "VOICE_NOT_SELECTABLE",
            {item["code"] for item in forged["blocking"]},
        )

        created = create_ad_task(
            self.db,
            self.request(
                model_profile_ids=selections,
                tts_voice_id="voice-task",
            ),
            self.root,
        )
        task_id = created["task_id"]
        binding = get_task_profile_binding(self.db, task_id, "tts")
        self.assertEqual(binding["profileSettings"]["voice"], "voice-task")
        self.assertEqual(
            binding["profileSettings"]["voice_name"],
            "本条视频音色",
        )
        self.assertEqual(
            get_task(self.db, task_id)["brief"]["tts_voice_id"],
            "voice-task",
        )
        settings = json.loads(
            (
                Path(created["input_dir"]) / "ad-settings.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(settings["tts_voice_id"], "voice-task")
        profile = self.db.connection.execute(
            "SELECT settings_json FROM model_profiles "
            "WHERE profile_id='doubao-task-profile'"
        ).fetchone()
        self.assertEqual(json.loads(profile[0])["voice"], "voice-common")

        replace_provider_voices(
            self.db,
            "doubao-task-voice",
            "seed-tts-2.0",
            [{
                "voiceId": "voice-common",
                "displayName": "常用音色（刷新后）",
                "resourceId": "seed-tts-2.0",
            }],
        )
        frozen = get_task_profile_binding(self.db, task_id, "tts")
        self.assertEqual(frozen["profileSettings"]["voice"], "voice-task")
        self.assertEqual(frozen["configDigest"], binding["configDigest"])

    def test_api_dispatches_create_and_preflight_by_route(self):
        api = WorkbenchAPI(self.db, self.root)
        preflight = api.dispatch("POST", "/api/tasks/preflight", self.request())
        self.assertEqual(preflight.status, 200)
        self.assertEqual(preflight.payload["route"], "ad_mix")
        created = api.dispatch("POST", "/api/tasks", self.request())
        self.assertEqual(created.status, 200)
        self.assertEqual(created.payload["task"]["task_type"], "ad_mix")
        bootstrap = api.dispatch("GET", "/api/bootstrap").payload
        ad_route = next(item for item in bootstrap["routes"] if item["key"] == "ad_mix")
        self.assertEqual(ad_route["availability"], "available")

    def test_locking_ad_brief_does_not_queue_digital_transcription(self):
        api = WorkbenchAPI(self.db, self.root)
        created = api.dispatch("POST", "/api/tasks", self.request()).payload
        task_id = created["task_id"]
        response = api.dispatch("POST", f"/api/tasks/{task_id}/brief/lock", {})
        self.assertEqual(response.status, 200)
