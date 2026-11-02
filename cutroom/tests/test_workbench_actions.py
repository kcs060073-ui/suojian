import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import media_catalog.workbench_actions as actions
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.model_profiles import (
    bind_task_profiles,
    create_model_profile,
    create_provider_account,
    mark_profile_check,
)
from media_catalog.task_sources import preview_source_sync, set_task_sources
from media_catalog.tasks import create_task, set_status
from media_catalog.workbench_actions import transcribe
from media_catalog.workbench_worker import ClaimedJob


class WorkbenchActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.task_id = "task-alpha"
        self.task_root = (
            self.root / "runtime" / "workbench" / "tasks" / self.task_id
        )
        (self.task_root / "inputs").mkdir(parents=True)
        python_exe = self.root / ".venv" / "Scripts" / "python.exe"
        python_exe.parent.mkdir(parents=True)
        python_exe.write_bytes(b"test-python")
        self.video = self.root / "source.mp4"
        self.video.write_bytes(b"immutable-source-video")
        self.primary = self.root / "primary-library"
        self.primary.mkdir()
        (self.primary / "park-note.txt").write_text("园区素材", encoding="utf-8")
        register_library(
            self.db,
            "primary",
            "本次园区镜头",
            "local",
            str(self.primary),
            purpose="task_specific",
        )
        self.script = "锁定原文"
        self.script_path = self.task_root / "inputs" / "final-script.txt"
        self.script_path.write_text(self.script, encoding="utf-8")
        create_task(
            self.db,
            self.task_id,
            "测试数字人",
            "digital_human",
            brief={
                "subtype": "zhaoshang",
                "digital_human_source": {"path": str(self.video.resolve())},
                "final_script": {
                    "relative_path": "inputs/final-script.txt",
                    "sha256": hashlib.sha256(
                        self.script.encode("utf-8")
                    ).hexdigest(),
                },
            },
        )
        set_task_sources(
            self.db,
            self.task_id,
            primary_library_id="primary",
            selected_generic_ids=[],
            include_default_generic=False,
        )
        set_status(self.db, self.task_id, "brief_locked")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_action_handlers_expose_packaging_preview_worker(self):
        runner = object()
        job = ClaimedJob(
            job_id="packaging-preview-job",
            job_type="packaging_preview",
            payload={"preview_id": "pv-1"},
            attempt_no=1,
            worker_id="worker-test",
            task_id=self.task_id,
            is_paid=False,
        )
        with patch.object(
            actions,
            "run_packaging_preview_action",
            return_value={"result": {"status": "ready"}},
        ) as preview_action:
            handlers = actions.build_action_handlers(
                self.db, self.root, runner=runner
            )
            result = handlers["packaging_preview"](job)

        preview_action.assert_called_once_with(
            self.db, job, self.root.resolve(), runner
        )
        self.assertEqual(result["result"]["status"], "ready")

    def test_action_handlers_expose_packaging_catalog_check_worker(self):
        runner = object()
        job = ClaimedJob(
            job_id="packaging-catalog-job",
            job_type="packaging_catalog_check",
            payload={"check_id": "pcc-1"},
            attempt_no=1,
            worker_id="worker-test",
            task_id=None,
            is_paid=False,
        )
        with patch.object(
            actions,
            "run_packaging_catalog_check_action",
            return_value={"result": {"status": "completed"}},
        ) as catalog_action:
            handlers = actions.build_action_handlers(
                self.db, self.root, runner=runner
            )
            result = handlers["packaging_catalog_check"](job)

        catalog_action.assert_called_once_with(
            self.db, job, self.root.resolve(), runner
        )
        self.assertEqual(result["result"]["status"], "completed")

    def test_action_handlers_expose_task_preview_worker(self):
        runner = object()
        job = self.job("generate_preview", asset_id="asset-video")
        with patch.object(
            actions,
            "generate_preview",
            return_value={"result": {"proxy": "asset-video.proxy.mp4"}},
        ) as preview_action:
            handlers = actions.build_action_handlers(
                self.db, self.root, runner=runner
            )
            result = handlers["generate_preview"](job)

        preview_action.assert_called_once_with(
            self.db, job, self.root.resolve(), runner
        )
        self.assertEqual(result["result"]["proxy"], "asset-video.proxy.mp4")

    def test_generate_preview_uses_browser_safe_proxy_pipeline(self):
        runner = object()
        job = self.job("generate_preview", asset_id="asset-video")
        ffmpeg = self.root / "ffmpeg.exe"
        with (
            patch.object(
                actions,
                "_task_context",
                return_value=(self.task_root, {"task_id": self.task_id}),
            ),
            patch.object(actions, "resolve_rough_media") as resolve_media,
            patch.object(actions, "find_ffmpeg", return_value=ffmpeg),
            patch.object(
                actions,
                "generate_asset_previews",
                return_value={"proxy": "cache/asset-video.proxy.mp4"},
            ) as generate,
        ):
            result = actions.generate_preview(self.db, job, self.root, runner)

        resolve_media.assert_called_once_with(
            self.db, self.task_id, "asset-video", "preview"
        )
        self.assertEqual(generate.call_args.args[:3], (self.db, "asset-video", ffmpeg))
        self.assertEqual(generate.call_args.args[3], {"thumbnail", "proxy"})
        self.assertEqual(result["result"]["asset_id"], "asset-video")
        self.assertEqual(result["result"]["proxy"], "cache/asset-video.proxy.mp4")

    def test_anchor_time_uses_locked_script_position_when_whisper_text_differs(self):
        script = "新疆一度绿电三毛一，是东部三分之一。综合算力成本低40%以上。"
        spoken = "新疆一度綠電三毛一是東部三分之一綜合算力成本低百分之四十以上"
        words = [
            {"w": character, "start": index * 0.1, "end": (index + 1) * 0.1}
            for index, character in enumerate(spoken)
        ]

        start, end = actions._anchor_time(
            script,
            words,
            "综合算力成本低40%以上。",
        )

        self.assertGreater(start, 0)
        self.assertGreater(end, start)
        self.assertLessEqual(end, words[-1]["end"])

    def test_anchor_time_rejects_text_outside_locked_script(self):
        with self.assertRaisesRegex(RuntimeError, "不属于锁定文案"):
            actions._anchor_time(
                "锁定文案",
                [{"w": "锁", "start": 0.0, "end": 0.1}],
                "模型改写",
            )

    def job(self, job_type="transcribe", **payload):
        return ClaimedJob(
            job_id=f"job-{job_type}",
            job_type=job_type,
            payload={"action": job_type, "task_id": self.task_id, **payload},
            attempt_no=1,
            worker_id="worker-test",
            task_id=self.task_id,
            is_paid=False,
        )

    def test_transcribe_uses_task_scoped_explicit_outputs_and_reuses_bound_result(self):
        calls = []

        def runner(command, **options):
            calls.append((command, options))
            self.assertIsInstance(command, list)
            srt = Path(command[command.index("--srt-output") + 1])
            words = Path(command[command.index("--words-output") + 1])
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:00,900\nWhisper错字\n",
                encoding="utf-8",
            )
            words.write_text(
                json.dumps(
                    [
                        {"w": "锁定", "start": 0.0, "end": 0.4},
                        {"w": "原文", "start": 0.4, "end": 0.9},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

        first = transcribe(self.db, self.job(), self.root, runner)
        self.assertEqual(first.get("cursor"), "transcribe:complete")
        self.assertEqual(first["progress"], {"current": 1, "total": 1})
        self.assertEqual(first["phase_label"], "语音识别完成")
        self.assertFalse(first["result"]["reused"])
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertEqual(Path(command[0]), self.root / ".venv" / "Scripts" / "python.exe")
        self.assertEqual(command[1:3], ["-X", "utf8"])
        self.assertEqual(Path(command[3]), self.root / "scripts" / "transcribe.py")
        self.assertEqual(Path(options["cwd"]), self.root)
        self.assertNotIn("shell", options)

        srt_path = self.task_root / "artifacts" / "transcript.srt"
        words_path = self.task_root / "artifacts" / "transcript.words.json"
        metadata_path = self.task_root / "artifacts" / "transcription.json"
        self.assertTrue(srt_path.is_file())
        self.assertTrue(words_path.is_file())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["task_id"], self.task_id)
        self.assertEqual(metadata["script_sha256"], hashlib.sha256(self.script.encode()).hexdigest())

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("已绑定的转写结果不得重复执行")

        second = transcribe(self.db, self.job(), self.root, must_not_run)
        self.assertTrue(second["result"]["reused"])
        self.assertEqual(second["result"]["words_path"], str(words_path.resolve()))

    def test_transcribe_script_accepts_explicit_outputs_without_breaking_positional_input(self):
        isolated_root = self.root / "isolated-script"
        scripts = isolated_root / "scripts"
        scripts.mkdir(parents=True)
        shutil.copyfile(
            Path(__file__).parents[1] / "scripts" / "transcribe.py",
            scripts / "transcribe.py",
        )
        fake_module = isolated_root / "faster_whisper.py"
        fake_module.write_text(
            """
class Word:
    def __init__(self, word, start, end):
        self.word, self.start, self.end = word, start, end

class Segment:
    text = '错误识别文本'
    start = 0.0
    end = 0.9
    words = [Word('锁定', 0.0, 0.4), Word('原文', 0.4, 0.9)]

class WhisperModel:
    def __init__(self, *_args, **_kwargs):
        pass
    def transcribe(self, *_args, **_kwargs):
        return [Segment()], object()
""".lstrip(),
            encoding="utf-8",
        )
        explicit = isolated_root / "explicit"
        explicit.mkdir()
        srt_path = explicit / "task.srt"
        words_path = explicit / "task.words.json"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(isolated_root)
        result = subprocess.run(
            [
                sys.executable,
                str(scripts / "transcribe.py"),
                str(self.video),
                "tiny",
                "--srt-output",
                str(srt_path),
                "--words-output",
                str(words_path),
            ],
            cwd=isolated_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("错误识别文本", srt_path.read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads(words_path.read_text(encoding="utf-8"))[0]["w"],
            "锁定",
        )

    def test_sync_sources_scans_only_bound_scope_and_completes_material_stage(self):
        preview = preview_source_sync(self.db, self.task_id)
        result = actions.sync_sources(
            self.db,
            self.job("sync_sources", impact_token=preview["impact_token"]),
            self.root,
            lambda *_args, **_kwargs: self.fail("素材同步不应启动外部 shell"),
        )
        self.assertEqual(result.get("cursor"), "sync_sources:complete")
        self.assertEqual(result["phase_label"], "素材增量同步完成")
        self.assertEqual(
            result["result"]["sources"]["primary"]["library_id"],
            "primary",
        )
        row = self.db.connection.execute(
            "SELECT status FROM task_stage_states "
            "WHERE task_id=? AND stage_key='material_sync'",
            (self.task_id,),
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id=?",
                (self.task_id,),
            ).fetchone()[0],
            1,
        )

    def test_recommend_storyboard_uses_locked_timing_and_only_bound_assets(self):
        artifacts = self.task_root / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "transcript.srt").write_text("timed", encoding="utf-8")
        (artifacts / "transcript.words.json").write_text(
            json.dumps(
                [
                    {"w": "锁定", "start": 0.0, "end": 0.4},
                    {"w": "原文", "start": 0.4, "end": 0.9},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        transcript_binding = {
            "task_id": self.task_id,
            "script_sha256": hashlib.sha256(self.script.encode()).hexdigest(),
        }
        source_stat = self.video.stat()
        transcript_binding.update(
            {
                "source_path": str(self.video.resolve()),
                "source_size_bytes": source_stat.st_size,
                "source_mtime_ns": source_stat.st_mtime_ns,
                "model": "small",
            }
        )
        transcript_binding["input_digest"] = hashlib.sha256(
            json.dumps(
                transcript_binding,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        (artifacts / "transcription.json").write_text(
            json.dumps(transcript_binding, ensure_ascii=False), encoding="utf-8"
        )

        bound_image = self.primary / "园区大门.png"
        bound_image.write_bytes(b"bound-image")
        unbound = self.root / "unbound-library"
        unbound.mkdir()
        unbound_image = unbound / "园区大门更匹配.png"
        unbound_image.write_bytes(b"unbound-image")
        register_library(
            self.db, "unbound", "未绑定素材", "local", str(unbound),
            purpose="task_specific",
        )
        with self.db.transaction() as connection:
            for asset_id, library_id, relative_path, payload in (
                ("asset-digital", "primary", "source.mp4", self.video.read_bytes()),
                ("asset-bound", "primary", bound_image.name, bound_image.read_bytes()),
                ("asset-unbound", "unbound", unbound_image.name, unbound_image.read_bytes()),
            ):
                connection.execute(
                    "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) "
                    "VALUES(?,?,?,?,datetime('now'))",
                    (
                        asset_id,
                        "video" if asset_id == "asset-digital" else "image",
                        len(payload),
                        "fp-" + asset_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,state) "
                    "VALUES(?,?,?,?, 'available')",
                    (asset_id, library_id, relative_path, 1),
                )
            connection.execute(
                "INSERT INTO task_assets(task_id,asset_id,role) VALUES(?,?,?)",
                (self.task_id, "asset-digital", "digital_human"),
            )
            connection.execute(
                "INSERT INTO capability_settings(capability,provider,model,credential_ref,last_checked_at) "
                "VALUES('general_text','火山方舟','doubao-test','cutroom/general_text',datetime('now'))"
            )

        create_provider_account(
            self.db,
            account_id="gateway-digital",
            name="数字人网关",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/gateway-digital",
            settings={"channel_id": 31},
        )
        create_model_profile(
            self.db,
            profile_id="profile-digital-frozen",
            capability="general_text",
            display_name="数字人冻结模型",
            account_id="gateway-digital",
            model="doubao-frozen",
            settings={"protocol": "openai"},
        )
        mark_profile_check(
            self.db,
            "profile-digital-frozen",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        bind_task_profiles(
            self.db,
            self.task_id,
            {"general_text": "profile-digital-frozen"},
            required_capabilities=("general_text",),
        )
        create_model_profile(
            self.db,
            profile_id="profile-digital-storyboard",
            capability="storyboard",
            display_name="数字人分镜模型",
            account_id="gateway-digital",
            model="storyboard-frozen",
            settings={"protocol": "openai"},
        )
        mark_profile_check(
            self.db,
            "profile-digital-storyboard",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        bind_task_profiles(
            self.db,
            self.task_id,
            {"storyboard": "profile-digital-storyboard"},
            required_capabilities=("storyboard",),
        )
        create_model_profile(
            self.db,
            profile_id="profile-digital-packaging",
            capability="packaging",
            display_name="数字人包装模型",
            account_id="gateway-digital",
            model="packaging-frozen",
            settings={"protocol": "openai"},
        )
        mark_profile_check(
            self.db,
            "profile-digital-packaging",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )
        bind_task_profiles(
            self.db,
            self.task_id,
            {"packaging": "profile-digital-packaging"},
            required_capabilities=("packaging",),
        )
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM task_model_bindings WHERE task_id=?",
                (self.task_id,),
            )
        bind_task_profiles(
            self.db,
            self.task_id,
            {"planning": "profile-digital-frozen"},
            required_capabilities=("planning",),
        )

        calls = []

        def runner(command, **options):
            calls.append((command, options))
            brief_path = Path(command[command.index("--brief-output") + 1])
            plan_path = Path(command[command.index("--plan-output") + 1])
            brief_path.write_text(
                json.dumps(
                    {
                        "keywords": ["锁定"],
                        "punchLines": [],
                        "title": {"text": "测试", "durationSec": 1},
                        "broll": [
                            {
                                "anchorText": "原文",
                                "query": "园区大门",
                                "prompt": "园区大门缓慢推近",
                                "kind": "image",
                                "durationSec": 0.4,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            plan_path.write_text('{"speed":1.25,"titles":[],"inserts":[]}', encoding="utf-8")
            return {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
            }

        first = actions.recommend_storyboard(
            self.db,
            self.job("recommend_storyboard", request_id="req-1"),
            self.root,
            runner,
        )
        self.assertEqual(first.get("cursor"), "recommend_storyboard:complete")
        self.assertFalse(first["result"]["reused"])
        self.assertNotIn("credential-secret", json.dumps(first, ensure_ascii=False))
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[1:3], ["-X", "utf8"])
        self.assertIn("--script", command)
        self.assertIn("--brief-output", command)
        self.assertIn("--plan-output", command)
        self.assertIn("plan_brief_runner.py", command[3])
        self.assertNotIn("--model", command)
        self.assertEqual(command[command.index("--task-id") + 1], self.task_id)
        self.assertEqual(
            command[command.index("--capability") + 1], "storyboard"
        )
        self.assertEqual(
            command[command.index("--packaging-capability") + 1], "packaging"
        )
        self.assertEqual(
            Path(command[command.index("--catalog-db") + 1]),
            self.db.path,
        )
        self.assertFalse(any("API_KEY" in item for item in command))
        self.assertNotIn("env", options)

        cards = [
            dict(row) for row in self.db.connection.execute(
                "SELECT idx,narrative_text,narrative_start,narrative_end,asset_id,mix_form,confirm_status "
                "FROM scene_cards WHERE task_id=? ORDER BY idx",
                (self.task_id,),
            ).fetchall()
        ]
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["asset_id"], "asset-digital")
        self.assertEqual(cards[0]["mix_form"], "数字人全屏")
        self.assertEqual(cards[0]["narrative_text"], self.script)
        self.assertEqual(cards[1]["asset_id"], "asset-bound")
        self.assertEqual(cards[1]["narrative_text"], "原文")
        self.assertEqual(
            (cards[1]["narrative_start"], cards[1]["narrative_end"]),
            (0.4, 0.9),
        )
        self.assertTrue(all(card["confirm_status"] == 0 for card in cards))
        self.assertNotIn(
            "asset-unbound",
            [row[0] for row in self.db.connection.execute(
                "SELECT asset_id FROM task_assets WHERE task_id=?",
                (self.task_id,),
            ).fetchall()],
        )
        stage = self.db.connection.execute(
            "SELECT status FROM task_stage_states WHERE task_id=? AND stage_key='ai_storyboard'",
            (self.task_id,),
        ).fetchone()
        self.assertEqual(stage["status"], "completed")

        metadata_path = artifacts / "storyboard" / "recommendation.json"
        interrupted = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(interrupted["source"], "standalone_model")
        interrupted["state"] = "generated"
        metadata_path.write_text(
            json.dumps(interrupted, ensure_ascii=False), encoding="utf-8"
        )

        second = actions.recommend_storyboard(
            self.db,
            self.job("recommend_storyboard", request_id="req-1"),
            self.root,
            lambda *_args, **_kwargs: self.fail("相同 AI 推荐不得重复付费执行"),
        )
        self.assertTrue(second["result"]["reused"])

    def test_plan_brief_runner_reads_credential_inside_atr_owned_process(self):
        fake_ts_node = self.root / "fake-ts-node.py"
        fake_ts_node.write_text(
            """
import json
import os
import sys

assert os.environ['ARK_API_KEY']
assert os.environ['ARK_MODEL'] == 'wrapper-model'
brief = sys.argv[sys.argv.index('--brief-output') + 1]
plan = sys.argv[sys.argv.index('--plan-output') + 1]
open(brief, 'w', encoding='utf-8').write(json.dumps({'broll': []}))
open(plan, 'w', encoding='utf-8').write(json.dumps({'speed': 1.25}))
""".lstrip(),
            encoding="utf-8",
        )
        output = self.root / "wrapper-output"
        output.mkdir()
        brief_path = output / "brief.json"
        plan_path = output / "plan.json"
        repo_root = Path(__file__).parents[1]
        environment = dict(os.environ)
        environment["ARK_API_KEY"] = "wrapper-secret"
        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "plan_brief_runner.py"),
                "--project-root", str(repo_root),
                "--node", sys.executable,
                "--ts-node", str(fake_ts_node),
                "--model", "wrapper-model",
                "--script", str(self.script_path),
                "--brief-output", str(brief_path),
                "--plan-output", str(plan_path),
                "--speed", "1.25",
                "--profile", "zhaoshang",
            ],
            cwd=repo_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(brief_path.is_file())
        self.assertTrue(plan_path.is_file())
        self.assertNotIn("wrapper-secret", result.stdout + result.stderr)

    def test_render_and_qa_uses_versioned_output_and_reuses_completed_action(self):
        artifacts = self.task_root / "artifacts"
        storyboard = artifacts / "storyboard"
        storyboard.mkdir(parents=True, exist_ok=True)
        words_path = artifacts / "transcript.words.json"
        words_path.write_text(
            '[{"w":"锁定原文","start":0.0,"end":0.9}]', encoding="utf-8"
        )
        brief_path = storyboard / "brief.json"
        brief_path.write_text(
            json.dumps(
                {
                    "keywords": ["锁定"],
                    "punchLines": ["锁定原文"],
                    "title": {"text": "测试标题", "durationSec": 3},
                    "broll": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        calls = []

        def workbench_runner(_command, **options):
            self.assertIn("timeout_seconds", options)
            self.assertIn("max_output_bytes", options)
            self.assertEqual(options.get("env"), {"SAFE": "1"})
            return {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
            }

        def fake_assemble(db, task_id, **options):
            calls.append((db, task_id, options))
            output = Path(options["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"candidate-video")
            return {
                "task_id": task_id,
                "status": "awaiting_human_review",
                "output_path": str(output),
                "qa": {"report_id": "qa-1", "status": "passed", "blocking": 0},
                "snapshots": {"qa_snapshot_id": "snapshot-qa-1"},
            }

        # This boundary test substitutes assemble, so no real trusted snapshot exists.
        # The integrated AssembleTests exercises the real completed-render binding.
        with patch("media_catalog.workbench_actions.assemble", side_effect=fake_assemble), patch("media_catalog.workbench_actions._validate_completed_render"):
            first = actions.render_and_qa(
                self.db, self.job("render_and_qa"), self.root, workbench_runner
            )
            second = actions.render_and_qa(
                self.db, self.job("render_and_qa"), self.root,
                lambda *_args, **_kwargs: self.fail("已完成候选不得重复渲染"),
            )

        expected = self.root / "out" / self.task_id / "candidate-v1" / "video-final.mp4"
        self.assertEqual(Path(first["result"]["output_path"]), expected)
        self.assertEqual(first["cursor"], "render_and_qa:complete")
        self.assertEqual(first["phase_label"], "精剪成片与自动质检完成")
        self.assertEqual(len(calls), 1)
        options = calls[0][2]
        semantics_path = storyboard / "semantics.json"
        self.assertEqual(
            json.loads(semantics_path.read_text(encoding="utf-8")),
            {"keywords": ["锁定"], "punchLines": ["锁定原文"]},
        )
        self.assertTrue(options["render"])
        self.assertEqual(Path(options["output_path"]), expected)
        self.assertEqual(
            options["subtitle_sources"],
            {
                "script_path": str(self.script_path.resolve()),
                "words_path": str(words_path.resolve()),
                "semantics_path": str(semantics_path.resolve()),
            },
        )
        self.assertIs(options["runner"], options["subtitle_runner"])
        process_result = options["runner"](
            ["tool"],
            cwd=str(self.root),
            env={"SAFE": "1"},
            timeout=12,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(process_result.returncode, 0)
        self.assertEqual(process_result.stdout, "ok")
        self.assertTrue(second["result"]["reused"])

    def test_changed_render_action_inputs_create_new_version_preserving_old(self):
        from media_catalog.snapshots import snapshot
        self.test_render_and_qa_uses_versioned_output_and_reuses_completed_action()
        old = self.root / "out" / self.task_id / "candidate-v1" / "video-final.mp4"
        old_bytes = old.read_bytes()
        snapshot(self.db, self.task_id, "mix", {}, str(old))
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO scene_cards(card_id,task_id,idx,narrative_text) VALUES('rework',?,1,'new cut')", (self.task_id,))
        def render(db, task_id, **options):
            output = Path(options["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"new-candidate")
            return {"status": "awaiting_human_review", "output_path": str(output), "snapshots": {"qa_snapshot_id": "qa-2"}}
        with patch("media_catalog.workbench_actions.assemble", side_effect=render):
            second = actions.render_and_qa(self.db, self.job("render_and_qa"), self.root, None)
        self.assertEqual(Path(second["result"]["output_path"]).parent.name, "candidate-v2")
        self.assertEqual(old.read_bytes(), old_bytes)

    def test_subprocess_adapter_uses_tree_fallback_only_for_atr_internal_error(self):
        def atr_internal_error(_command, **_options):
            return {
                "started": False,
                "timed_out": False,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "error": "ATR 未提供可核验的进程终态",
                "error_code": "runtime.internal_error",
            }

        expected = subprocess.CompletedProcess(
            ["tool.exe"], 0, stdout="ok", stderr=""
        )
        with patch(
            "media_catalog.render_mix_adapter._run_process_tree",
            return_value=expected,
        ) as fallback:
            result = actions._as_subprocess_runner(atr_internal_error)(
                ["tool.exe"],
                cwd=str(self.root),
                env={"PATH": "controlled"},
                timeout=30,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        self.assertIs(result, expected)
        self.assertEqual(fallback.call_args.kwargs["timeout"], 30)
        self.assertEqual(fallback.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(fallback.call_args.kwargs["stderr"], subprocess.PIPE)

    def test_checked_runner_uses_tree_fallback_for_atr_internal_error(self):
        def atr_internal_error(_command, **_options):
            return {
                "started": False,
                "timed_out": False,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "error": "ATR 未提供可核验的进程终态",
                "error_code": "runtime.internal_error",
            }

        expected = subprocess.CompletedProcess(
            ["tool.exe"], 0, stdout="ok", stderr=""
        )
        with patch(
            "media_catalog.render_mix_adapter._run_process_tree",
            return_value=expected,
        ) as fallback:
            result = actions._run_checked(
                atr_internal_error,
                ["tool.exe"],
                cwd=self.root,
                timeout_seconds=30,
                max_output_bytes=2048,
                env={"PATH": "controlled"},
            )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "ok")
        self.assertEqual(fallback.call_args.kwargs["timeout"], 30)
        self.assertEqual(fallback.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(fallback.call_args.kwargs["stderr"], subprocess.PIPE)

    def test_checked_runner_uses_tree_fallback_when_atr_missing(self):
        def atr_missing(_command, **_options):
            return {
                "started": False, "timed_out": False, "returncode": None,
                "stdout": "", "stderr": "",
                "error": "无法保证硬超时：本机 Agent Tool Runtime（ATR）不可用",
            }

        expected = subprocess.CompletedProcess(["tool.exe"], 0, stdout="ok", stderr="")
        with patch("media_catalog.render_mix_adapter._run_process_tree",
                   return_value=expected) as fallback:
            result = actions._run_checked(
                atr_missing, ["tool.exe"], cwd=self.root,
                timeout_seconds=30, max_output_bytes=2048,
            )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(fallback.call_args.kwargs["timeout"], 30)

    def test_build_publish_package_reuses_reviewed_candidate_without_rendering(self):
        candidate = self.root / "out" / self.task_id / "candidate-v1" / "video-final.mp4"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"reviewed-candidate")
        reviewed = {
            "candidate_id": "candidate-v1",
            "qa_snapshot_id": "snapshot-qa-1",
            "qa_report_id": "qa-1",
            "output_path": str(candidate),
        }
        calls = []

        def fake_assemble(db, task_id, **options):
            calls.append((db, task_id, options))
            return {
                "task_id": task_id,
                "status": "published",
                "go": True,
                "output_path": str(candidate),
                "publish": {
                    "record_id": "pub-1",
                    "status": "cleared",
                    "package_dir": str(self.root / "out" / "publish" / self.task_id),
                },
            }

        job = self.job(
            "build_publish_package",
            platform="视频号",
            title="园区视频",
            description="简介",
            tags=["园区", "招商"],
            cover_choice="candidate-2",
            external_cover_path=None,
            delivery_type="editable",
        )
        with (
            patch("media_catalog.workbench_actions.latest_review_candidate", return_value=reviewed),
            patch("media_catalog.workbench_actions.assemble", side_effect=fake_assemble),
        ):
            first = actions.build_publish_package(self.db, job, self.root, object())
            second = actions.build_publish_package(
                self.db, job, self.root,
                lambda *_args, **_kwargs: self.fail("已完成发布包不得重复生成"),
            )

        self.assertEqual(first["cursor"], "build_publish_package:complete")
        self.assertEqual(first["phase_label"], "发布包生成完成")
        self.assertEqual(len(calls), 2)
        options = calls[0][2]
        self.assertTrue(options["render"])
        self.assertEqual(Path(options["output_path"]), candidate)
        self.assertEqual(options["cover_choice"], "candidate-2")
        self.assertIsNone(options["external_cover_path"])
        self.assertEqual(options["delivery_type"], "editable")
        self.assertNotIn("runner", options)
        self.assertTrue(second["result"]["reused"])

    def test_publish_worker_rejects_a_job_bound_to_an_older_candidate(self):
        candidate = self.root / "candidate-current.mp4"
        candidate.write_bytes(b"current")
        reviewed = {
            "candidate_id": "candidate-current",
            "qa_snapshot_id": "snapshot-current",
            "qa_report_id": "qa-current",
            "output_path": str(candidate),
        }
        job = self.job(
            "build_publish_package",
            candidate_id="candidate-old",
            platform="视频号",
        )
        with patch(
            "media_catalog.workbench_actions.latest_review_candidate",
            return_value=reviewed,
        ), patch("media_catalog.workbench_actions.assemble") as assembled:
            with self.assertRaisesRegex(RuntimeError, "PUBLISH_CANDIDATE_STALE"):
                actions.build_publish_package(self.db, job, self.root, None)
        assembled.assert_not_called()
    def test_changed_publish_copy_can_retry_after_a_blocked_action(self):
        candidate = self.root / "candidate.mp4"
        candidate.write_bytes(b"candidate")
        reviewed = {
            "candidate_id": "candidate-v1",
            "qa_snapshot_id": "snapshot-qa-1",
            "qa_report_id": "qa-1",
            "output_path": str(candidate),
        }
        def build(db, task_id, **options):
            blocked = options["title"] == "100%"
            return {"status": "publish_blocked" if blocked else "published", "output_path": str(candidate), "publish": {"record_id": "old" if blocked else "new", "status": "blocked" if blocked else "cleared"}}
        with patch("media_catalog.workbench_actions.latest_review_candidate", return_value=reviewed), patch("media_catalog.workbench_actions.assemble", side_effect=build):
            first = actions.build_publish_package(self.db, self.job("build_publish_package", title="100%"), self.root, None)
            second = actions.build_publish_package(self.db, self.job("build_publish_package", title="normal"), self.root, None)
        self.assertEqual(first["result"]["status"], "publish_blocked")
        self.assertEqual(second["result"]["status"], "published")
        self.assertNotEqual(first["result"]["publish"]["record_id"], second["result"]["publish"]["record_id"])



if __name__ == "__main__":
    unittest.main()
