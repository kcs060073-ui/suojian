import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from media_catalog.ad_actions import (
    build_ad_packaging_preview_job,
    finalize_ad_storyboard,
    recommend_ad_storyboard,
    render_ad_packaging_preview,
    render_ad_and_qa,
)
from media_catalog.ad_workflow import create_ad_task
from media_catalog.candidate_versions import list_candidates
from media_catalog.credentials import save_capability_setting
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.material_authorization import confirm_material_authorization
from media_catalog.workbench_worker import enqueue_job
from media_catalog.model_profiles import (
    bind_task_profiles,
    create_model_profile,
    create_provider_account,
    mark_profile_check,
)
from media_catalog.packaging_plan import clone_plan, confirm_plan, disable_optional_slot, propose
from media_catalog.scene_cards import list_scene_cards, set_picture_lock
from media_catalog.snapshots import latest_review_candidate, record_render_dependency_snapshot
from media_catalog.task_sources import preview_source_sync, sync_task_sources
from media_catalog.tasks import set_status
from media_catalog.workbench_worker import ClaimedJob


class AdPackagingPreviewTests(unittest.TestCase):
    def setUp(self):
        self.policy_lookup = patch("media_catalog.packaging_plan.reference_policy_for_plan", return_value=None)
        self.policy_lookup.start()
        self.addCleanup(self.policy_lookup.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task_root = self.root / "runtime/workbench/tasks/task-ad"
        ad_dir = self.task_root / "artifacts/ad"
        ad_dir.mkdir(parents=True)
        self.script = self.task_root / "locked.txt"
        self.script.write_text("锁定文案", encoding="utf-8")
        self.voice = ad_dir / "voice.mp3"
        self.voice.write_bytes(b"existing-voice")
        (ad_dir / "adplan.json").write_text("{}", encoding="utf-8")
        (ad_dir / "adtiming.json").write_text("{}", encoding="utf-8")
        self.clip = self.root / "clip.mp4"
        self.clip.write_bytes(b"clip")
        self.current = SimpleNamespace(
            identity=SimpleNamespace(task_id="task-ad", plan_id="plan-ad"),
            duration_seconds=8.0,
            plan={
                "plan_id": "plan-ad",
                "items": [
                    {
                        "slot": "subtitle",
                        "asset_ref": "sub-a",
                        "is_backup": False,
                    }
                ],
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_ad_preview_uses_existing_voice_and_bounded_renderer_flags(self):
        compiled = SimpleNamespace(
            snapshot={"plan_id": "plan-ad"},
            plan_fields={"subtitlePresetId": "sub-a"},
            coverage=[
                {"slot": "voice", "status": "rendered"},
                {"slot": "subtitle", "status": "rendered"},
            ],
        )
        task = {"task_id": "task-ad", "title": "广告标题"}
        brief = {"aspect_ratio": "9:16"}
        timing = {
            "total": 15.0,
            "segments": [
                {"i": 0, "start": 0.0, "end": 15.0, "dur": 15.0}
            ],
        }
        cards = [
            {
                "asset_id": "asset-1",
                "narrative_text": "锁定文案",
                "enter_tc": 2.0,
                "exit_tc": 20.0,
                "segment_id": "seg-1",
                "confirm_status": 1,
            }
        ]
        calls = []

        def runner(command, **options):
            calls.append((command, options))
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"rendered-ad-preview")
            return {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
            }

        with (
            patch(
                "media_catalog.ad_actions._task_context",
                return_value=(self.task_root, task, brief, self.script, "sha"),
            ),
            patch(
                "media_catalog.ad_actions._validate_ad_outputs",
                return_value=({}, timing),
            ),
            patch("media_catalog.ad_actions.list_scene_cards", return_value=cards),
            patch(
                "media_catalog.ad_actions._resolved_asset_paths",
                return_value={"asset-1": self.clip},
            ),
            patch(
                "media_catalog.ad_actions.compile_packaging_layers",
                return_value=compiled,
            ),
            patch(
                "media_catalog.ad_actions.find_item",
                return_value={"id": "sub-a", "font": "font.otf"},
            ),
        ):
            result = render_ad_packaging_preview(
                object(), self.current, self.root, "pv-ad", runner
            )

        command = calls[0][0]
        payload = json.loads(
            Path(command[command.index("--plan") + 1]).read_text(encoding="utf-8")
        )
        self.assertEqual(payload["voice"], str(self.voice.resolve()))
        self.assertEqual(payload["segments"][0]["dur"], 8.0)
        self.assertEqual(payload["segments"][0]["sourceEnd"], 10.0)
        self.assertEqual(command[command.index("--max-duration") + 1], "8.0")
        self.assertEqual(command[command.index("--width") + 1], "540")
        self.assertEqual(command[command.index("--height") + 1], "960")
        self.assertEqual(command[command.index("--crf") + 1], "28")
        self.assertEqual(result["coverage"], compiled.coverage)

    def test_ad_preview_omits_source_window_when_no_window_was_locked(self):
        compiled = SimpleNamespace(
            snapshot={"plan_id": "plan-ad"},
            plan_fields={"subtitlePresetId": "sub-a"},
            coverage=[{"slot": "subtitle", "status": "rendered"}],
        )
        timing = {
            "total": 8.0,
            "segments": [{"i": 0, "start": 0.0, "end": 8.0, "dur": 8.0}],
        }
        cards = [{
            "asset_id": "asset-1",
            "narrative_text": "锁定文案",
            "enter_tc": None,
            "exit_tc": None,
            "segment_id": "seg-1",
            "confirm_status": 1,
        }]
        with (
            patch(
                "media_catalog.ad_actions._task_context",
                return_value=(
                    self.task_root,
                    {"task_id": "task-ad", "title": "广告标题"},
                    {"aspect_ratio": "9:16"},
                    self.script,
                    "sha",
                ),
            ),
            patch(
                "media_catalog.ad_actions._validate_ad_outputs",
                return_value=({}, timing),
            ),
            patch("media_catalog.ad_actions.list_scene_cards", return_value=cards),
            patch(
                "media_catalog.ad_actions._resolved_asset_paths",
                return_value={"asset-1": self.clip},
            ),
            patch(
                "media_catalog.ad_actions.compile_packaging_layers",
                return_value=compiled,
            ),
            patch(
                "media_catalog.ad_actions.find_item",
                return_value={"id": "sub-a", "font": "font.otf"},
            ),
        ):
            job = build_ad_packaging_preview_job(
                object(), self.current, self.root, "pv-ad-no-window"
            )

        payload = json.loads(Path(job["plan_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("sourceStart", payload["segments"][0])
        self.assertNotIn("sourceEnd", payload["segments"][0])


class AdActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.materials = self.root / "materials"
        self.materials.mkdir()
        (self.materials / "城市航拍.mp4").write_bytes(b"city-video")
        (self.materials / "厂房生产线.mp4").write_bytes(b"factory-video")
        register_library(
            self.db, "primary", "活动素材", "local", str(self.materials),
            purpose="task_specific",
        )
        created = create_ad_task(self.db, {
            "route": "ad_mix",
            "title": "广告动作测试",
            "final_script": "锁定原文",
            "primary_library_id": "primary",
            "selected_generic_ids": [],
            "include_default_generic": False,
            "purpose": "信息流获客",
            "target_duration_seconds": 12,
            "aspect_ratio": "9:16",
            "voice_mode": "ai_tts",
        }, self.root)
        self.task_id = created["task_id"]
        set_status(self.db, self.task_id, "brief_locked")
        preview = preview_source_sync(self.db, self.task_id)
        sync_task_sources(
            self.db,
            self.task_id,
            impact_token=preview["impact_token"],
        )
        save_capability_setting(
            self.db, "general_text", "火山方舟", "ad-model", "cutroom/general_text",
        )
        save_capability_setting(
            self.db, "tts", "火山豆包 TTS", "voice-id", "cutroom/tts",
        )
        create_provider_account(
            self.db,
            account_id="gateway-ad",
            name="广告网关",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/gateway-ad",
            settings={"channel_id": 41},
        )
        for capability, profile_id, model, settings in (
            (
                "general_text",
                "profile-ad-text",
                "ad-text-frozen",
                {"protocol": "openai"},
            ),
            (
                "storyboard",
                "profile-ad-storyboard",
                "ad-storyboard-frozen",
                {"protocol": "openai"},
            ),
            (
                "tts",
                "profile-ad-tts",
                "ad-tts-frozen",
                {"protocol": "openai", "voice": "zh_female_frozen"},
            ),
        ):
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=profile_id,
                account_id="gateway-ad",
                model=model,
                settings=settings,
            )
            mark_profile_check(
                self.db,
                profile_id,
                available=True,
                checked_at=datetime.now(timezone.utc),
            )
        bind_task_profiles(
            self.db,
            self.task_id,
            {
                "general_text": "profile-ad-text",
                "storyboard": "profile-ad-storyboard",
                "tts": "profile-ad-tts",
            },
            required_capabilities=("general_text", "storyboard", "tts"),
        )
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM task_model_bindings WHERE task_id=?",
                (self.task_id,),
            )
        bind_task_profiles(
            self.db,
            self.task_id,
            {
                "planning": "profile-ad-text",
                "tts": "profile-ad-tts",
            },
            required_capabilities=("planning", "tts"),
        )
        node = self.root / "node.exe"
        node.write_bytes(b"node")
        ts_node = self.root / "node_modules" / "ts-node" / "dist" / "bin.js"
        ts_node.parent.mkdir(parents=True)
        ts_node.write_text("runner", encoding="utf-8")
        for relative in (
            "scripts/render-ad.ts", "scripts/ad-style.ts", "scripts/load-env.ts",
            "src/ad-subtitle-layout.js", "media_catalog/ad_subtitles.py",
            "media_catalog/reference_editing.py", "media_catalog/camera_motion.py", "media_catalog/music_matching.py", "media_catalog/emphasis_packaging.py",
            "tsconfig.json", "assets/fonts/SourceHanSerifSC-Heavy.otf",
        ):
            runtime_file = self.root / relative
            runtime_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_file.write_text("isolated-runtime-fixture", encoding="utf-8")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def job(self):
        return ClaimedJob(
            job_id="job-ad-storyboard",
            job_type="recommend_storyboard",
            payload={
                "action": "recommend_storyboard",
                "task_id": self.task_id,
                "request_id": "request-1",
            },
            attempt_no=1,
            worker_id="worker-test",
            task_id=self.task_id,
            is_paid=True,
        )

    def test_blocked_ad_qa_is_recorded_as_a_visible_candidate(self):
        from media_catalog import ad_actions
        from media_catalog.qa import _save_report

        output = self.root / "blocked-candidate.mp4"
        output.write_bytes(b"blocked-render")
        rendered = record_render_dependency_snapshot(
            self.db,
            self.task_id,
            {
                "output_path": str(output),
                "dependency_digest": "blocked-dependency",
                "dependency_snapshot": {"route": "ad_mix", "files": []},
                "plan_path": str(self.root / "plan.json"),
                "ass_path": None,
                "packaging": {"plan_id": "plan-blocked"},
            },
            producer="assemble-ad-v1",
        )

        def blocked_qa(db, task_id, video_path, *, evidence=None):
            report = {
                "report_id": "qa-ad-blocked",
                "task_id": task_id,
                "rule_version": "qa-test",
                "video_path": video_path,
                "video_spec": {"width": 1080, "height": 1920},
                "status": "blocked",
                "issue_count": 1,
                "blocking_count": 1,
                "issues": [{
                    "check": "material_authorization",
                    "severity": "blocking",
                    "ok": False,
                    "detail": "authorization missing",
                }],
                "created_at": "2026-09-23 10:00:00",
            }
            _save_report(db, report)
            return report

        with patch("media_catalog.ad_actions.run_qa", side_effect=blocked_qa):
            result = ad_actions._finish_ad_render_qa(
                self.db, self.task_id, rendered
            )

        self.assertEqual(result["cursor"], "render_and_qa:qa_blocked")
        candidates = list_candidates(self.db, self.task_id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["status"], "qa_blocked")
        self.assertEqual(candidates[0]["qa_report_id"], "qa-ad-blocked")
        self.assertEqual(
            result["result"]["candidate"]["candidate_id"],
            candidates[0]["candidate_id"],
        )

    def test_storyboard_and_tts_are_task_scoped_and_match_only_bound_assets(self):
        calls = []

        def runner(command, **options):
            calls.append((command, options))
            plan = Path(command[command.index("--plan-output") + 1])
            timing = Path(command[command.index("--timing-output") + 1])
            voice = Path(command[command.index("--voice-output") + 1])
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text(json.dumps({
                "ratio": "9:16",
                "segments": [
                    {"text": "锁定", "query": "城市航拍", "prompt": "城市镜头", "type": "video"},
                    {"text": "原文", "query": "厂房生产线", "prompt": "生产镜头", "type": "video"},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            timing.write_text(json.dumps({
                "total": 2.4,
                "segments": [
                    {"i": 0, "text": "锁定", "start": 0, "end": 1.1, "dur": 1.1},
                    {"i": 1, "text": "原文", "start": 1.1, "end": 2.4, "dur": 1.3},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            voice.write_bytes(b"voice")
            return {
                "started": True, "timed_out": False, "returncode": 0,
                "stdout": "ok", "stderr": "",
            }

        result = recommend_ad_storyboard(self.db, self.job(), self.root, runner)
        self.assertEqual(result["cursor"], "recommend_storyboard:complete")
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertIn("ad_pipeline_runner.py", Path(command[3]).name)
        self.assertEqual(Path(options["cwd"]), self.root)
        self.assertNotIn("--general-model", command)
        self.assertNotIn("--voice", command)
        self.assertEqual(command[command.index("--task-id") + 1], self.task_id)
        self.assertEqual(
            command[command.index("--text-capability") + 1], "storyboard"
        )
        self.assertEqual(
            Path(command[command.index("--catalog-db") + 1]),
            self.db.path,
        )
        cards = list_scene_cards(self.db, self.task_id)
        self.assertEqual([card["narrative_text"] for card in cards], ["锁定", "原文"])
        self.assertTrue(all(card["asset_id"] for card in cards))
        self.assertTrue(all(card["mix_form"] == "broll全屏" for card in cards))
        bound_assets = self.db.connection.execute(
            """
            SELECT asset_id, role FROM task_assets
            WHERE task_id=? ORDER BY asset_id
            """,
            (self.task_id,),
        ).fetchall()
        self.assertEqual(
            {row["asset_id"] for row in bound_assets},
            {card["asset_id"] for card in cards},
        )
        self.assertTrue(all(row["role"] == "broll" for row in bound_assets))
        task_artifacts = (
            self.root / "runtime" / "workbench" / "tasks" / self.task_id
            / "artifacts" / "ad"
        )
        self.assertTrue((task_artifacts / "adplan.json").is_file())
        self.assertTrue((task_artifacts / "adtiming.json").is_file())
        self.assertTrue((task_artifacts / "voice.mp3").is_file())

        second = recommend_ad_storyboard(
            self.db,
            self.job(),
            self.root,
            lambda *_args, **_kwargs: self.fail("bound result must be reused"),
        )
        self.assertTrue(second["result"]["reused"])

    def test_rewritten_or_incomplete_copy_is_rejected_before_scene_cards(self):
        def runner(command, **_options):
            Path(command[command.index("--plan-output") + 1]).write_text(
                json.dumps({
                    "ratio": "9:16",
                    "segments": [
                        {"text": "改写", "query": "城市", "prompt": "镜头", "type": "video"}
                    ],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            Path(command[command.index("--timing-output") + 1]).write_text(
                json.dumps({
                    "total": 1,
                    "segments": [
                        {"i": 0, "text": "改写", "start": 0, "end": 1, "dur": 1}
                    ],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            Path(command[command.index("--voice-output") + 1]).write_bytes(b"voice")
            return {
                "started": True, "timed_out": False, "returncode": 0,
                "stdout": "ok", "stderr": "",
            }

        with self.assertRaisesRegex(RuntimeError, "AD_COPY_COVERAGE_INVALID"):
            recommend_ad_storyboard(self.db, self.job(), self.root, runner)
        self.assertEqual(list_scene_cards(self.db, self.task_id), [])

    def test_agent_ad_action_never_invokes_plan_ad_or_text_model(self):
        created = create_ad_task(
            self.db,
            {
                "route": "ad_mix",
                "title": "Agent 广告动作测试",
                "final_script": "锁定原文",
                "primary_library_id": "primary",
                "selected_generic_ids": [],
                "include_default_generic": False,
                "purpose": "信息流获客",
                "target_duration_seconds": 12,
                "aspect_ratio": "9:16",
                "voice_mode": "ai_tts",
                "orchestration_mode": "agent",
                "model_profile_ids": {"tts": "profile-ad-tts"},
            },
            self.root,
        )
        task_id = created["task_id"]
        set_status(self.db, task_id, "brief_locked")
        preview = preview_source_sync(self.db, task_id)
        sync_task_sources(
            self.db,
            task_id,
            impact_token=preview["impact_token"],
        )
        job_id = "job-agent-ad-storyboard"
        enqueue_job(
            self.db,
            job_id=job_id,
            job_type="recommend_storyboard",
            payload={"action": "recommend_storyboard", "task_id": task_id},
            task_id=task_id,
            cost_status="estimated",
            provider_request_id="agent-ad-request-1",
        )
        script_sha = created["task"]["brief"]["final_script"]["sha256"]
        binding = {
            "task_id": task_id,
            "script_sha256": script_sha,
            "source_snapshot_ids": [
                row[0]
                for row in self.db.connection.execute(
                    "SELECT snapshot_id FROM task_source_snapshots "
                    "WHERE task_id=? ORDER BY snapshot_id",
                    (task_id,),
                )
            ],
            "ratio": "9:16",
            "job_id": job_id,
        }
        plan = {
            "ratio": "9:16",
            "segments": [
                {
                    "text": "锁定",
                    "query": "城市航拍",
                    "prompt": "城市镜头",
                    "type": "video",
                },
                {
                    "text": "原文",
                    "query": "厂房生产线",
                    "prompt": "生产镜头",
                    "type": "video",
                },
            ],
        }
        commands = []

        def runner(command, **_options):
            commands.append(command)
            Path(command[command.index("--timing-output") + 1]).write_text(
                json.dumps(
                    {
                        "total": 2.4,
                        "segments": [
                            {
                                "i": 0,
                                "text": "锁定",
                                "start": 0,
                                "end": 1.1,
                                "dur": 1.1,
                            },
                            {
                                "i": 1,
                                "text": "原文",
                                "start": 1.1,
                                "end": 2.4,
                                "dur": 1.3,
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            Path(command[command.index("--voice-output") + 1]).write_bytes(b"voice")
            return {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
            }

        with self.assertRaisesRegex(RuntimeError, "TTS_COST_APPROVAL_REQUIRED"):
            finalize_ad_storyboard(
                self.db,
                task_id=task_id,
                project_root=self.root,
                plan=plan,
                input_binding=binding,
                input_digest="agent-input-digest",
                runner=runner,
                source="agent_artifact",
            )
        self.assertEqual(commands, [])
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE job_payloads SET cost_status='approved', "
                "requested_cost_status='approved' WHERE job_id=?",
                (job_id,),
            )
        result = finalize_ad_storyboard(
            self.db,
            task_id=task_id,
            project_root=self.root,
            plan=plan,
            input_binding=binding,
            input_digest="agent-input-digest",
            runner=runner,
            source="agent_artifact",
        )

        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertIn("ad_tts_runner.py", " ".join(command))
        self.assertNotIn("plan-ad.ts", " ".join(command))
        self.assertNotIn("--text-capability", command)
        self.assertEqual(result["source"], "agent_artifact")
        self.assertEqual(result["scene_card_count"], 2)
        self.assertIsNone(
            self.db.connection.execute(
                "SELECT 1 FROM task_model_bindings "
                "WHERE task_id=? AND capability IN ('planning','storyboard','general_text')",
                (task_id,),
            ).fetchone()
        )


    def test_locked_ad_renders_with_explicit_paths_and_creates_trusted_candidate(self):
        def planning_runner(command, **_options):
            Path(command[command.index("--plan-output") + 1]).write_text(
                json.dumps({
                    "ratio": "9:16",
                    "segments": [
                        {"text": "锁定", "query": "城市航拍", "prompt": "城市镜头", "type": "video"},
                        {"text": "原文", "query": "厂房生产线", "prompt": "生产镜头", "type": "video"},
                    ],
                }, ensure_ascii=False), encoding="utf-8",
            )
            Path(command[command.index("--timing-output") + 1]).write_text(
                json.dumps({
                    "total": 2.4,
                    "segments": [
                        {"i": 0, "text": "锁定", "start": 0, "end": 1.1, "dur": 1.1},
                        {"i": 1, "text": "原文", "start": 1.1, "end": 2.4, "dur": 1.3},
                    ],
                }, ensure_ascii=False), encoding="utf-8",
            )
            Path(command[command.index("--voice-output") + 1]).write_bytes(b"voice")
            return {"started": True, "timed_out": False, "returncode": 0, "stdout": "ok", "stderr": ""}

        recommend_ad_storyboard(self.db, self.job(), self.root, planning_runner)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE scene_cards SET enter_tc=5,exit_tc=8 WHERE task_id=? AND idx=0",
                (self.task_id,),
            )
        set_picture_lock(self.db, self.task_id, confirm_all=True)
        attestation = confirm_material_authorization(
            self.db, self.task_id, "fixture-reviewer"
        )
        plans = propose(self.db, self.task_id, None)
        self.assertTrue(plans)
        adopted = clone_plan(self.db, plans[0], plan_name="采用系统推荐")
        disable_optional_slot(self.db, adopted, "bgm")
        confirm_plan(self.db, adopted)
        active_plan_id = clone_plan(
            self.db, adopted, plan_name="最新包装门禁方案"
        )
        confirm_plan(self.db, active_plan_id)
        render_calls = []

        def render_runner(command, **options):
            render_calls.append((command, options))
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"rendered-ad")
            return {"started": True, "timed_out": False, "returncode": 0, "stdout": "ok", "stderr": ""}

        seen_evidence = []

        def passed_qa(db, task_id, video_path, *, evidence=None):
            seen_evidence.append(evidence)
            from media_catalog.qa import _save_report
            checks = [
                "video_evidence", "black_frame", "freeze", "silence", "clipping",
                "subtitle_evidence", "loudness_integrated", "true_peak",
                "material_fingerprint", "material_scope", "material_authorization",
            ]
            report = {
                "report_id": "qa-ad-pass",
                "task_id": task_id,
                "rule_version": "qa-test",
                "video_path": video_path,
                "video_spec": {"width": 1080, "height": 1920},
                "status": "passed",
                "issue_count": len(checks),
                "blocking_count": 0,
                "issues": [
                    {"check": check, "severity": "info", "ok": True, "detail": "passed"}
                    for check in checks
                ],
                "created_at": "2026-09-08 12:00:00",
            }
            _save_report(db, report)
            return report

        render_job = ClaimedJob(
            job_id="job-ad-render",
            job_type="render_and_qa",
            payload={"action": "render_and_qa", "task_id": self.task_id},
            attempt_no=1,
            worker_id="worker-test",
            task_id=self.task_id,
            is_paid=False,
        )
        with patch("media_catalog.ad_actions.run_qa", side_effect=passed_qa):
            result = render_ad_and_qa(self.db, render_job, self.root, render_runner)
            repeated = render_ad_and_qa(self.db, render_job, self.root, render_runner)
            self.assertTrue(repeated["result"]["reused"])
            with self.db.transaction() as connection:
                connection.execute("UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?", (self.task_id,))
            with self.assertRaisesRegex(RuntimeError, "PICTURE_LOCK_MISSING"):
                render_ad_and_qa(self.db, render_job, self.root, render_runner)
            with self.db.transaction() as connection:
                connection.execute("UPDATE picture_lock SET changed_since_lock=0 WHERE task_id=?", (self.task_id,))

        self.assertEqual(result["cursor"], "render_and_qa:complete")
        self.assertTrue(seen_evidence)
        self.assertTrue(Path(seen_evidence[0]["subtitle_path"]).is_file())
        self.assertEqual(
            seen_evidence[0]["dependency_snapshot"]["route"], "ad_mix"
        )
        self.assertEqual(
            seen_evidence[0]["shot_authorization"],
            {asset_id: "approved" for asset_id in attestation["assetIds"]},
        )
        self.assertEqual(
            seen_evidence[0]["dependency_snapshot"][
                "material_authorization_attestation"
            ]["attestationId"],
            attestation["attestationId"],
        )
        self.assertEqual(len(render_calls), 1)
        command, options = render_calls[0]
        self.assertEqual(Path(options["cwd"]), self.root)
        self.assertEqual(options["max_output_bytes"], 1024 * 1024)
        self.assertIn("--plan", command)
        self.assertIn("--output", command)
        rendered_plan = json.loads(Path(command[command.index("--plan") + 1]).read_text(encoding="utf-8"))
        with self.subTest("locked source window"):
            self.assertEqual(rendered_plan["segments"][0].get("sourceStart"), 5)
            self.assertEqual(rendered_plan["segments"][0].get("sourceEnd"), 8)
        with self.subTest("confirmed subtitle preset"):
            self.assertEqual(rendered_plan.get("subtitlePreset", {}).get("id"), "sub-heavy-orange")
            self.assertEqual(rendered_plan["subtitlePreset"]["primary_hex"], "#FFB300")
        with self.subTest("formal render consumes the shared packaging compiler"):
            self.assertIn("sfx", rendered_plan)
            self.assertIn("watermark", rendered_plan)
        output = Path(command[command.index("--output") + 1])
        self.assertTrue(output.is_file())
        candidate = latest_review_candidate(self.db, self.task_id)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["producer"], "assemble-ad-v1")
        self.assertEqual(candidate["output_path"], str(output.resolve()))
        self.assertEqual(candidate["dependency_snapshot"]["route"], "ad_mix")
        self.assertEqual(
            candidate["dependency_snapshot"]["packaging_compiled"]["plan_id"],
            active_plan_id,
        )
        self.assertEqual(
            candidate["dependency_snapshot"]["packaging"]["plan_id"],
            active_plan_id,
        )

    def test_ad_reuse_rejects_changed_renderer_font_and_missing_runtime(self):
        self.test_locked_ad_renders_with_explicit_paths_and_creates_trusted_candidate()
        for relative in ("scripts/render-ad.ts", "assets/fonts/SourceHanSerifSC-Heavy.otf"):
            with self.subTest(relative=relative):
                runtime_file = self.root / relative
                original = runtime_file.read_bytes()
                runtime_file.write_bytes(b"changed-runtime")
                with self.assertRaisesRegex(RuntimeError, "DEPENDENCY_SNAPSHOT_CHANGED"):
                    render_ad_and_qa(self.db, self.job(), self.root, None)
                runtime_file.write_bytes(original)
        (self.root / "scripts" / "render-ad.ts").unlink()
        with self.assertRaisesRegex(RuntimeError, "AD_RENDER_DEPENDENCY_MISSING|DEPENDENCY_SNAPSHOT_CHANGED"):
            render_ad_and_qa(self.db, self.job(), self.root, None)

    def test_ad_reuse_rejects_withdrawn_confirmed_packaging(self):
        self.test_locked_ad_renders_with_explicit_paths_and_creates_trusted_candidate()
        # Current SQLite triggers make confirmed plans immutable. Simulate an
        # invalid legacy/boundary read to ensure cached execution still validates it.
        with patch("media_catalog.ad_actions.get_plan", return_value={
            "task_id": self.task_id, "status": "draft",
        }):
            with self.assertRaisesRegex(RuntimeError, "PACKAGING_PLAN_MISSING"):
                render_ad_and_qa(self.db, self.job(), self.root, None)

    def test_ad_publish_rejects_reconfirmed_subtitle_before_reuse_or_new_package(self):
        from media_catalog import publish
        from media_catalog.ad_actions import build_ad_publish_package
        from media_catalog.packaging_plan import replace_item
        from tests.review_helpers import approve_review

        self.test_locked_ad_renders_with_explicit_paths_and_creates_trusted_candidate()
        candidate = latest_review_candidate(self.db, self.task_id)
        row = self.db.connection.execute(
            "SELECT report_json FROM qa_reports WHERE report_id=?",
            (candidate["qa_report_id"],),
        ).fetchone()
        report = json.loads(row[0])
        report["video_spec"]["duration"] = 2.4
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE qa_reports SET report_json=? WHERE report_id=?",
                (json.dumps(report), candidate["qa_report_id"]),
            )
        approve_review(self.db, self.task_id, candidate["qa_snapshot_id"],
                       candidate["qa_report_id"], "fixture-reviewer")

        def cover(_ffmpeg, _video, _timestamp, target):
            target.write_bytes(b"fixture-cover")

        with (
            patch.object(publish, "DELIVER_ROOT", self.root / "publish"),
            patch.object(publish, "find_ffmpeg", return_value=Path("ffmpeg")),
            patch.object(publish, "_extract_cover", side_effect=cover),
        ):
            publish_job = self.job()
            publish_job.payload.update({
                "tags": ["招商", "产业"],
                "cover_choice": "candidate-2",
                "delivery_type": "editable",
            })
            first = build_ad_publish_package(self.db, publish_job, self.root, None)
            repeated = build_ad_publish_package(self.db, publish_job, self.root, None)
            self.assertTrue(repeated["result"]["reused"])
            self.assertEqual(first["result"]["cover_choice"], "candidate-2")
            self.assertEqual(first["result"]["delivery_type"], "editable")
            self.assertTrue(
                Path(first["result"]["package_dir"], "delivery-manifest.json").is_file()
            )
            old_plan = candidate["dependency_snapshot"]["packaging"]["plan_id"]
            new_plan = clone_plan(self.db, old_plan, plan_name="new confirmed subtitles")
            replace_item(self.db, new_plan, "subtitle", "sub-ad-smiley-punch")
            confirm_plan(self.db, new_plan)
            for title in (None, "new publish copy"):
                job = self.job()
                job.payload["title"] = title
                with self.subTest(title=title):
                    with self.assertRaisesRegex(RuntimeError, "AD_RENDER_INPUT_CHANGED"):
                        build_ad_publish_package(self.db, job, self.root, None)
            self.assertTrue(Path(first["result"]["package_dir"]).is_dir())
            self.assertEqual(self.db.connection.execute(
                "SELECT COUNT(*) FROM publish_records WHERE task_id=?", (self.task_id,),
            ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
