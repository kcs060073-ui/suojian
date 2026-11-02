import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.credentials import CAPABILITY_SPECS
from media_catalog.model_profiles import (
    ModelProfileInUse,
    ModelProfileUnavailable,
    bind_task_profiles,
    create_model_profile,
    create_provider_account,
    delete_model_profile,
    get_task_profile_binding,
    list_provider_accounts,
    list_task_profile_bindings,
    list_model_profiles,
    mark_profile_check,
    override_snapshot_profile_settings,
    resolve_task_profile_snapshots,
    save_task_profile_bindings,
    set_default_profile,
)


class ModelProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase.open(Path(self.tmp.name) / "catalog.sqlite")
        self.db.migrate()
        self.db.connection.execute(
            "INSERT INTO tasks(task_id,title,task_type) VALUES('task-1','广告','ad_mix')"
        )
        self.db.connection.commit()
        create_provider_account(
            self.db,
            account_id="account-ark",
            name="本机 New API",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/account-ark",
            settings={"channel_id": 7},
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _create_profile(self, profile_id, model, *, default=False):
        return create_model_profile(
            self.db,
            profile_id=profile_id,
            capability="general_text",
            display_name=model,
            account_id="account-ark",
            model=model,
            settings={"protocol": "openai_chat"},
            is_default=default,
        )

    def test_one_capability_can_have_multiple_profiles_and_only_one_default(self):
        self._create_profile("profile-a", "doubao-seed", default=True)
        self._create_profile("profile-b", "deepseek-v4")

        set_default_profile(self.db, "profile-b")
        profiles = list_model_profiles(self.db, capability="general_text")

        self.assertEqual([item["profileId"] for item in profiles], ["profile-a", "profile-b"])
        self.assertEqual(
            [item["profileId"] for item in profiles if item["isDefault"]],
            ["profile-b"],
        )
        serialized = json.dumps(profiles, ensure_ascii=False)
        self.assertNotIn("credential_ref", serialized)
        self.assertNotIn("cutroom/provider/account-ark", serialized)

    def test_direct_provider_public_metadata_round_trips_without_credential_reference(self):
        create_provider_account(
            self.db,
            account_id="account-direct",
            name="火山方舟直连",
            provider="volcengine_ark",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            credential_ref="cutroom/provider/account-direct",
            settings={
                "preset": "volcengine_ark",
                "protocol": "openai",
                "note": "分镜与包装",
                "default_model": "ep-example",
            },
        )

        account = next(
            item
            for item in list_provider_accounts(self.db)
            if item["accountId"] == "account-direct"
        )

        self.assertEqual(account["preset"], "volcengine_ark")
        self.assertEqual(account["note"], "分镜与包装")
        self.assertEqual(account["defaultModel"], "ep-example")
        self.assertIsNone(account["channelId"])
        serialized = json.dumps(account, ensure_ascii=False)
        self.assertNotIn("credentialRef", serialized)
        self.assertNotIn("cutroom/provider/account-direct", serialized)

    def test_text_work_is_split_into_independent_model_capabilities(self):
        self.assertIn("copywriting", CAPABILITY_SPECS)
        self.assertIn("storyboard", CAPABILITY_SPECS)
        self.assertIn("packaging", CAPABILITY_SPECS)
        self.assertEqual(CAPABILITY_SPECS["copywriting"]["label"], "文案分析与改写")
        self.assertEqual(CAPABILITY_SPECS["storyboard"]["label"], "分镜规划")
        self.assertEqual(CAPABILITY_SPECS["packaging"]["label"], "包装推荐")

        now = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        selections = {}
        for capability in ("copywriting", "storyboard", "packaging"):
            profile_id = f"profile-{capability}"
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=capability,
                account_id="account-ark",
                model=f"model-{capability}",
                settings={"protocol": "openai_chat"},
            )
            mark_profile_check(
                self.db, profile_id, available=True, checked_at=now
            )
            selections[capability] = profile_id

        bindings = bind_task_profiles(
            self.db,
            "task-1",
            selections,
            required_capabilities=("copywriting", "storyboard", "packaging"),
            now=now,
        )

        self.assertEqual(bindings["copywriting"]["model"], "model-copywriting")
        self.assertEqual(bindings["storyboard"]["model"], "model-storyboard")
        self.assertEqual(bindings["packaging"]["model"], "model-packaging")

    def test_planning_and_broll_are_canonical_user_facing_roles(self):
        self.assertEqual(CAPABILITY_SPECS["planning"]["label"], "文案策划")
        self.assertEqual(CAPABILITY_SPECS["broll"]["label"], "B-roll 画面")
        self.assertNotEqual(CAPABILITY_SPECS["planning"].get("visible"), False)
        for legacy in (
            "copywriting",
            "storyboard",
            "packaging",
            "general_text",
            "video_generation",
        ):
            self.assertFalse(CAPABILITY_SPECS[legacy]["visible"])

    def test_legacy_text_profile_can_be_frozen_as_one_planning_role(self):
        create_model_profile(
            self.db,
            profile_id="legacy-copywriting",
            capability="copywriting",
            display_name="旧智谱文案",
            account_id="account-ark",
            model="glm-5.3",
            settings={"protocol": "openai"},
        )
        checked_at = datetime.now(timezone.utc)
        mark_profile_check(
            self.db,
            "legacy-copywriting",
            available=True,
            checked_at=checked_at,
        )

        snapshots = resolve_task_profile_snapshots(
            self.db,
            {"planning": "legacy-copywriting"},
            required_capabilities=("planning",),
            now=checked_at,
        )

        self.assertEqual(snapshots["planning"]["capability"], "planning")
        self.assertEqual(snapshots["planning"]["profileId"], "legacy-copywriting")
        self.assertEqual(snapshots["planning"]["model"], "glm-5.3")

    def test_task_binding_requires_recent_successful_check_and_freezes_snapshot(self):
        self._create_profile("profile-a", "deepseek-v4", default=True)
        now = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)

        with self.assertRaisesRegex(ModelProfileUnavailable, "尚未通过可用性检测"):
            bind_task_profiles(
                self.db,
                "task-1",
                {"general_text": "profile-a"},
                required_capabilities=("general_text",),
                now=now,
            )

        mark_profile_check(
            self.db,
            "profile-a",
            available=True,
            checked_at=now - timedelta(minutes=5),
        )
        bindings = bind_task_profiles(
            self.db,
            "task-1",
            {"general_text": "profile-a"},
            required_capabilities=("general_text",),
            now=now,
        )
        self.assertEqual(bindings["general_text"]["model"], "deepseek-v4")
        self.assertEqual(bindings["general_text"]["channelId"], 7)
        self.assertEqual(bindings["general_text"]["baseUrl"], "http://127.0.0.1:3000")

        self.db.connection.execute(
            "UPDATE model_profiles SET model='changed-later' WHERE profile_id='profile-a'"
        )
        self.db.connection.commit()
        frozen = get_task_profile_binding(self.db, "task-1", "general_text")
        self.assertEqual(frozen["model"], "deepseek-v4")
        self.assertEqual(frozen["credentialRef"], "cutroom/provider/account-ark")
        public_bindings = list_task_profile_bindings(self.db, "task-1")
        self.assertEqual(public_bindings[0]["displayName"], "deepseek-v4")
        self.assertEqual(public_bindings[0]["model"], "deepseek-v4")
        self.assertNotIn("credentialRef", public_bindings[0])
        self.assertNotIn("accountSettings", public_bindings[0])

    def test_profile_delete_is_allowed_when_unused_and_blocked_when_bound(self):
        self._create_profile("unused-profile", "unused-model")
        self.assertEqual(
            delete_model_profile(self.db, "unused-profile"),
            {"profileId": "unused-profile", "deleted": True},
        )

        self._create_profile("bound-profile", "bound-model")
        checked_at = datetime.now(timezone.utc)
        mark_profile_check(
            self.db,
            "bound-profile",
            available=True,
            checked_at=checked_at,
        )
        bind_task_profiles(
            self.db,
            "task-1",
            {"general_text": "bound-profile"},
            required_capabilities=("general_text",),
            now=checked_at,
        )

        with self.assertRaisesRegex(ModelProfileInUse, "任务使用"):
            delete_model_profile(self.db, "bound-profile")

    def test_stale_or_failed_profile_cannot_be_bound(self):
        self._create_profile("profile-a", "deepseek-v4", default=True)
        now = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        mark_profile_check(
            self.db,
            "profile-a",
            available=True,
            checked_at=now - timedelta(hours=25),
        )
        with self.assertRaisesRegex(ModelProfileUnavailable, "检测结果已经过期"):
            bind_task_profiles(
                self.db,
                "task-1",
                {"general_text": "profile-a"},
                required_capabilities=("general_text",),
                now=now,
            )

    def test_task_binding_rejects_tampered_snapshot(self):
        self._create_profile("profile-a", "deepseek-v4", default=True)
        now = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
        mark_profile_check(self.db, "profile-a", available=True, checked_at=now)
        bind_task_profiles(
            self.db,
            "task-1",
            {"general_text": "profile-a"},
            required_capabilities=("general_text",),
            now=now,
        )
        row = self.db.connection.execute(
            "SELECT snapshot_json FROM task_model_bindings "
            "WHERE task_id='task-1' AND capability='general_text'"
        ).fetchone()
        snapshot = json.loads(row["snapshot_json"])
        snapshot["model"] = "tampered-model"
        self.db.connection.execute(
            "UPDATE task_model_bindings SET snapshot_json=? "
            "WHERE task_id='task-1' AND capability='general_text'",
            (json.dumps(snapshot),),
        )
        self.db.connection.commit()

        with self.assertRaisesRegex(ModelProfileUnavailable, "完整性校验失败"):
            get_task_profile_binding(self.db, "task-1", "general_text")

        mark_profile_check(
            self.db,
            "profile-a",
            available=False,
            error_code="MODEL_NOT_FOUND",
            error_message="model unavailable",
            checked_at=now,
        )
        with self.assertRaisesRegex(ModelProfileUnavailable, "当前不可用"):
            bind_task_profiles(
                self.db,
                "task-1",
                {"general_text": "profile-a"},
                required_capabilities=("general_text",),
                now=now,
            )

    def test_snapshot_settings_override_recomputes_digest_without_mutating_source(self):
        create_model_profile(
            self.db,
            profile_id="tts-override",
            capability="tts",
            display_name="配音覆盖",
            account_id="account-ark",
            model="seed-tts-2.0",
            settings={"protocol": "openai_speech", "voice": "voice-common"},
        )
        now = datetime.now(timezone.utc)
        mark_profile_check(
            self.db,
            "tts-override",
            available=True,
            checked_at=now,
        )
        original = resolve_task_profile_snapshots(
            self.db,
            {"tts": "tts-override"},
            required_capabilities=("tts",),
            now=now,
        )["tts"]

        overridden = override_snapshot_profile_settings(
            original,
            {
                "voice": "voice-task",
                "voice_name": "本条视频音色",
                "resource_id": "seed-tts-2.0",
            },
        )

        self.assertEqual(original["profileSettings"]["voice"], "voice-common")
        self.assertEqual(overridden["profileSettings"]["voice"], "voice-task")
        self.assertNotEqual(original["configDigest"], overridden["configDigest"])
        save_task_profile_bindings(self.db, "task-1", {"tts": overridden})
        frozen = get_task_profile_binding(self.db, "task-1", "tts")
        self.assertEqual(frozen["profileSettings"]["voice"], "voice-task")

        tampered = json.loads(json.dumps(frozen))
        tampered["profileSettings"]["voice"] = "tampered"
        self.db.connection.execute(
            "UPDATE task_model_bindings SET snapshot_json=? "
            "WHERE task_id='task-1' AND capability='tts'",
            (json.dumps(tampered),),
        )
        self.db.connection.commit()
        with self.assertRaisesRegex(ModelProfileUnavailable, "完整性校验失败"):
            get_task_profile_binding(self.db, "task-1", "tts")


if __name__ == "__main__":
    unittest.main()
