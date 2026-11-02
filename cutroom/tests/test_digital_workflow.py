import json
import hashlib
import inspect
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import media_catalog.digital_workflow as digital_workflow
from media_catalog.database import CatalogDatabase
from media_catalog.digital_workflow import (
    DigitalTaskPreflightError,
    DigitalWorkflowDependencies,
    create_digital_task,
    preflight_digital_task,
    recover_incomplete_digital_tasks,
)
from media_catalog.libraries import register_library
from media_catalog.model_profiles import (
    create_model_profile,
    create_provider_account,
    get_task_profile_binding,
    mark_profile_check,
)
from media_catalog.task_sources import get_task_sources
from media_catalog.workflow import WORKFLOW_STAGES


KNOWN_MACHINE = {
    "status": "estimated",
    "min": 420,
    "max": 780,
    "unit": "seconds",
    "basis": "同设备、同路线的 3 条 60—90 秒数字人历史记录",
    "reason": None,
}
KNOWN_HUMAN = {
    "status": "estimated",
    "min": 240,
    "max": 480,
    "unit": "seconds",
    "basis": "两次无培训操作记录中的四次确认耗时",
    "reason": None,
}
UNAVAILABLE = {
    "status": "unavailable",
    "min": None,
    "max": None,
    "unit": "seconds",
    "basis": None,
    "reason": "尚无适用基线",
}

VALID_MEDIA_JSON = json.dumps(
    {
        "format": {"duration": "86.25"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "duration": "86.25",
                "disposition": {"attached_pic": 0},
            },
            {
                "index": 1,
                "codec_type": "audio",
                "duration": "86.25",
                "channels": 1,
                "sample_rate": "44100",
            },
        ],
    }
)


class _DigitalWorkflowFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.primary = self.root / "park"
        self.generic = self.root / "generic"
        self.default_generic = self.root / "default-generic"
        for path in (self.primary, self.generic, self.default_generic):
            path.mkdir()
        self.video = self.root / "zen-video.mp4"
        self.video.write_bytes(b"final-digital-human-video")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        register_library(
            self.db, "primary", "本次主素材", "local", str(self.primary),
            purpose="task_specific",
        )
        register_library(
            self.db, "generic", "本次指定通用素材", "local", str(self.generic),
            purpose="generic",
        )
        register_library(
            self.db, "default-generic", "系统默认通用素材", "local",
            str(self.default_generic), purpose="generic",
            is_default_generic=True,
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def request(self, **overrides):
        request = {
            "title": "新疆数字人 v2",
            "route": "digital_human",
            "subtype": "zhaoshang",
            "digital_human_mp4": str(self.video),
            "final_script": "这是禅镜实际使用的最终口播文案。",
            "primary_library_id": "primary",
            "selected_generic_ids": ["generic"],
            "include_default_generic": True,
        }
        request.update(overrides)
        return request

    @staticmethod
    def _snapshot(path):
        path = Path(path)
        stat = path.stat()
        return {
            "path": str(path.resolve()),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "quick_fingerprint": "test-fingerprint",
            "file_identity": "test-file-identity",
        }

    @staticmethod
    def _directory_status(path):
        path = Path(path)
        return {
            "exists": path.exists(),
            "is_dir": path.is_dir(),
            "readable": path.is_dir(),
        }

    def dependencies(
        self,
        *,
        estimate_source=None,
        queue_observer=None,
        disk_free_bytes=None,
        tool_status=None,
        directory_status=None,
        file_snapshot=None,
        process_runner=None,
    ):
        return DigitalWorkflowDependencies(
            file_snapshot=file_snapshot or self._snapshot,
            directory_status=directory_status or self._directory_status,
            tool_status=tool_status or (
                lambda name, _root: {
                    "available": True,
                    "path": f"C:/tools/{name}.exe",
                    "basis": f"测试确认 {name} 可用",
                }
            ),
            disk_free_bytes=disk_free_bytes or (lambda _path: 10**12),
            estimate_source=estimate_source or (
                lambda _request, _context: {
                    "machine_seconds": KNOWN_MACHINE,
                    "human_seconds": KNOWN_HUMAN,
                    "api_cost": {
                        "status": "unavailable",
                        "min": None,
                        "max": None,
                        "unit": None,
                        "basis": None,
                        "reason": "文本模型价格或用量依据未配置",
                        "included_costs": ["AI 分镜文本模型"],
                        "excluded_costs": ["另行确认的缺失画面生成"],
                    },
                }
            ),
            queue_observer=queue_observer or (
                lambda _db, _request: {
                    "heavy_slot": "busy",
                    "running_job_id": "render-1",
                    "waiting_count": 2,
                    "observed_at": "2026-09-06T06:00:00Z",
                    "estimated_wait_seconds": {
                        "status": "estimated",
                        "min": 120,
                        "max": 360,
                        "unit": "seconds",
                        "basis": "当前任务剩余时间与排队任务历史耗时",
                        "reason": None,
                    },
                }
            ),
            now=lambda: "2026-09-06T06:00:00Z",
            process_runner=process_runner or self._trusted_process_runner,
        )

    @staticmethod
    def _trusted_process_runner(
        command, *, cwd, timeout_seconds, max_output_bytes
    ):
        del cwd, timeout_seconds, max_output_bytes
        if "-show_entries" in command:
            output = VALID_MEDIA_JSON
        elif "-version" in command:
            executable = Path(command[0]).name.lower()
            output = (
                "ffprobe version 8.1.1"
                if "ffprobe" in executable
                else "ffmpeg version 8.1.1"
            )
        elif "--version" in command:
            output = (
                "v10.9.2"
                if any("ts-node" in item.lower() for item in command)
                else "v22.14.0"
            )
        else:
            output = ""
        return {
            "started": True,
            "timed_out": False,
            "returncode": 0,
            "stdout": output,
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }


class DigitalPreflightTests(_DigitalWorkflowFixture):
    def test_dependencies_expose_one_shared_process_runner(self):
        self.assertIn(
            "process_runner", DigitalWorkflowDependencies.__dataclass_fields__
        )

    def test_default_dependencies_accept_injected_process_runner(self):
        self.assertIn(
            "process_runner",
            inspect.signature(digital_workflow._default_dependencies).parameters,
        )

    def test_default_faster_whisper_probe_disables_bytecode_writes(self):
        python_path = self.root / ".venv" / "Scripts" / "python.exe"
        python_path.parent.mkdir(parents=True)
        python_path.write_bytes(b"")
        transcribe = self.root / "scripts" / "transcribe.py"
        transcribe.parent.mkdir(parents=True)
        transcribe.write_text("", encoding="utf-8")

        calls = []

        def runner(command, **options):
            calls.append((list(command), options))
            return self._trusted_process_runner(command, **options)

        self.assertIn(
            "process_runner",
            inspect.signature(digital_workflow._default_tool_status).parameters,
        )
        if "process_runner" not in inspect.signature(
            digital_workflow._default_tool_status
        ).parameters:
            return
        observation = digital_workflow._default_tool_status(
            "faster-whisper", self.root, process_runner=runner
        )

        self.assertTrue(observation["available"])
        self.assertEqual(calls[0][0][1:3], ["-B", "-c"])
        self.assertEqual(calls[0][1]["cwd"], self.root)
        self.assertLessEqual(calls[0][1]["timeout_seconds"], 20)

    def test_default_dependencies_use_one_runner_for_all_tools_and_media(self):
        python_path = self.root / ".venv" / "Scripts" / "python.exe"
        python_path.parent.mkdir(parents=True)
        python_path.write_bytes(b"")
        transcribe = self.root / "scripts" / "transcribe.py"
        transcribe.parent.mkdir(parents=True)
        transcribe.write_text("", encoding="utf-8")
        ts_node = self.root / "node_modules" / "ts-node" / "dist" / "bin.js"
        ts_node.parent.mkdir(parents=True)
        ts_node.write_bytes(b"")
        calls = []

        def runner(command, **options):
            calls.append((list(command), options))
            return self._trusted_process_runner(command, **options)

        with patch(
            "media_catalog.digital_workflow.find_ffmpeg",
            return_value=str(self.root / "ffmpeg.exe"),
        ), patch(
            "media_catalog.digital_workflow.find_ffprobe",
            return_value=str(self.root / "ffprobe.exe"),
        ), patch(
            "media_catalog.digital_workflow._which_tool",
            return_value=str(self.root / "node.exe"),
        ), patch("media_catalog.digital_workflow.subprocess.run") as old_run:
            old_run.return_value.returncode = 0
            result = preflight_digital_task(
                self.db,
                self.request(include_default_generic=False),
                self.root,
                dependencies=digital_workflow._default_dependencies(
                    process_runner=runner
                ),
            )

        self.assertEqual(result["blocking"], [])
        commands = [item[0] for item in calls]
        self.assertTrue(any(command[-1] == "-version" for command in commands))
        self.assertTrue(any(command[-1] == "--version" for command in commands))
        self.assertTrue(any(command[1:3] == ["-B", "-c"] for command in commands))
        self.assertTrue(any("-show_entries" in command for command in commands))
        self.assertEqual(len(commands), len(digital_workflow.REQUIRED_TOOLS) + 1)
        self.assertTrue(all(isinstance(command, list) for command in commands))
        self.assertTrue(all(item[1]["cwd"] == self.root for item in calls))
        old_run.assert_not_called()

    def test_local_ts_node_probe_runs_js_entry_with_node_without_batch_shell(self):
        node_path = self.root / "node.exe"
        ts_node_entry = self.root / "node_modules" / "ts-node" / "dist" / "bin.js"
        ts_node_entry.parent.mkdir(parents=True)
        ts_node_entry.write_text("", encoding="utf-8")
        calls = []

        def runner(command, **options):
            calls.append((list(command), options))
            return self._trusted_process_runner(command, **options)

        with patch(
            "media_catalog.digital_workflow._which_tool",
            return_value=str(node_path),
        ):
            observation = digital_workflow._default_tool_status(
                "ts-node", self.root, process_runner=runner
            )

        self.assertTrue(observation["available"])
        self.assertEqual(
            calls[0][0], [str(node_path), str(ts_node_entry), "--version"]
        )

    def test_tool_timeout_nonzero_and_start_failure_are_blocking(self):
        cases = (
            (
                "timeout",
                lambda command, **_options: {
                    "started": True,
                    "timed_out": True,
                    "returncode": None,
                    "stdout": "",
                    "stderr": "",
                }
                if Path(command[0]).name.lower().startswith("ffmpeg")
                else self._trusted_process_runner(command, **_options),
                "超时",
            ),
            (
                "nonzero",
                lambda command, **_options: {
                    "started": True,
                    "timed_out": False,
                    "returncode": 9,
                    "stdout": "",
                    "stderr": "C:/secret/token/file: failed\n" * 1000,
                }
                if Path(command[0]).name.lower().startswith("node")
                else self._trusted_process_runner(command, **_options),
                "退出码 9",
            ),
            (
                "damaged",
                lambda command, **_options: (_ for _ in ()).throw(
                    OSError("not a valid application")
                )
                if Path(command[0]).name.lower().startswith("ffprobe")
                else self._trusted_process_runner(command, **_options),
                "无法启动",
            ),
        )
        for label, runner, reason in cases:
            with self.subTest(label=label), patch(
                "media_catalog.digital_workflow.find_ffmpeg",
                return_value=str(self.root / "ffmpeg.exe"),
            ), patch(
                "media_catalog.digital_workflow.find_ffprobe",
                return_value=str(self.root / "ffprobe.exe"),
            ), patch(
                "media_catalog.digital_workflow._which_tool",
                return_value=str(self.root / "node.exe"),
            ):
                result = preflight_digital_task(
                    self.db,
                    self.request(include_default_generic=False),
                    self.root,
                    dependencies=digital_workflow._default_dependencies(
                        process_runner=runner
                    ),
                )
            missing = [
                item for item in result["blocking"]
                if item["code"] == "REQUIRED_TOOL_MISSING"
            ]
            self.assertTrue(missing)
            self.assertTrue(any(reason in (item["basis"] or "") for item in missing))
            self.assertTrue(all(len(item["basis"] or "") <= 300 for item in missing))
            self.assertTrue(
                all("C:/secret/token" not in (item["basis"] or "") for item in missing)
            )

    def test_tool_discovery_failure_does_not_expose_search_paths(self):
        with patch(
            "media_catalog.digital_workflow.find_ffmpeg",
            side_effect=digital_workflow.ToolNotFound(
                "searched C:/secret/token/tool-directory"
            ),
        ):
            observation = digital_workflow._default_tool_status(
                "ffmpeg",
                self.root,
                process_runner=self._trusted_process_runner,
            )
        self.assertFalse(observation["available"])
        self.assertIn("找不到", observation["basis"])
        self.assertNotIn("C:/secret/token", observation["basis"])
        self.assertLessEqual(len(observation["basis"]), 300)

    def test_injected_tool_basis_redacts_absolute_command_paths(self):
        result = preflight_digital_task(
            self.db,
            self.request(include_default_generic=False),
            self.root,
            dependencies=self.dependencies(
                tool_status=lambda name, _root: {
                    "available": True,
                    "path": f"C:/tools/{name}.exe",
                    "basis": (
                        "执行 C:/secret/token/private-tool.exe --version 成功；"
                        + "x" * 1000
                    ),
                }
            ),
        )
        tool_checks = [
            item for item in result["checks"]
            if item["code"] in {"REQUIRED_TOOL_READY", "REQUIRED_TOOL_MISSING"}
        ]
        self.assertEqual(len(tool_checks), len(digital_workflow.REQUIRED_TOOLS))
        self.assertTrue(
            all("C:/secret/token" not in (item["basis"] or "") for item in tool_checks)
        )
        self.assertTrue(all(len(item["basis"] or "") <= 300 for item in tool_checks))

    @unittest.skipUnless(os.name == "nt", "Windows uses ATR Job Objects")
    def test_default_process_runner_is_shell_free_hidden_and_output_bounded(self):
        atr = self.root / "atr.exe"
        atr.write_bytes(b"fixture")
        tool = self.root / "tool.exe"
        calls = []
        response = {
            "schema_version": "1.0",
            "status": "success",
            "phase": "execute",
            "result": {
                "exit_code": 0,
                "stdout": "v1.2.3",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
        }

        def launcher(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command, 0, json.dumps(response).encode("utf-8"), b""
            )

        result = digital_workflow._windows_atr_process_runner(
            [str(tool), "--version"],
            cwd=self.root,
            timeout_seconds=3,
            max_output_bytes=32,
            atr_path=atr,
            launcher=launcher,
        )

        command, kwargs = calls[0]
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(Path(kwargs["cwd"]), self.root)
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertEqual(command[0], str(atr.resolve()))
        self.assertEqual(command[1:4], ["run-process", "--exe", str(tool.resolve())])
        self.assertIn("--arg", command)
        self.assertEqual(command[command.index("--timeout-ms") + 1], "3000")
        self.assertEqual(command[command.index("--max-output-bytes") + 1], "32")
        self.assertEqual(command[command.index("--caller-agent") + 1], "codex")
        self.assertIn("--json", command)
        self.assertEqual(result["returncode"], 0)
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 32)
        self.assertLessEqual(len(result["stderr"].encode("utf-8")), 32)
        self.assertFalse(result["stdout_truncated"])
        self.assertFalse(result["stderr_truncated"])

    @unittest.skipUnless(os.name == "nt", "Windows uses ATR Job Objects")
    def test_windows_atr_runner_clamps_timeout_to_supported_one_hour(self):
        atr = self.root / "atr.exe"
        atr.write_bytes(b"fixture")
        tool = self.root / "tool.exe"
        calls = []
        response = {
            "schema_version": "1.0",
            "status": "success",
            "phase": "execute",
            "result": {
                "exit_code": 0,
                "stdout": "ok",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
        }

        def launcher(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command, 0, json.dumps(response).encode("utf-8"), b""
            )

        result = digital_workflow._windows_atr_process_runner(
            [str(tool), "--render"],
            cwd=self.root,
            timeout_seconds=7200,
            max_output_bytes=64,
            atr_path=atr,
            launcher=launcher,
        )

        command, kwargs = calls[0]
        self.assertEqual(command[command.index("--timeout-ms") + 1], "3600000")
        self.assertEqual(kwargs["timeout"], 3610)
        self.assertTrue(result["started"])

    @unittest.skipUnless(os.name == "nt", "Windows uses ATR Job Objects")
    def test_windows_atr_runner_preserves_internal_error_code_for_safe_fallback(self):
        atr = self.root / "atr.exe"
        atr.write_bytes(b"fixture")
        response = {
            "schema_version": "1.0",
            "status": "permanent_failure",
            "phase": "output",
            "error": {
                "code": "runtime.internal_error",
                "message": "The runtime could not complete the operation.",
            },
        }

        result = digital_workflow._windows_atr_process_runner(
            [str(self.root / "tool.exe"), "--render"],
            cwd=self.root,
            timeout_seconds=2,
            max_output_bytes=64,
            atr_path=atr,
            launcher=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 4, json.dumps(response).encode("utf-8"), b""
            ),
        )

        self.assertFalse(result["started"])
        self.assertEqual(result["error_code"], "runtime.internal_error")

    def test_default_process_runner_reports_real_start_failure(self):
        runner = getattr(digital_workflow, "_default_process_runner", None)
        self.assertIsNotNone(runner)
        if runner is None:
            return
        result = runner(
            [str(self.root / "definitely-missing-tool.exe"), "--version"],
            cwd=self.root,
            timeout_seconds=1,
            max_output_bytes=64,
        )
        self.assertFalse(result["started"])
        self.assertIsNone(result["returncode"])
        self.assertLessEqual(len(result["error"]), 200)

    @unittest.skipUnless(os.name == "nt", "Windows uses ATR Job Objects")
    def test_default_process_runner_kills_probe_at_hard_timeout(self):
        atr = self.root / "atr.exe"
        atr.write_bytes(b"fixture")
        response = {
            "schema_version": "1.0",
            "status": "permanent_failure",
            "phase": "timeout",
            "result": {
                "exit_code": 1,
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
            "error": {"code": "process.timeout", "message": "bounded"},
        }
        result = digital_workflow._windows_atr_process_runner(
            [str(self.root / "tool.exe"), "--version"],
            cwd=self.root,
            timeout_seconds=1,
            max_output_bytes=64,
            atr_path=atr,
            launcher=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 2, json.dumps(response).encode("utf-8"), b""
            ),
        )
        self.assertTrue(result["timed_out"], result)
        self.assertIsNone(result["returncode"])

    @unittest.skipUnless(os.name == "nt", "Windows uses ATR Job Objects")
    def test_default_process_runner_timeout_kills_child_before_it_can_write_marker(self):
        child = self.root / "child_marker.py"
        parent = self.root / "parent_spawns_child.py"
        marker = self.root / "child-leaked.marker"
        child.write_text(
            "import pathlib, sys, time\n"
            "time.sleep(1.8)\n"
            "pathlib.Path(sys.argv[1]).write_text('leaked', encoding='utf-8')\n",
            encoding="utf-8",
        )
        parent.write_text(
            "import subprocess, sys, time\n"
            "flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n"
            "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], "
            "creationflags=flags)\n"
            "time.sleep(10)\n",
            encoding="utf-8",
        )

        result = digital_workflow._default_process_runner(
            [
                sys.executable,
                str(parent),
                str(child),
                str(marker),
            ],
            cwd=self.root,
            timeout_seconds=1,
            max_output_bytes=4096,
        )
        time.sleep(2.2)

        self.assertTrue(result["timed_out"], result)
        self.assertFalse(marker.exists(), "timed-out child escaped and wrote marker")

    @unittest.skipUnless(os.name == "nt", "ATR is the Windows process owner")
    def test_windows_atr_runner_fails_closed_on_untrusted_terminal_results(self):
        runner = getattr(digital_workflow, "_windows_atr_process_runner", None)
        self.assertIsNotNone(runner)
        if runner is None:
            return
        atr = self.root / "atr.exe"
        atr.write_bytes(b"fixture")

        def completed(stdout, *, returncode=0, stderr=b""):
            return subprocess.CompletedProcess([], returncode, stdout, stderr)

        cases = {
            "malformed-json": completed(b"not-json"),
            "invalid-utf8": completed(
                b'{"schema_version":"1.0","status":"success","phase":"execute",'
                b'"result":{"exit_code":0,"stdout":"v1.2.\xff","stderr":"",'
                b'"stdout_truncated":false,"stderr_truncated":false}}'
            ),
            "uncertain-status": completed(
                json.dumps({"schema_version": "1.0", "status": "running"}).encode()
            ),
            "truncated-target": completed(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "status": "success",
                        "phase": "execute",
                        "result": {
                            "exit_code": 0,
                            "stdout": "v22.14.0",
                            "stderr": "",
                            "stdout_truncated": True,
                            "stderr_truncated": False,
                        },
                    }
                ).encode()
            ),
            "timeout-status-with-successful-atr-exit": completed(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "status": "permanent_failure",
                        "phase": "timeout",
                        "error": {"code": "process.timeout", "message": "bounded"},
                    }
                ).encode(),
                returncode=0,
            ),
        }
        for label, response in cases.items():
            with self.subTest(label=label):
                result = runner(
                    [str(self.root / "tool.exe"), "--version"],
                    cwd=self.root,
                    timeout_seconds=2,
                    max_output_bytes=64,
                    atr_path=atr,
                    launcher=lambda *_args, value=response, **_kwargs: value,
                )
                self.assertIsNot(result.get("started"), True)
                self.assertIsNone(result.get("returncode"))
                self.assertIn("ATR", result.get("error", ""))

        failed_start = runner(
            [str(self.root / "tool.exe"), "--version"],
            cwd=self.root,
            timeout_seconds=2,
            max_output_bytes=64,
            atr_path=atr,
            launcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("secret launch detail")
            ),
        )
        self.assertIsNot(failed_start.get("started"), True)
        self.assertIn("无法保证硬超时", failed_start.get("error", ""))
        self.assertNotIn("secret launch detail", failed_start.get("error", ""))

        missing_runtime = runner(
            [str(self.root / "tool.exe"), "--version"],
            cwd=self.root,
            timeout_seconds=2,
            max_output_bytes=64,
            atr_path=self.root / "missing-atr.exe",
            launcher=lambda *_args, **_kwargs: self.fail(
                "missing ATR must block before launching"
            ),
        )
        self.assertIsNot(missing_runtime.get("started"), True)
        self.assertIn("无法保证硬超时", missing_runtime.get("error", ""))
        self.assertIn(
            "无法保证硬超时",
            digital_workflow._process_failure_reason(
                missing_runtime, "node --version"
            ),
        )

    def test_tool_version_output_requires_real_signature_and_no_truncation(self):
        validator = getattr(digital_workflow, "_validate_tool_probe_output", None)
        self.assertIsNotNone(validator)
        if validator is None:
            return
        valid_outputs = {
            "ffmpeg": "ffmpeg version 8.1.1-full_build Copyright FFmpeg",
            "ffprobe": "ffprobe version 8.1.1-full_build Copyright FFmpeg",
            "node": "v22.14.0",
            "ts-node": "v10.9.2",
        }
        for name, output in valid_outputs.items():
            with self.subTest(name=name):
                result = validator(
                    name,
                    {
                        "started": True,
                        "timed_out": False,
                        "returncode": 0,
                        "stdout": output,
                        "stderr": "",
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                    },
                )
                self.assertIsNone(result)

        invalid = (
            ("ffmpeg", "", ""),
            ("ffmpeg", "ffprobe version 8.1.1", ""),
            (
                "ffmpeg",
                "exit0 garbage\nffmpeg version 8.1.1 Copyright FFmpeg",
                "",
            ),
            ("ffprobe", "exit0 garbage", ""),
            ("node", "node version 22", ""),
            ("ts-node", "v10", ""),
        )
        for name, stdout, stderr in invalid:
            with self.subTest(name=name, stdout=stdout):
                reason = validator(
                    name,
                    {
                        "started": True,
                        "timed_out": False,
                        "returncode": 0,
                        "stdout": stdout,
                        "stderr": stderr,
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                    },
                )
                self.assertIsInstance(reason, str)

        for stream in ("stdout_truncated", "stderr_truncated"):
            payload = {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": "v22.14.0",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
            payload[stream] = True
            with self.subTest(stream=stream):
                reason = validator("node", payload)
                self.assertIn("上限", reason)

    def test_default_runner_rejects_empty_and_arbitrary_bytes_named_mp4(self):
        for label, payload in (
            ("empty", b""),
            ("arbitrary", b"not-an-mp4"),
        ):
            with self.subTest(label=label):
                self.video.write_bytes(payload)
                result = preflight_digital_task(
                    self.db,
                    self.request(include_default_generic=False),
                    self.root,
                    dependencies=self.dependencies(
                        process_runner=digital_workflow._default_process_runner
                    ),
                )
                self.assertIn(
                    "DIGITAL_HUMAN_MP4_INVALID",
                    {item["code"] for item in result["blocking"]},
                )
                self.assertNotIn(
                    "DIGITAL_HUMAN_MP4_READY",
                    {item["code"] for item in result["checks"]},
                )

    def test_media_probe_rejects_nonzero_malformed_audio_only_and_missing_duration(self):
        cases = {
            "nonzero": {
                "started": True,
                "timed_out": False,
                "returncode": 1,
                "stdout": "",
                "stderr": "invalid data",
            },
            "malformed": {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": "{not-json",
                "stderr": "",
            },
            "audio-only": {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "format": {"duration": "4.0"},
                        "streams": [{"codec_type": "audio", "duration": "4.0"}],
                    }
                ),
                "stderr": "",
            },
            "missing-duration": {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": json.dumps(
                    {"format": {}, "streams": [{"codec_type": "video"}]}
                ),
                "stderr": "",
            },
            "video-without-audio": {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "format": {"duration": "4.0"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "duration": "4.0",
                                "disposition": {"attached_pic": 0},
                            }
                        ],
                    }
                ),
                "stderr": "",
            },
            "cover-art-with-audio": {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "format": {"duration": "4.0"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "duration": "4.0",
                                "disposition": {"attached_pic": 1},
                            },
                            {
                                "codec_type": "audio",
                                "duration": "4.0",
                                "channels": 1,
                                "sample_rate": "44100",
                            },
                        ],
                    }
                ),
                "stderr": "",
            },
            "invalid-audio-parameters": {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "format": {"duration": "4.0"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "duration": "4.0",
                                "disposition": {"attached_pic": 0},
                            },
                            {
                                "codec_type": "audio",
                                "duration": "4.0",
                                "channels": 0,
                                "sample_rate": "0",
                            },
                        ],
                    }
                ),
                "stderr": "",
            },
            "unknown-video-disposition": {
                "started": True,
                "timed_out": False,
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "format": {"duration": "4.0"},
                        "streams": [
                            {"codec_type": "video", "duration": "4.0"},
                            {
                                "codec_type": "audio",
                                "duration": "4.0",
                                "channels": 1,
                                "sample_rate": "44100",
                            },
                        ],
                    }
                ),
                "stderr": "",
            },
            "timeout": {
                "started": True,
                "timed_out": True,
                "returncode": None,
                "stdout": "",
                "stderr": "",
            },
        }
        for label, media_result in cases.items():
            def runner(command, **options):
                if "-show_entries" in command:
                    return media_result
                return self._trusted_process_runner(command, **options)

            with self.subTest(label=label):
                result = preflight_digital_task(
                    self.db,
                    self.request(include_default_generic=False),
                    self.root,
                    dependencies=self.dependencies(process_runner=runner),
                )
                codes = {item["code"] for item in result["blocking"]}
                self.assertIn("DIGITAL_HUMAN_MP4_INVALID", codes)
                self.assertNotIn(
                    "DIGITAL_HUMAN_MP4_READY",
                    {item["code"] for item in result["checks"]},
                )

    def test_media_probe_accepts_injected_video_and_narration_audio_fixture(self):
        result = digital_workflow._probe_digital_human_mp4(
            self.video,
            "C:/tools/ffprobe.exe",
            self.root,
            self._trusted_process_runner,
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["video_stream_count"], 1)
        self.assertEqual(result.get("audio_stream_count"), 1)

    def test_media_probe_requires_ffprobe_and_detects_source_change(self):
        ffprobe_missing = lambda name, _root: {
            "available": name != "ffprobe",
            "path": None if name == "ffprobe" else f"C:/tools/{name}.exe",
            "basis": "测试探测",
        }
        without_ffprobe = preflight_digital_task(
            self.db,
            self.request(include_default_generic=False),
            self.root,
            dependencies=self.dependencies(tool_status=ffprobe_missing),
        )
        self.assertIn(
            "DIGITAL_HUMAN_MP4_INVALID",
            {item["code"] for item in without_ffprobe["blocking"]},
        )
        self.assertNotIn(
            "DIGITAL_HUMAN_MP4_READY",
            {item["code"] for item in without_ffprobe["checks"]},
        )

        snapshots = 0

        def changing_snapshot(path):
            nonlocal snapshots
            snapshots += 1
            result = self._snapshot(path)
            if snapshots > 1:
                result["mtime_ns"] += 1
                result["quick_fingerprint"] = "changed"
            return result

        changed = preflight_digital_task(
            self.db,
            self.request(include_default_generic=False),
            self.root,
            dependencies=self.dependencies(file_snapshot=changing_snapshot),
        )
        self.assertIn(
            "DIGITAL_HUMAN_MP4_CHANGED",
            {item["code"] for item in changed["blocking"]},
        )
        self.assertNotIn(
            "DIGITAL_HUMAN_MP4_READY",
            {item["code"] for item in changed["checks"]},
        )

    def test_preflight_returns_contract_without_business_side_effects(self):
        tables = (
            "tasks", "assets", "asset_locations", "task_source_scopes",
            "task_source_snapshots", "task_stage_states", "jobs",
        )
        before = {
            table: self.db.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in tables
        }
        original = self.video.read_bytes()

        result = preflight_digital_task(
            self.db, self.request(), self.root,
            dependencies=self.dependencies(),
        )

        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["next_action"], "create_task")
        self.assertGreater(result["estimated_space_bytes"], self.video.stat().st_size)
        self.assertEqual(result["estimates"]["machine_seconds"], KNOWN_MACHINE)
        self.assertEqual(result["estimates"]["human_seconds"], KNOWN_HUMAN)
        self.assertEqual(result["queue"]["heavy_slot"], "busy")
        self.assertEqual(result["queue"]["waiting_count"], 2)
        self.assertEqual(
            [item["key"] for item in result["confirmations"]],
            ["brief", "picture", "packaging", "full_review"],
        )
        self.assertTrue(
            all("approved" not in item for item in result["confirmations"])
        )
        self.assertTrue(
            any(item["code"] == "DISK_SPACE" for item in result["checks"])
        )
        after = {
            table: self.db.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in tables
        }
        self.assertEqual(after, before)
        self.assertEqual(self.video.read_bytes(), original)
        self.assertFalse(
            (self.root / "runtime" / "workbench" / "tasks").exists()
        )

    def test_default_preflight_queue_observer_scopes_details_to_current_task(self):
        from media_catalog.workbench_worker import (
            claim_next_heavy_job,
            enqueue_job,
            fail_job,
        )

        self.db.connection.executemany(
            "INSERT INTO tasks(task_id, title) VALUES(?, ?)",
            (("other-task", "其他任务"), ("current-task", "当前任务")),
        )
        self.db.connection.commit()
        enqueue_job(
            self.db,
            job_id="render-running",
            task_id="other-task",
            job_type="render",
            payload={"secret": "other-task-payload"},
        )
        enqueue_job(
            self.db,
            job_id="scan-waiting",
            task_id="current-task",
            job_type="scan",
            payload={"library_id": "primary"},
        )
        claim_next_heavy_job(self.db, worker_id="worker-without-browser")

        dependencies = self.dependencies(
            queue_observer=digital_workflow._default_queue_observer
        )
        busy = preflight_digital_task(
            self.db,
            self.request(task_id="current-task"),
            self.root,
            dependencies=dependencies,
        )

        self.assertEqual(busy["queue"]["heavy_slot"], "busy")
        self.assertIsNone(busy["queue"]["running_job_id"])
        self.assertIsNone(busy["queue"]["running_job_type"])
        self.assertEqual(
            busy["queue"]["reason"], "其他任务正在占用重任务队列"
        )
        self.assertEqual(busy["queue"]["waiting_count"], 1)
        self.assertEqual(
            busy["queue"]["queued_jobs"],
            [
                {
                    "job_id": "scan-waiting",
                    "job_type": "scan",
                    "position": 1,
                    "reason": "等待当前重任务完成",
                }
            ],
        )
        self.assertNotIn("payload", str(busy["queue"]))
        owner_view = preflight_digital_task(
            self.db,
            self.request(task_id="other-task"),
            self.root,
            dependencies=dependencies,
        )
        self.assertEqual(
            owner_view["queue"]["running_job_id"], "render-running"
        )
        self.assertEqual(owner_view["queue"]["running_job_type"], "render")

        fail_job(
            self.db,
            "render-running",
            worker_id="worker-without-browser",
            error="test terminal",
        )
        waiting = claim_next_heavy_job(
            self.db, worker_id="worker-after-browser-closed"
        )
        self.assertEqual(waiting.job_id, "scan-waiting")
        fail_job(
            self.db,
            "scan-waiting",
            worker_id="worker-after-browser-closed",
            error="test terminal",
        )

        ready = preflight_digital_task(
            self.db,
            self.request(task_id="current-task"),
            self.root,
            dependencies=dependencies,
        )
        self.assertEqual(ready["queue"]["heavy_slot"], "available")
        self.assertIsNone(ready["queue"]["running_job_id"])
        self.assertIsNone(ready["queue"]["running_job_type"])
        self.assertEqual(ready["queue"]["waiting_count"], 0)
        self.assertEqual(ready["queue"]["queued_jobs"], [])

    def test_default_queue_keeps_global_position_when_other_task_is_ahead(self):
        from media_catalog.workbench_worker import enqueue_job

        self.db.connection.executemany(
            "INSERT INTO tasks(task_id, title) VALUES(?, ?)",
            (("other-task", "其他任务"), ("current-task", "当前任务")),
        )
        self.db.connection.commit()
        enqueue_job(
            self.db,
            job_id="other-first",
            task_id="other-task",
            job_type="render",
            payload={},
        )
        enqueue_job(
            self.db,
            job_id="current-second",
            task_id="current-task",
            job_type="scan",
            payload={},
        )

        result = preflight_digital_task(
            self.db,
            self.request(task_id="current-task"),
            self.root,
            dependencies=self.dependencies(
                queue_observer=digital_workflow._default_queue_observer
            ),
        )

        self.assertNotEqual(result["queue"]["heavy_slot"], "unknown")
        self.assertEqual(result["queue"]["waiting_count"], 1)
        self.assertEqual(
            result["queue"]["queued_jobs"],
            [
                {
                    "job_id": "current-second",
                    "job_type": "scan",
                    "position": 2,
                    "reason": "等待工作进程领取",
                }
            ],
        )
        self.assertNotIn("other-first", str(result["queue"]))

    def test_default_queue_uses_same_type_history_for_current_task_wait(self):
        from media_catalog.workbench_worker import enqueue_job

        self.db.connection.executemany(
            "INSERT INTO tasks(task_id, title) VALUES(?, ?)",
            (("other-eta", "前序任务"), ("current-eta", "当前任务")),
        )
        self.db.connection.commit()
        enqueue_job(
            self.db,
            job_id="render-sample",
            task_id="other-eta",
            job_type="render",
            payload={},
        )
        self.db.connection.execute(
            """
            UPDATE jobs SET status='completed',started_at='2026-09-22 01:00:00',
              finished_at='2026-09-22 01:01:00'
            WHERE job_id='render-sample'
            """
        )
        self.db.connection.execute(
            "DELETE FROM job_queue_entries WHERE job_id='render-sample'"
        )
        self.db.connection.commit()
        enqueue_job(
            self.db, job_id="render-ahead", task_id="other-eta",
            job_type="render", payload={},
        )
        enqueue_job(
            self.db, job_id="render-current", task_id="current-eta",
            job_type="render", payload={},
        )

        queue = digital_workflow._default_queue_observer(
            self.db, {"task_id": "current-eta"}
        )

        self.assertEqual(queue["estimated_wait_seconds"]["status"], "estimated")
        self.assertEqual(queue["estimated_wait_seconds"]["min"], 60)
        self.assertEqual(queue["estimated_wait_seconds"]["max"], 60)

    def test_preflight_identifies_current_task_unknown_provider_outcome(self):
        from media_catalog.workbench_worker import (
            claim_next_heavy_job,
            enqueue_job,
            recover_interrupted_jobs,
        )

        self.db.connection.execute(
            "INSERT INTO tasks(task_id, title) VALUES('current-task', '当前任务')"
        )
        self.db.connection.commit()
        enqueue_job(
            self.db,
            job_id="tts-uncertain",
            task_id="current-task",
            job_type="tts",
            payload={"text": "园区介绍"},
            cost_status="approved",
            provider_request_id="tts-request",
        )
        claimed_at = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
        claim_next_heavy_job(
            self.db,
            worker_id="dead-worker",
            clock=lambda: claimed_at,
            lease_seconds=1,
        )
        recover_interrupted_jobs(
            self.db,
            worker_id="replacement-worker",
            clock=lambda: claimed_at + timedelta(seconds=2),
        )

        result = preflight_digital_task(
            self.db,
            self.request(task_id="current-task"),
            self.root,
            dependencies=self.dependencies(
                queue_observer=digital_workflow._default_queue_observer
            ),
        )

        self.assertTrue(result["queue"]["manual_check_required"])
        self.assertEqual(
            result["queue"]["provider_outcome_unknown"],
            [
                {
                    "job_id": "tts-uncertain",
                    "job_type": "tts",
                    "reason": "工作进程在供应商结果确认前停止",
                }
            ],
        )

    def test_invalid_route_or_subtype_blocks_before_external_probes(self):
        calls = []
        dependencies = self.dependencies(
            file_snapshot=lambda _path: calls.append("file"),
            tool_status=lambda _name, _root: calls.append("tool"),
            disk_free_bytes=lambda _path: calls.append("disk"),
            estimate_source=lambda _request, _context: calls.append("estimate"),
            queue_observer=lambda _db, _request: calls.append("queue"),
        )
        for request in (
            self.request(route="park_promo"),
            self.request(subtype="unreleased"),
        ):
            with self.subTest(request=request):
                result = preflight_digital_task(
                    self.db, request, self.root, dependencies=dependencies,
                )
                self.assertIn(
                    "DIGITAL_ROUTE_NOT_AVAILABLE",
                    {item["code"] for item in result["blocking"]},
                )
        self.assertEqual(calls, [])

    def test_missing_inputs_offline_primary_and_illegal_generic_block(self):
        self.db.connection.execute(
            "UPDATE libraries SET online=0 WHERE library_id='primary'"
        )
        self.db.connection.commit()
        result = preflight_digital_task(
            self.db,
            self.request(
                final_script="  ",
                digital_human_mp4=str(self.root / "missing.mp4"),
                selected_generic_ids=["primary", "missing-library"],
            ),
            self.root,
            dependencies=self.dependencies(
                file_snapshot=lambda path: (_ for _ in ()).throw(
                    FileNotFoundError(str(path))
                )
            ),
        )
        codes = {item["code"] for item in result["blocking"]}
        self.assertIn("FINAL_SCRIPT_REQUIRED", codes)
        self.assertIn("DIGITAL_HUMAN_MP4_INVALID", codes)
        self.assertIn("PRIMARY_LIBRARY_OFFLINE", codes)
        self.assertIn("GENERIC_LIBRARY_INVALID", codes)
        self.assertEqual(result["next_action"], "fix_blocking_checks")

    def test_offline_generic_warns_without_blocking(self):
        self.db.connection.execute(
            "UPDATE libraries SET online=0 WHERE library_id='generic'"
        )
        self.db.connection.commit()
        result = preflight_digital_task(
            self.db, self.request(include_default_generic=False), self.root,
            dependencies=self.dependencies(),
        )
        self.assertEqual(result["blocking"], [])
        self.assertIn(
            "GENERIC_LIBRARY_OFFLINE",
            {item["code"] for item in result["warnings"]},
        )

    def test_directory_probe_failure_becomes_blocking_instead_of_crashing(self):
        result = preflight_digital_task(
            self.db,
            self.request(),
            self.root,
            dependencies=self.dependencies(
                directory_status=lambda _path: (_ for _ in ()).throw(
                    OSError("mount check failed")
                )
            ),
        )
        self.assertIn(
            "PRIMARY_LIBRARY_OFFLINE",
            {item["code"] for item in result["blocking"]},
        )
        self.assertEqual(result["next_action"], "fix_blocking_checks")

    def test_missing_tool_and_insufficient_space_block(self):
        def tool_status(name, _root):
            return {
                "available": name != "ts-node",
                "path": None,
                "basis": "本机只读工具探测",
            }

        result = preflight_digital_task(
            self.db, self.request(), self.root,
            dependencies=self.dependencies(
                tool_status=tool_status,
                disk_free_bytes=lambda _path: 1,
            ),
        )
        codes = {item["code"] for item in result["blocking"]}
        self.assertIn("REQUIRED_TOOL_MISSING", codes)
        self.assertIn("INSUFFICIENT_DISK_SPACE", codes)

    def test_tool_observation_requires_true_availability_and_executable_path(self):
        result = preflight_digital_task(
            self.db,
            self.request(include_default_generic=False),
            self.root,
            dependencies=self.dependencies(
                tool_status=lambda _name, _root: {
                    "available": "yes",
                    "path": None,
                    "basis": "untrusted observation",
                }
            ),
        )
        missing = [
            item for item in result["blocking"]
            if item["code"] == "REQUIRED_TOOL_MISSING"
        ]
        self.assertEqual(len(missing), len(digital_workflow.REQUIRED_TOOLS))

    def test_missing_estimate_evidence_stays_unavailable(self):
        def estimate_source(_request, _context):
            return {
                "machine_seconds": UNAVAILABLE,
                "human_seconds": UNAVAILABLE,
                "api_cost": {
                    "status": "unavailable",
                    "min": None,
                    "max": None,
                    "unit": None,
                    "basis": None,
                    "reason": "缺少价格、币种或用量依据",
                    "included_costs": ["AI 分镜文本模型"],
                    "excluded_costs": ["视频生成模型"],
                },
            }

        result = preflight_digital_task(
            self.db, self.request(), self.root,
            dependencies=self.dependencies(estimate_source=estimate_source),
        )
        self.assertIsNone(result["estimates"]["machine_seconds"]["min"])
        self.assertIsNone(result["estimates"]["human_seconds"]["max"])
        self.assertIsNone(result["estimates"]["api_cost"]["unit"])
        self.assertEqual(
            result["estimates"]["api_cost"]["excluded_costs"],
            ["缺失画面视频生成"],
        )

    def test_cost_scope_is_system_owned_and_separates_local_remote_steps(self):
        captured_contexts = []

        def estimate_source(_request, context):
            captured_contexts.append(context)
            return {
                "machine_seconds": UNAVAILABLE,
                "human_seconds": UNAVAILABLE,
                "api_cost": {
                    "status": "unavailable",
                    "min": None,
                    "max": None,
                    "unit": None,
                    "basis": None,
                    "reason": "缺少远程步骤价格依据",
                },
            }

        result = preflight_digital_task(
            self.db,
            self.request(
                all_selected_steps_local=True,
                selected_steps=["local_only"],
            ),
            self.root,
            dependencies=self.dependencies(estimate_source=estimate_source),
        )
        scope = result["cost_scope"]
        selected = {item["key"]: item for item in scope["selected_steps"]}
        excluded = {item["key"]: item for item in scope["excluded_steps"]}
        self.assertFalse(scope["all_selected_steps_local"])
        self.assertEqual(selected["speech_timing"]["execution"], "local")
        self.assertEqual(selected["ai_storyboard"]["execution"], "remote")
        self.assertEqual(selected["render_and_qa"]["execution"], "local")
        self.assertEqual(excluded["missing_shot_generation"]["execution"], "remote")
        self.assertNotIn("model", json.dumps(scope, ensure_ascii=False).lower())
        self.assertIn("远程", scope["summary"])
        self.assertEqual(captured_contexts[0]["cost_scope"], scope)
        self.assertEqual(
            result["estimates"]["api_cost"]["included_costs"],
            ["分镜模型 + 包装模型：AI 规划"],
        )
        self.assertEqual(
            result["estimates"]["api_cost"]["excluded_costs"],
            ["缺失画面视频生成"],
        )

        generated = preflight_digital_task(
            self.db,
            self.request(enable_video_generation=True),
            self.root,
            dependencies=self.dependencies(estimate_source=estimate_source),
        )
        generated_scope = generated["cost_scope"]
        self.assertIn(
            "missing_shot_generation",
            {item["key"] for item in generated_scope["selected_steps"]},
        )
        self.assertNotIn(
            "missing_shot_generation",
            {item["key"] for item in generated_scope["excluded_steps"]},
        )

    def test_remote_steps_reject_every_exact_zero_cost_claim(self):
        cases = (
            ("known", True, ["本地转写"]),
            ("known", False, []),
            ("estimated", True, []),
            ("estimated", False, ["AI 分镜文本模型"]),
        )
        for status, self_reported_local, included in cases:
            with self.subTest(
                status=status,
                self_reported_local=self_reported_local,
                included=included,
            ):
                result = preflight_digital_task(
                    self.db,
                    self.request(all_selected_steps_local=self_reported_local),
                    self.root,
                    dependencies=self.dependencies(
                        estimate_source=lambda _request, _context: {
                            "machine_seconds": KNOWN_MACHINE,
                            "human_seconds": KNOWN_HUMAN,
                            "api_cost": {
                                "status": status,
                                "min": 0,
                                "max": 0,
                                "unit": "CNY",
                                "basis": "来源声称本次不产生费用",
                                "reason": None,
                                "all_selected_steps_local": self_reported_local,
                                "included_costs": included,
                                "excluded_costs": [],
                            },
                        }
                    ),
                )
                cost = result["estimates"]["api_cost"]
                self.assertEqual(cost["status"], "unavailable")
                self.assertIsNone(cost["min"])
                self.assertIsNone(cost["max"])
                self.assertIsNone(cost["unit"])
                self.assertIn("远程", cost["reason"])
                self.assertEqual(
                    cost["included_costs"],
                    ["分镜模型 + 包装模型：AI 规划"],
                )
                self.assertEqual(cost["excluded_costs"], ["缺失画面视频生成"])

    def test_estimator_cannot_mutate_system_cost_scope_to_claim_local_zero(self):
        def mutate_scope(_request, context):
            context["cost_scope"]["all_selected_steps_local"] = True
            context["cost_scope"]["selected_steps"] = [
                {
                    "key": "forged",
                    "label": "伪造本地步骤",
                    "execution": "local",
                }
            ]
            return {
                "machine_seconds": KNOWN_MACHINE,
                "human_seconds": KNOWN_HUMAN,
                "api_cost": {
                    "status": "known",
                    "min": 0,
                    "max": 0,
                    "unit": "CNY",
                    "basis": "估算器篡改后声称全部本地",
                },
            }

        result = preflight_digital_task(
            self.db,
            self.request(),
            self.root,
            dependencies=self.dependencies(estimate_source=mutate_scope),
        )
        self.assertFalse(result["cost_scope"]["all_selected_steps_local"])
        self.assertIn(
            "ai_storyboard",
            {item["key"] for item in result["cost_scope"]["selected_steps"]},
        )
        cost = result["estimates"]["api_cost"]
        self.assertEqual(cost["status"], "unavailable")
        self.assertEqual(
            cost["included_costs"],
            ["分镜模型 + 包装模型：AI 规划"],
        )

    def test_remote_cost_with_real_nonnegative_range_keeps_system_scope(self):
        result = preflight_digital_task(
            self.db,
            self.request(enable_video_generation=True),
            self.root,
            dependencies=self.dependencies(
                estimate_source=lambda _request, _context: {
                    "machine_seconds": KNOWN_MACHINE,
                    "human_seconds": KNOWN_HUMAN,
                    "api_cost": {
                        "status": "estimated",
                        "min": 0.12,
                        "max": 0.48,
                        "unit": "CNY",
                        "basis": "配置价格乘以本次文本和候选画面用量区间",
                        "reason": None,
                        "included_costs": [],
                        "excluded_costs": ["来源伪造项目"],
                    },
                }
            ),
        )
        cost = result["estimates"]["api_cost"]
        self.assertEqual(cost["status"], "estimated")
        self.assertEqual((cost["min"], cost["max"], cost["unit"]), (0.12, 0.48, "CNY"))
        self.assertEqual(
            cost["included_costs"],
            ["分镜模型 + 包装模型：AI 规划", "缺失画面视频生成"],
        )
        self.assertEqual(cost["excluded_costs"], [])

    def test_malformed_estimate_item_does_not_replace_valid_siblings(self):
        result = preflight_digital_task(
            self.db,
            self.request(),
            self.root,
            dependencies=self.dependencies(
                estimate_source=lambda _request, _context: {
                    "machine_seconds": KNOWN_MACHINE,
                    "human_seconds": {
                        "status": "estimated",
                        "min": 60,
                        "max": 30,
                        "unit": "seconds",
                        "basis": "范围倒置",
                    },
                    "api_cost": {
                        "status": "estimated",
                        "min": 0.08,
                        "max": 0.2,
                        "unit": "CNY",
                        "basis": "已配置文本用量和价格区间",
                    },
                }
            ),
        )
        self.assertEqual(result["estimates"]["machine_seconds"], KNOWN_MACHINE)
        self.assertEqual(result["estimates"]["human_seconds"]["status"], "unavailable")
        self.assertEqual(result["estimates"]["api_cost"]["status"], "estimated")

    def test_one_estimate_item_read_error_does_not_replace_valid_siblings(self):
        class OneBrokenItem(dict):
            def get(self, key, default=None):
                if key == "human_seconds":
                    raise LookupError("human estimate row is corrupt")
                return super().get(key, default)

        result = preflight_digital_task(
            self.db,
            self.request(),
            self.root,
            dependencies=self.dependencies(
                estimate_source=lambda _request, _context: OneBrokenItem(
                    {
                        "machine_seconds": KNOWN_MACHINE,
                        "api_cost": {
                            "status": "estimated",
                            "min": 0.08,
                            "max": 0.2,
                            "unit": "CNY",
                            "basis": "已配置文本用量和价格区间",
                        },
                    }
                )
            ),
        )
        self.assertEqual(result["estimates"]["machine_seconds"], KNOWN_MACHINE)
        self.assertEqual(result["estimates"]["human_seconds"]["status"], "unavailable")
        self.assertIn("human", result["estimates"]["human_seconds"]["reason"])
        self.assertEqual(result["estimates"]["api_cost"]["status"], "estimated")

    def test_estimated_exact_zero_and_missing_explanations_fail_closed(self):
        result = preflight_digital_task(
            self.db,
            self.request(),
            self.root,
            dependencies=self.dependencies(
                estimate_source=lambda _request, _context: {
                    "machine_seconds": {
                        "status": "estimated",
                        "min": 0,
                        "max": 0,
                        "unit": "seconds",
                        "basis": "样本范围",
                    },
                    "human_seconds": {
                        "status": "unavailable",
                        "min": None,
                        "max": None,
                        "unit": "seconds",
                        "basis": None,
                        "reason": "",
                    },
                    "api_cost": {
                        "status": "unavailable",
                        "min": None,
                        "max": None,
                        "unit": None,
                        "basis": None,
                        "reason": "",
                    },
                }
            ),
        )
        self.assertEqual(result["estimates"]["machine_seconds"]["status"], "unavailable")
        self.assertIn("精确零", result["estimates"]["machine_seconds"]["reason"])
        self.assertTrue(result["estimates"]["human_seconds"]["reason"])
        self.assertTrue(result["estimates"]["api_cost"]["reason"])

    def test_sqlite_and_regular_observer_failures_degrade_without_crashing(self):
        class LockedConnection:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("database is locked")

        class LockedDatabase:
            connection = LockedConnection()

        def locked_queue(_db, request):
            return digital_workflow._default_queue_observer(LockedDatabase(), request)

        for label, estimate_error, queue_observer in (
            (
                "sqlite",
                sqlite3.OperationalError("database is locked"),
                locked_queue,
            ),
            (
                "os",
                OSError("estimator I/O failed"),
                lambda _db, _request: (_ for _ in ()).throw(
                    OSError("queue I/O failed")
                ),
            ),
            (
                "ordinary",
                LookupError("ordinary estimator failure"),
                lambda _db, _request: (_ for _ in ()).throw(
                    LookupError("ordinary queue failure")
                ),
            ),
        ):
            with self.subTest(label=label):
                failed = preflight_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(
                        estimate_source=lambda _request, _context, error=estimate_error: (
                            _ for _ in ()
                        ).throw(error),
                        queue_observer=queue_observer,
                    ),
                )
                self.assertEqual(
                    {key: value["status"] for key, value in failed["estimates"].items()},
                    {
                        "machine_seconds": "unavailable",
                        "human_seconds": "unavailable",
                        "api_cost": "unavailable",
                    },
                )
                self.assertTrue(failed["estimates"]["machine_seconds"]["reason"])
                self.assertTrue(failed["estimates"]["human_seconds"]["reason"])
                self.assertTrue(failed["estimates"]["api_cost"]["reason"])
                self.assertEqual(failed["queue"]["heavy_slot"], "unknown")
                self.assertIsNone(failed["queue"]["running_job_id"])
                self.assertIsNone(failed["queue"]["waiting_count"])
                self.assertEqual(
                    failed["queue"]["estimated_wait_seconds"]["status"],
                    "unavailable",
                )
                self.assertEqual(failed["queue"]["observed_at"], "2026-09-06T06:00:00Z")

    def test_malformed_queue_and_wait_estimate_fail_closed(self):
        cases = (
            {
                "heavy_slot": "available",
                "running_job_id": None,
                "waiting_count": -1,
                "observed_at": "2026-09-06T05:59:00Z",
                "estimated_wait_seconds": KNOWN_MACHINE,
            },
            {
                "heavy_slot": "unknown",
                "running_job_id": "must-not-leak",
                "waiting_count": None,
                "observed_at": "2026-09-06T05:59:00Z",
                "estimated_wait_seconds": UNAVAILABLE,
                "reason": "observer unavailable",
            },
            {
                "heavy_slot": "available",
                "running_job_id": None,
                "running_job_type": None,
                "waiting_count": 1,
                "queued_jobs": ["not-a-job-object"],
                "observed_at": "2026-09-06T05:59:00Z",
                "estimated_wait_seconds": UNAVAILABLE,
            },
        )
        for raw in cases:
            with self.subTest(raw=raw):
                result = preflight_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(
                        queue_observer=lambda _db, _request, value=raw: value
                    ),
                )
                self.assertEqual(result["queue"]["heavy_slot"], "unknown")
                self.assertIsNone(result["queue"]["running_job_id"])
                self.assertIsNone(result["queue"]["waiting_count"])
                self.assertEqual(result["queue"]["observed_at"], "2026-09-06T06:00:00Z")

        malformed_waits = (
            {
                "status": "estimated",
                "min": 0,
                "max": 0,
                "unit": "seconds",
                "basis": "不应将估算精确零当成确定值",
            },
            {
                "status": "estimated",
                "min": 90,
                "max": 30,
                "unit": "seconds",
                "basis": "范围倒置",
            },
            {
                "status": "known",
                "min": 0,
                "max": 0,
                "unit": "seconds",
                "basis": "",
            },
            {
                "status": "unavailable",
                "min": None,
                "max": None,
                "unit": "seconds",
                "basis": None,
                "reason": "",
            },
        )
        for malformed_wait in malformed_waits:
            with self.subTest(malformed_wait=malformed_wait):
                queue = {
                    "heavy_slot": "available",
                    "running_job_id": None,
                    "waiting_count": 0,
                    "observed_at": "2026-09-06T05:59:00Z",
                    "estimated_wait_seconds": malformed_wait,
                }
                result = preflight_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(
                        queue_observer=lambda _db, _request, value=queue: value
                    ),
                )
                self.assertEqual(result["queue"]["heavy_slot"], "available")
                wait = result["queue"]["estimated_wait_seconds"]
                self.assertEqual(wait["status"], "unavailable")
                self.assertIsNone(wait["min"])
                self.assertIsNone(wait["max"])
                self.assertTrue(wait["reason"])

    def test_queue_empty_and_failure_are_honest(self):
        empty = lambda _db, _request: {
            "heavy_slot": "available",
            "running_job_id": None,
            "waiting_count": 0,
            "observed_at": "2026-09-06T06:00:00Z",
            "estimated_wait_seconds": {
                "status": "known",
                "min": 0,
                "max": 0,
                "unit": "seconds",
                "basis": "SQLite 中无运行或排队的重任务",
                "reason": None,
            },
        }
        available = preflight_digital_task(
            self.db, self.request(), self.root,
            dependencies=self.dependencies(queue_observer=empty),
        )
        self.assertEqual(available["queue"]["heavy_slot"], "available")
        self.assertEqual(
            available["queue"]["estimated_wait_seconds"]["max"], 0
        )

        failed = preflight_digital_task(
            self.db, self.request(), self.root,
            dependencies=self.dependencies(
                queue_observer=lambda _db, _request: (_ for _ in ()).throw(
                    RuntimeError("queue unavailable")
                )
            ),
        )
        self.assertEqual(failed["queue"]["heavy_slot"], "unknown")
        self.assertIsNone(failed["queue"]["waiting_count"])
        self.assertEqual(
            failed["queue"]["estimated_wait_seconds"]["status"],
            "unavailable",
        )
        self.assertIn("queue unavailable", failed["queue"]["reason"])


class DigitalTaskCreationTests(_DigitalWorkflowFixture):
    def _configure_text_profile(self, *, available=True):
        create_provider_account(
            self.db,
            account_id="gateway",
            name="New API",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/gateway",
            settings={"channel_id": 7},
        )
        create_model_profile(
            self.db,
            profile_id="text-profile",
            capability="general_text",
            display_name="数字人文案",
            account_id="gateway",
            model="deepseek-v4",
            settings={"protocol": "openai_chat"},
            is_default=True,
        )
        mark_profile_check(
            self.db,
            "text-profile",
            available=available,
            checked_at=datetime.now(timezone.utc),
        )
        create_model_profile(
            self.db,
            profile_id="video-profile",
            capability="video_generation",
            display_name="缺口画面模型",
            account_id="gateway",
            model="seedance-frozen",
            settings={"protocol": "doubao_video"},
        )
        mark_profile_check(
            self.db,
            "video-profile",
            available=True,
            checked_at=datetime.now(timezone.utc),
        )

    def test_create_freezes_selected_general_text_profile(self):
        self._configure_text_profile()
        with patch("media_catalog.task_sources.scan_library"):
            created = create_digital_task(
                self.db,
                self.request(
                    model_profile_ids={
                        "general_text": "text-profile",
                        "video_generation": "video-profile",
                    }
                ),
                self.root,
                dependencies=self.dependencies(),
            )
        binding = get_task_profile_binding(
            self.db, created["task_id"], "general_text"
        )
        self.assertEqual(binding["model"], "deepseek-v4")
        self.assertEqual(binding["channelId"], 7)
        video_binding = get_task_profile_binding(
            self.db, created["task_id"], "video_generation"
        )
        self.assertEqual(video_binding["model"], "seedance-frozen")

    def test_create_freezes_one_planning_and_optional_broll_profile(self):
        create_provider_account(
            self.db,
            account_id="three-role-gateway",
            name="三类模型账号",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            credential_ref="cutroom/provider/three-role-gateway",
            settings={"preset": "openai_compatible", "protocol": "openai"},
        )
        for profile_id, capability, model, protocol in (
            ("planning-profile", "planning", "glm-5.3", "openai"),
            ("broll-profile", "broll", "seedance-test", "doubao_video"),
        ):
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=profile_id,
                account_id="three-role-gateway",
                model=model,
                settings={"protocol": protocol},
            )
            mark_profile_check(
                self.db,
                profile_id,
                available=True,
                checked_at=datetime.now(timezone.utc),
            )

        with patch("media_catalog.task_sources.scan_library"):
            created = create_digital_task(
                self.db,
                self.request(
                    model_profile_ids={
                        "planning": "planning-profile",
                        "broll": "broll-profile",
                    }
                ),
                self.root,
                dependencies=self.dependencies(),
            )

        self.assertEqual(
            get_task_profile_binding(self.db, created["task_id"], "planning")["model"],
            "glm-5.3",
        )
        self.assertEqual(
            get_task_profile_binding(self.db, created["task_id"], "broll")["model"],
            "seedance-test",
        )
        self.assertIsNone(
            get_task_profile_binding(self.db, created["task_id"], "storyboard")
        )

    def test_create_freezes_independent_copy_storyboard_and_packaging_profiles(self):
        create_provider_account(
            self.db,
            account_id="split-gateway",
            name="拆分 New API",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/split-gateway",
            settings={"channel_id": 17},
        )
        selections = {}
        for capability in ("copywriting", "storyboard", "packaging"):
            profile_id = f"digital-{capability}"
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=profile_id,
                account_id="split-gateway",
                model=f"digital-model-{capability}",
                settings={"protocol": "openai_chat"},
            )
            mark_profile_check(
                self.db,
                profile_id,
                available=True,
                checked_at=datetime.now(timezone.utc),
            )
            selections[capability] = profile_id

        with patch("media_catalog.task_sources.scan_library"):
            created = create_digital_task(
                self.db,
                self.request(model_profile_ids=selections),
                self.root,
                dependencies=self.dependencies(),
            )

        for capability in ("copywriting", "storyboard", "packaging"):
            binding = get_task_profile_binding(
                self.db, created["task_id"], capability
            )
            self.assertEqual(binding["model"], f"digital-model-{capability}")

    def test_digital_creation_does_not_require_optional_copywriting_profile(self):
        create_provider_account(
            self.db,
            account_id="required-gateway",
            name="必需模型 New API",
            provider="new_api",
            base_url="http://127.0.0.1:3000",
            credential_ref="cutroom/provider/required-gateway",
            settings={"channel_id": 19},
        )
        selections = {}
        for capability in ("storyboard", "packaging"):
            profile_id = f"required-{capability}"
            create_model_profile(
                self.db,
                profile_id=profile_id,
                capability=capability,
                display_name=profile_id,
                account_id="required-gateway",
                model=f"required-model-{capability}",
                settings={"protocol": "openai_chat"},
            )
            mark_profile_check(
                self.db,
                profile_id,
                available=True,
                checked_at=datetime.now(timezone.utc),
            )
            selections[capability] = profile_id

        with patch("media_catalog.task_sources.scan_library"):
            created = create_digital_task(
                self.db,
                self.request(model_profile_ids=selections),
                self.root,
                dependencies=self.dependencies(),
            )
        self.assertIsNone(
            get_task_profile_binding(self.db, created["task_id"], "copywriting")
        )

    def test_preflight_blocks_selected_unavailable_general_text_profile(self):
        self._configure_text_profile(available=False)
        result = preflight_digital_task(
            self.db,
            self.request(model_profile_ids={"general_text": "text-profile"}),
            self.root,
            dependencies=self.dependencies(),
        )
        self.assertIn(
            "MODEL_PROFILE_UNAVAILABLE",
            {item["code"] for item in result["blocking"]},
        )

    def test_agent_mode_ignores_text_profile_but_freezes_external_video_profile(self):
        self._configure_text_profile(available=False)
        request = self.request(
            orchestration_mode="agent",
            model_profile_ids={
                "general_text": "text-profile",
                "video_generation": "video-profile",
            },
        )
        preflight = preflight_digital_task(
            self.db,
            request,
            self.root,
            dependencies=self.dependencies(),
        )
        self.assertNotIn(
            "MODEL_PROFILE_UNAVAILABLE",
            {item["code"] for item in preflight["blocking"]},
        )

        with patch("media_catalog.task_sources.scan_library"):
            created = create_digital_task(
                self.db,
                request,
                self.root,
                dependencies=self.dependencies(),
            )

        self.assertEqual(created["task"]["orchestration_mode"], "agent")
        self.assertIsNone(
            get_task_profile_binding(self.db, created["task_id"], "general_text")
        )
        self.assertEqual(
            get_task_profile_binding(
                self.db, created["task_id"], "video_generation"
            )["model"],
            "seedance-frozen",
        )

    def _fail_after_binding_with_closed_database(self, request=None):
        from media_catalog.task_sources import set_task_sources as real_set_sources

        def close_then_fail(*args, **kwargs):
            real_set_sources(*args, **kwargs)
            self.db.close()
            raise RuntimeError("original creation failure")

        with patch(
            "media_catalog.digital_workflow.set_task_sources",
            side_effect=close_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "original creation failure"):
                create_digital_task(
                    self.db,
                    request or self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

    def test_task_id_collision_does_not_overwrite_existing_task(self):
        existing_id = "task_" + "a" * 32
        self.db.connection.execute(
            """
            INSERT INTO tasks(task_id, title, task_type, status)
            VALUES(?, '保留的任务', 'digital_human', 'draft')
            """,
            (existing_id,),
        )
        self.db.connection.commit()
        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            side_effect=["a" * 32, "b" * 32],
        ):
            result = create_digital_task(
                self.db,
                self.request(),
                self.root,
                dependencies=self.dependencies(),
            )

        self.assertNotEqual(result["task_id"], existing_id)
        existing = self.db.connection.execute(
            "SELECT title FROM tasks WHERE task_id=?", (existing_id,)
        ).fetchone()
        self.assertEqual(existing["title"], "保留的任务")

    def test_create_persists_inputs_source_scope_and_twelve_stage_workflow(self):
        original_bytes = self.video.read_bytes()
        original_mtime = self.video.stat().st_mtime_ns

        with patch("media_catalog.task_sources.scan_library") as scan:
            result = create_digital_task(
                self.db,
                self.request(),
                self.root,
                dependencies=self.dependencies(),
            )

        task_id = result["task_id"]
        self.assertRegex(task_id, r"^task_[0-9a-f]{32}$")
        self.assertNotIn("新疆", task_id)
        input_dir = Path(result["input_dir"])
        self.assertEqual(
            (input_dir / "final-script.txt").read_text(encoding="utf-8"),
            self.request()["final_script"],
        )
        source_record_path = input_dir / "source.json"
        source_record = json.loads(source_record_path.read_text(encoding="utf-8"))
        self.assertTrue(source_record["read_only"])
        self.assertEqual(source_record["path"], str(self.video.resolve()))
        self.assertEqual(source_record["asset_id"], result["source_asset_id"])
        self.assertFalse(source_record_path.stat().st_mode & stat.S_IWUSR)
        self.assertEqual(self.video.read_bytes(), original_bytes)
        self.assertEqual(self.video.stat().st_mtime_ns, original_mtime)
        self.assertEqual(list(input_dir.glob("*.mp4")), [])

        task = self.db.connection.execute(
            "SELECT task_type, brief_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        self.assertEqual(task["task_type"], "digital_human")
        brief = json.loads(task["brief_json"])
        self.assertEqual(brief["subtype"], "zhaoshang")
        self.assertEqual(
            brief["digital_human_source"]["asset_id"],
            result["source_asset_id"],
        )
        digital_assets = self.db.connection.execute(
            """
            SELECT asset_id, role FROM task_assets
            WHERE task_id=? AND role='digital_human'
            """,
            (task_id,),
        ).fetchall()
        self.assertEqual(
            [dict(row) for row in digital_assets],
            [{"asset_id": result["source_asset_id"], "role": "digital_human"}],
        )
        self.assertNotIn("creation_recovery", brief)
        self.assertFalse(
            (input_dir.parent / digital_workflow._OWNER_MARKER_NAME).exists()
        )
        source = get_task_sources(self.db, task_id)
        self.assertEqual(source["primary"]["library_id"], "primary")
        self.assertEqual(
            [item["library_id"] for item in source["selected_generic"]],
            ["generic"],
        )
        self.assertEqual(
            [item["library_id"] for item in source["default_generic"]],
            ["default-generic"],
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM task_source_snapshots WHERE task_id=?",
                (task_id,),
            ).fetchone()[0],
            3,
        )
        stages = self.db.connection.execute(
            """
            SELECT stage_key, status FROM task_stage_states
            WHERE task_id=? ORDER BY rowid
            """,
            (task_id,),
        ).fetchall()
        self.assertEqual([row["stage_key"] for row in stages], list(WORKFLOW_STAGES))
        self.assertEqual(stages[0]["status"], "completed")
        self.assertEqual(stages[1]["status"], "ready")
        self.assertTrue(
            all(row["status"] == "not_started" for row in stages[2:])
        )
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            0,
        )
        recovery_root = self.root / "runtime" / "workbench" / "recovery"
        self.assertEqual(
            list(recovery_root.glob("*.json")) if recovery_root.exists() else [], []
        )
        scan.assert_not_called()

    def test_created_multiline_script_digest_matches_exact_file_bytes(self):
        request = self.request(final_script="第一行\n第二行\n")

        created = create_digital_task(
            self.db,
            request,
            self.root,
            dependencies=self.dependencies(),
        )

        script_path = Path(created["input_dir"]) / "final-script.txt"
        row = self.db.connection.execute(
            "SELECT brief_json FROM tasks WHERE task_id=?",
            (created["task_id"],),
        ).fetchone()
        brief = json.loads(row["brief_json"])
        self.assertEqual(
            brief["final_script"]["sha256"],
            hashlib.sha256(script_path.read_bytes()).hexdigest(),
        )

    def test_same_unchanged_source_asset_is_reused_across_tasks(self):
        first = create_digital_task(
            self.db,
            self.request(title="任务一"),
            self.root,
            dependencies=self.dependencies(),
        )
        second = create_digital_task(
            self.db,
            self.request(title="任务二"),
            self.root,
            dependencies=self.dependencies(),
        )
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["source_asset_id"], second["source_asset_id"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM assets"
            ).fetchone()[0],
            1,
        )

    def test_blocked_preflight_creates_no_task_or_directory(self):
        with self.assertRaises(DigitalTaskPreflightError) as captured:
            create_digital_task(
                self.db,
                self.request(final_script=" "),
                self.root,
                dependencies=self.dependencies(),
            )
        self.assertIn(
            "FINAL_SCRIPT_REQUIRED",
            {item["code"] for item in captured.exception.preflight["blocking"]},
        )
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            0,
        )
        self.assertFalse(
            (self.root / "runtime" / "workbench" / "tasks").exists()
        )

    def test_source_change_during_creation_is_compensated(self):
        calls = 0

        def changing_snapshot(path):
            nonlocal calls
            calls += 1
            snapshot = self._snapshot(path)
            if calls > 1:
                snapshot["quick_fingerprint"] = "changed-fingerprint"
            return snapshot

        with self.assertRaises(DigitalTaskPreflightError):
            create_digital_task(
                self.db,
                self.request(),
                self.root,
                dependencies=self.dependencies(file_snapshot=changing_snapshot),
            )
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            0,
        )
        task_root = self.root / "runtime" / "workbench" / "tasks"
        self.assertEqual(list(task_root.glob("*")) if task_root.exists() else [], [])

    def test_failure_after_source_binding_removes_task_snapshots_and_new_asset(self):
        from media_catalog.task_sources import set_task_sources as real_set_sources

        def fail_after_binding(*args, **kwargs):
            real_set_sources(*args, **kwargs)
            raise RuntimeError("injected after source binding")

        with patch(
            "media_catalog.digital_workflow.set_task_sources",
            side_effect=fail_after_binding,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        for table in (
            "tasks",
            "assets",
            "asset_locations",
            "task_source_scopes",
            "task_source_snapshots",
            "task_stage_states",
        ):
            self.assertEqual(
                self.db.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0],
                0,
                table,
            )
        task_root = self.root / "runtime" / "workbench" / "tasks"
        self.assertEqual(list(task_root.glob("*")) if task_root.exists() else [], [])

    def test_atomic_rename_failure_is_fully_compensated(self):
        with patch(
            "media_catalog.digital_workflow._atomic_rename",
            side_effect=OSError("rename failed"),
        ):
            with self.assertRaisesRegex(OSError, "rename failed"):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            0,
        )
        task_root = self.root / "runtime" / "workbench" / "tasks"
        self.assertEqual(list(task_root.glob("*")) if task_root.exists() else [], [])

    def test_rename_collision_never_deletes_external_final_directory(self):
        task_id = "task_" + "c" * 32
        tasks_root = self.root / "runtime" / "workbench" / "tasks"
        external_final = tasks_root / task_id
        sentinel = external_final / "external-sentinel.txt"

        def destination_appears(_staging, final):
            final.mkdir(parents=True)
            sentinel.write_text("belongs to another process", encoding="utf-8")
            raise FileExistsError("destination appeared concurrently")

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="c" * 32,
        ), patch(
            "media_catalog.digital_workflow._atomic_rename",
            side_effect=destination_appears,
        ):
            with self.assertRaisesRegex(
                FileExistsError, "destination appeared concurrently"
            ):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        self.assertEqual(
            sentinel.read_text(encoding="utf-8"), "belongs to another process"
        )
        self.assertFalse((tasks_root / f".{task_id}.staging").exists())
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            0,
        )
        recovery_root = self.root / "runtime" / "workbench" / "recovery"
        self.assertEqual(
            list(recovery_root.glob("*.json")) if recovery_root.exists() else [], []
        )

    def test_staging_collision_never_deletes_external_staging_directory(self):
        task_id = "task_" + "4" * 32
        tasks_root = self.root / "runtime" / "workbench" / "tasks"
        external_staging = tasks_root / f".{task_id}.staging"
        sentinel = external_staging / "external-sentinel.txt"
        real_atomic_write = digital_workflow._atomic_write_json
        writes = 0

        def create_staging_after_initial_journal(path, payload, **kwargs):
            nonlocal writes
            writes += 1
            real_atomic_write(path, payload, **kwargs)
            if writes == 1:
                external_staging.mkdir(parents=True)
                sentinel.write_text("external staging", encoding="utf-8")

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="4" * 32,
        ), patch(
            "media_catalog.digital_workflow._atomic_write_json",
            side_effect=create_staging_after_initial_journal,
        ):
            with self.assertRaises(FileExistsError):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "external staging")
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            0,
        )

    def test_closed_database_does_not_mask_original_error_and_keeps_recovery_journal(self):
        task_id = "task_" + "d" * 32
        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="d" * 32,
        ):
            self._fail_after_binding_with_closed_database()

        tasks_root = self.root / "runtime" / "workbench" / "tasks"
        self.assertTrue((tasks_root / f".{task_id}.staging").exists())
        recovery_files = list(
            (self.root / "runtime" / "workbench" / "recovery").glob("*.json")
        )
        self.assertEqual(len(recovery_files), 1)
        journal = digital_workflow._read_recovery_journal(recovery_files[0])
        self.assertEqual(journal["task_id"], task_id)
        self.assertEqual(journal["error"]["type"], "RuntimeError")
        self.assertEqual(journal["error"]["message"], "original creation failure")
        self.assertEqual(journal["recovery"]["database"]["status"], "deferred")
        self.assertEqual(journal["recovery"]["staging"]["status"], "not_attempted")
        self.assertEqual(journal["recovery"]["validation"]["status"], "failed")

    def test_file_cleanup_failure_preserves_database_for_retry_and_original_error(self):
        from media_catalog.task_sources import set_task_sources as real_set_sources

        def fail_after_binding(*args, **kwargs):
            real_set_sources(*args, **kwargs)
            raise RuntimeError("original business error")

        with patch(
            "media_catalog.digital_workflow.set_task_sources",
            side_effect=fail_after_binding,
        ), patch(
            "media_catalog.digital_workflow._remove_tree",
            side_effect=OSError("filesystem cleanup denied"),
        ):
            with self.assertRaisesRegex(RuntimeError, "original business error"):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            1,
        )
        recovery_path = next(
            (self.root / "runtime" / "workbench" / "recovery").glob("*.json")
        )
        journal = digital_workflow._read_recovery_journal(recovery_path)
        self.assertEqual(journal["recovery"]["database"]["status"], "deferred")
        self.assertEqual(journal["recovery"]["staging"]["status"], "failed")
        result = recover_incomplete_digital_tasks(self.db, self.root)
        self.assertEqual(len(result["recovered"]), 1)
        self.assertFalse(recovery_path.exists())
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            0,
        )

    def test_recovery_replays_database_cleanup_and_is_idempotent(self):
        task_id = "task_" + "e" * 32
        db_path = self.db.path
        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="e" * 32,
        ):
            self._fail_after_binding_with_closed_database()

        reopened = CatalogDatabase.open(db_path)
        try:
            result = recover_incomplete_digital_tasks(reopened, self.root)
            self.assertEqual(
                [item["task_id"] for item in result["recovered"]], [task_id]
            )
            self.assertEqual(result["failed"], [])
            for table in (
                "tasks",
                "assets",
                "asset_locations",
                "task_source_scopes",
                "task_source_snapshots",
                "task_stage_states",
            ):
                self.assertEqual(
                    reopened.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                    table,
                )
            again = recover_incomplete_digital_tasks(reopened, self.root)
            self.assertEqual(again["processed"], 0)
            self.assertEqual(again["recovered"], [])
            self.assertEqual(again["failed"], [])
        finally:
            reopened.close()

    def test_recovery_claims_final_only_when_rename_moved_ownership_marker(self):
        task_id = "task_" + "3" * 32
        tasks_root = self.root / "runtime" / "workbench" / "tasks"

        def rename_then_interrupt(staging, final):
            staging.rename(final)
            raise KeyboardInterrupt("simulated process interruption")

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="3" * 32,
        ), patch(
            "media_catalog.digital_workflow._atomic_rename",
            side_effect=rename_then_interrupt,
        ), patch("media_catalog.digital_workflow._record_failed_cleanup"):
            with self.assertRaisesRegex(
                KeyboardInterrupt, "simulated process interruption"
            ):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        final = tasks_root / task_id
        self.assertTrue(final.is_dir())
        recovery_path = (
            self.root
            / "runtime"
            / "workbench"
            / "recovery"
            / f"create-{task_id}.json"
        )
        journal = digital_workflow._read_recovery_journal(recovery_path)
        self.assertFalse(journal["paths"]["final_owned"])
        self.assertEqual(journal["step"], "rename_pending")

        result = recover_incomplete_digital_tasks(self.db, self.root)
        self.assertEqual(
            [item["task_id"] for item in result["recovered"]], [task_id]
        )
        self.assertFalse(final.exists())
        self.assertFalse(recovery_path.exists())
        self.assertIsNone(
            self.db.connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        )

    def test_completed_journal_recovery_finalizes_without_rolling_back_task(self):
        task_id = "task_" + "9" * 32
        real_atomic_write = digital_workflow._atomic_write_json

        def interrupt_after_completed_write(path, payload, **kwargs):
            real_atomic_write(path, payload, **kwargs)
            if payload.get("step") == "completed":
                raise KeyboardInterrupt("simulated hard stop after completed journal")

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="9" * 32,
        ), patch(
            "media_catalog.digital_workflow._atomic_write_json",
            side_effect=interrupt_after_completed_write,
        ), patch("media_catalog.digital_workflow._record_failed_cleanup"):
            with self.assertRaisesRegex(KeyboardInterrupt, "simulated hard stop"):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        final = self.root / "runtime" / "workbench" / "tasks" / task_id
        journal = (
            self.root
            / "runtime"
            / "workbench"
            / "recovery"
            / f"create-{task_id}.json"
        )
        before_counts = {
            table: self.db.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "tasks",
                "assets",
                "asset_locations",
                "task_source_scopes",
                "task_source_snapshots",
                "task_stage_states",
            )
        }
        self.assertTrue(final.is_dir())
        self.assertEqual(
            digital_workflow._read_recovery_journal(journal)["step"],
            "completed",
        )

        result = recover_incomplete_digital_tasks(self.db, self.root)

        self.assertEqual(
            [item["task_id"] for item in result["recovered"]], [task_id]
        )
        self.assertEqual(result["failed"], [])
        self.assertTrue(final.is_dir())
        self.assertFalse(journal.exists())
        self.assertFalse((final / digital_workflow._OWNER_MARKER_NAME).exists())
        after_counts = {
            table: self.db.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in before_counts
        }
        self.assertEqual(after_counts, before_counts)
        task = self.db.connection.execute(
            "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        self.assertIsNotNone(task)
        self.assertNotIn("creation_recovery", json.loads(task["brief_json"]))

    def test_stale_completed_journal_cannot_authorize_successful_task_deletion(self):
        captured_completed = None
        real_atomic_write = digital_workflow._atomic_write_json

        def capture_completed(path, payload, **kwargs):
            nonlocal captured_completed
            real_atomic_write(path, payload, **kwargs)
            if payload.get("step") == "completed":
                captured_completed = json.loads(json.dumps(payload))

        with patch(
            "media_catalog.digital_workflow._atomic_write_json",
            side_effect=capture_completed,
        ):
            created = create_digital_task(
                self.db,
                self.request(),
                self.root,
                dependencies=self.dependencies(),
            )

        task_id = created["task_id"]
        final = self.root / "runtime" / "workbench" / "tasks" / task_id
        journal = (
            self.root
            / "runtime"
            / "workbench"
            / "recovery"
            / f"create-{task_id}.json"
        )
        journal.write_text(json.dumps(captured_completed), encoding="utf-8")

        result = recover_incomplete_digital_tasks(self.db, self.root)

        self.assertEqual(result["recovered"], [])
        self.assertEqual([item["task_id"] for item in result["failed"]], [task_id])
        self.assertTrue(final.is_dir())
        self.assertIsNotNone(
            self.db.connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        )
        self.assertTrue(journal.exists())

    def test_recovery_marker_mismatch_changes_neither_database_nor_files(self):
        task_id = "task_" + "8" * 32

        def rename_then_interrupt(staging, final):
            staging.rename(final)
            raise KeyboardInterrupt("simulated interruption")

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="8" * 32,
        ), patch(
            "media_catalog.digital_workflow._atomic_rename",
            side_effect=rename_then_interrupt,
        ), patch("media_catalog.digital_workflow._record_failed_cleanup"):
            with self.assertRaises(KeyboardInterrupt):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        final = self.root / "runtime" / "workbench" / "tasks" / task_id
        journal = (
            self.root
            / "runtime"
            / "workbench"
            / "recovery"
            / f"create-{task_id}.json"
        )
        marker = final / digital_workflow._OWNER_MARKER_NAME
        marker.write_text(
            json.dumps({"task_id": task_id, "owner_token": "0" * 32}),
            encoding="utf-8",
        )
        before = final.joinpath("inputs", "final-script.txt").read_bytes()

        result = recover_incomplete_digital_tasks(self.db, self.root)

        self.assertEqual(result["recovered"], [])
        self.assertEqual([item["task_id"] for item in result["failed"]], [task_id])
        self.assertIsNotNone(
            self.db.connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        )
        self.assertEqual(
            final.joinpath("inputs", "final-script.txt").read_bytes(), before
        )
        self.assertTrue(journal.exists())

    def test_recovery_unknown_step_changes_neither_database_nor_files(self):
        task_id = "task_" + "6" * 32

        def rename_then_interrupt(staging, final):
            staging.rename(final)
            raise KeyboardInterrupt("simulated interruption")

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="6" * 32,
        ), patch(
            "media_catalog.digital_workflow._atomic_rename",
            side_effect=rename_then_interrupt,
        ), patch("media_catalog.digital_workflow._record_failed_cleanup"):
            with self.assertRaises(KeyboardInterrupt):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        final = self.root / "runtime" / "workbench" / "tasks" / task_id
        journal = (
            self.root
            / "runtime"
            / "workbench"
            / "recovery"
            / f"create-{task_id}.json"
        )
        state = digital_workflow._read_recovery_journal(journal)
        state["step"] = "unknown-future-step"
        journal.write_text(json.dumps(state), encoding="utf-8")
        before = final.joinpath("inputs", "final-script.txt").read_bytes()

        result = recover_incomplete_digital_tasks(self.db, self.root)

        self.assertEqual(result["recovered"], [])
        self.assertEqual([item["task_id"] for item in result["failed"]], [task_id])
        self.assertIsNotNone(
            self.db.connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        )
        self.assertEqual(
            final.joinpath("inputs", "final-script.txt").read_bytes(), before
        )
        self.assertTrue(journal.exists())

    def test_recovery_tampered_created_flags_change_neither_database_nor_files(self):
        task_id = "task_" + "5" * 32

        def rename_then_interrupt(staging, final):
            staging.rename(final)
            raise KeyboardInterrupt("simulated interruption")

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="5" * 32,
        ), patch(
            "media_catalog.digital_workflow._atomic_rename",
            side_effect=rename_then_interrupt,
        ), patch("media_catalog.digital_workflow._record_failed_cleanup"):
            with self.assertRaises(KeyboardInterrupt):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        final = self.root / "runtime" / "workbench" / "tasks" / task_id
        journal = (
            self.root
            / "runtime"
            / "workbench"
            / "recovery"
            / f"create-{task_id}.json"
        )
        state = digital_workflow._read_recovery_journal(journal)
        state["source"]["location_created"] = not state["source"][
            "location_created"
        ]
        journal.write_text(json.dumps(state), encoding="utf-8")

        result = recover_incomplete_digital_tasks(self.db, self.root)

        self.assertEqual(result["recovered"], [])
        self.assertEqual([item["task_id"] for item in result["failed"]], [task_id])
        self.assertIsNotNone(
            self.db.connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        )
        self.assertTrue(final.is_dir())
        self.assertTrue(journal.exists())

    def test_recovery_refuses_parent_reparse_swap_before_tree_removal(self):
        task_id = "task_" + "7" * 32

        def rename_then_interrupt(staging, final):
            staging.rename(final)
            raise KeyboardInterrupt("simulated interruption")

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="7" * 32,
        ), patch(
            "media_catalog.digital_workflow._atomic_rename",
            side_effect=rename_then_interrupt,
        ), patch("media_catalog.digital_workflow._record_failed_cleanup"):
            with self.assertRaises(KeyboardInterrupt):
                create_digital_task(
                    self.db,
                    self.request(),
                    self.root,
                    dependencies=self.dependencies(),
                )

        workbench = self.root / "runtime" / "workbench"
        tasks_root = workbench / "tasks"
        real_tasks = workbench / "tasks-before-reparse-swap"
        victim_root = self.root / "outside-victim"
        victim_task = victim_root / task_id
        victim_task.mkdir(parents=True)
        sentinel = victim_task / "sentinel.txt"
        sentinel.write_text("must survive", encoding="utf-8")
        state = digital_workflow._read_recovery_journal(
            workbench / "recovery" / f"create-{task_id}.json"
        )
        digital_workflow._write_owner_marker(
            victim_task, task_id, state["owner_token"]
        )
        real_remove_tree = digital_workflow._remove_tree
        swapped = False

        def replace_parent_then_remove(path, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                tasks_root.rename(real_tasks)
                if os.name == "nt":
                    completed = subprocess.run(
                        [
                            "cmd.exe",
                            "/d",
                            "/c",
                            "mklink",
                            "/J",
                            str(tasks_root),
                            str(victim_root),
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if completed.returncode != 0:
                        self.skipTest("当前 Windows 环境无法创建 Junction")
                else:
                    tasks_root.symlink_to(victim_root, target_is_directory=True)
            return real_remove_tree(path, *args, **kwargs)

        try:
            with patch(
                "media_catalog.digital_workflow._remove_tree",
                side_effect=replace_parent_then_remove,
            ):
                result = recover_incomplete_digital_tasks(self.db, self.root)

            self.assertEqual(result["recovered"], [])
            self.assertEqual(
                [item["task_id"] for item in result["failed"]], [task_id]
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive")
            self.assertIsNotNone(
                self.db.connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            )
        finally:
            if os.path.lexists(tasks_root):
                if tasks_root.is_dir() and not tasks_root.is_symlink():
                    try:
                        os.rmdir(tasks_root)
                    except OSError:
                        pass
                else:
                    tasks_root.unlink()
            if real_tasks.exists():
                real_tasks.rename(tasks_root)

    def test_create_refuses_recovery_reparse_swap_before_journal_update(self):
        task_id = "task_" + "8" * 32
        workbench = self.root / "runtime" / "workbench"
        recovery_root = workbench / "recovery"
        real_recovery = workbench / "recovery-before-reparse-swap"
        outside_recovery = self.root / "outside-recovery"
        outside_recovery.mkdir()
        outside_journal = outside_recovery / f"create-{task_id}.json"
        outside_journal.write_text("must survive", encoding="utf-8")
        real_write_owner_marker = digital_workflow._write_owner_marker
        swapped = False

        def write_owner_then_swap(path, owner_task_id, owner_token):
            nonlocal swapped
            real_write_owner_marker(path, owner_task_id, owner_token)
            if swapped:
                return
            swapped = True
            recovery_root.rename(real_recovery)
            if os.name == "nt":
                completed = subprocess.run(
                    [
                        "cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(recovery_root),
                        str(outside_recovery),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    self.skipTest("当前 Windows 环境无法创建 Junction")
            else:
                recovery_root.symlink_to(outside_recovery, target_is_directory=True)

        try:
            with patch(
                "media_catalog.digital_workflow.secrets.token_hex",
                return_value="8" * 32,
            ), patch(
                "media_catalog.digital_workflow._write_owner_marker",
                side_effect=write_owner_then_swap,
            ):
                with self.assertRaisesRegex(ValueError, "恢复日志"):
                    create_digital_task(
                        self.db,
                        self.request(),
                        self.root,
                        dependencies=self.dependencies(),
                    )

            self.assertEqual(
                outside_journal.read_text(encoding="utf-8"), "must survive"
            )
        finally:
            if os.path.lexists(recovery_root):
                if recovery_root.is_dir() and not recovery_root.is_symlink():
                    try:
                        os.rmdir(recovery_root)
                    except OSError:
                        pass
                else:
                    recovery_root.unlink()
            if real_recovery.exists():
                real_recovery.rename(recovery_root)

    def test_create_refuses_runtime_reparse_swap_before_journal_update(self):
        task_id = "task_" + "6" * 32
        runtime_root = self.root / "runtime"
        real_runtime = self.root / "runtime-before-reparse-swap"
        outside_runtime = self.root / "outside-runtime"
        outside_recovery = outside_runtime / "workbench" / "recovery"
        outside_recovery.mkdir(parents=True)
        outside_journal = outside_recovery / f"create-{task_id}.json"
        outside_journal.write_text("must survive", encoding="utf-8")
        real_write_owner_marker = digital_workflow._write_owner_marker
        swapped = False

        def write_owner_then_swap(path, owner_task_id, owner_token):
            nonlocal swapped
            real_write_owner_marker(path, owner_task_id, owner_token)
            if swapped:
                return
            swapped = True
            runtime_root.rename(real_runtime)
            if os.name == "nt":
                completed = subprocess.run(
                    [
                        "cmd.exe", "/d", "/c", "mklink", "/J",
                        str(runtime_root), str(outside_runtime),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    self.skipTest("当前 Windows 环境无法创建 Junction")
            else:
                runtime_root.symlink_to(outside_runtime, target_is_directory=True)

        try:
            with patch(
                "media_catalog.digital_workflow.secrets.token_hex",
                return_value="6" * 32,
            ), patch(
                "media_catalog.digital_workflow._write_owner_marker",
                side_effect=write_owner_then_swap,
            ):
                with self.assertRaisesRegex(ValueError, "恢复日志"):
                    create_digital_task(
                        self.db,
                        self.request(),
                        self.root,
                        dependencies=self.dependencies(),
                    )
            self.assertEqual(
                outside_journal.read_text(encoding="utf-8"), "must survive"
            )
        finally:
            if os.path.lexists(runtime_root):
                if runtime_root.is_dir() and not runtime_root.is_symlink():
                    try:
                        os.rmdir(runtime_root)
                    except OSError:
                        pass
                else:
                    runtime_root.unlink()
            if real_runtime.exists():
                real_runtime.rename(runtime_root)

    def test_journal_append_uses_verified_handle_if_recovery_parent_swaps(self):
        if os.name != "nt":
            self.skipTest("Windows 文件句柄写入竞争回归")
        workbench = self.root / "runtime" / "workbench"
        recovery_root = workbench / "recovery"
        recovery_root.mkdir(parents=True)
        real_recovery = workbench / "recovery-before-handle-append"
        outside_recovery = self.root / "outside-handle-append"
        outside_recovery.mkdir()
        task_id = "task_" + "a" * 32
        journal = recovery_root / f"create-{task_id}.json"
        outside_journal = outside_recovery / journal.name
        outside_journal.write_text("must survive", encoding="utf-8")
        digital_workflow._atomic_write_json(
            journal, {"step": "planned"}, trusted_root=workbench
        )
        real_write = digital_workflow._windows_write_json_record
        swapped = False
        swap_blocked = False
        verified_handle_content = b""
        write_error = None

        def swap_parent_then_write(descriptor, payload):
            nonlocal swapped, swap_blocked, verified_handle_content, write_error
            if not swapped:
                swapped = True
                try:
                    recovery_root.rename(real_recovery)
                except PermissionError:
                    swap_blocked = True
                    raise
                completed = subprocess.run(
                    [
                        "cmd.exe", "/d", "/c", "mklink", "/J",
                        str(recovery_root), str(outside_recovery),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    self.skipTest("当前 Windows 环境无法创建 Junction")
            try:
                result = real_write(descriptor, payload)
            except BaseException as error:
                write_error = error
                raise
            os.lseek(descriptor, 0, os.SEEK_SET)
            verified_handle_content = os.read(descriptor, 1024 * 1024)
            return result

        try:
            with patch(
                "media_catalog.digital_workflow._windows_write_json_record",
                side_effect=swap_parent_then_write,
            ):
                with self.assertRaises((OSError, ValueError)) as raised:
                    digital_workflow._atomic_write_json(
                        journal, {"step": "updated"}, trusted_root=workbench
                    )
            self.assertEqual(
                outside_journal.read_text(encoding="utf-8"), "must survive"
            )
            self.assertTrue(
                swap_blocked
                or b'"step":"updated"' in verified_handle_content,
                f"write={write_error!r}; raised={raised.exception!r}",
            )
        finally:
            if os.path.lexists(recovery_root):
                if recovery_root.is_dir() and not recovery_root.is_symlink():
                    try:
                        os.rmdir(recovery_root)
                    except OSError:
                        pass
                else:
                    recovery_root.unlink()
            if real_recovery.exists():
                real_recovery.rename(recovery_root)

    def test_recovery_journal_ignores_trailing_partial_record(self):
        workbench = self.root / "runtime" / "workbench"
        recovery = workbench / "recovery"
        recovery.mkdir(parents=True)
        journal = recovery / "partial-recovery.json"
        journal.write_text(
            '{"step":"planned"}\n{"step":"created"}\n{"step":',
            encoding="utf-8",
        )

        self.assertEqual(
            digital_workflow._read_recovery_journal(journal)["step"],
            "created",
        )
        digital_workflow._atomic_write_json(
            journal, {"step": "recovered"}, trusted_root=workbench
        )
        self.assertEqual(
            digital_workflow._read_recovery_journal(journal)["step"],
            "recovered",
        )

    def test_recovery_journal_ignores_partial_utf8_tail_and_can_append(self):
        workbench = self.root / "runtime" / "workbench"
        recovery = workbench / "recovery"
        recovery.mkdir(parents=True)
        journal = recovery / "partial-utf8-recovery.json"
        partial_chinese = "中".encode("utf-8")[:2]
        journal.write_bytes(
            b'{"step":"planned"}\n{"message":"' + partial_chinese
        )

        self.assertEqual(
            digital_workflow._read_recovery_journal(journal)["step"],
            "planned",
        )
        digital_workflow._atomic_write_json(
            journal, {"step": "recovered"}, trusted_root=workbench
        )
        self.assertEqual(
            digital_workflow._read_recovery_journal(journal)["step"],
            "recovered",
        )

    def test_recovery_journal_appends_after_pretty_legacy_object(self):
        workbench = self.root / "runtime" / "workbench"
        recovery = workbench / "recovery"
        recovery.mkdir(parents=True)
        journal = recovery / "pretty-legacy-recovery.json"
        journal.write_text(
            json.dumps(
                {"step": "legacy", "message": "旧记录"},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        digital_workflow._atomic_write_json(
            journal, {"step": "updated"}, trusted_root=workbench
        )

        self.assertEqual(
            digital_workflow._read_recovery_journal(journal)["step"],
            "updated",
        )

    def test_recovery_journal_rejects_malformed_complete_record(self):
        journal = self.root / "malformed-recovery.json"
        journal.write_text(
            '{"step":"planned"}\nnot-json\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "损坏"):
            digital_workflow._read_recovery_journal(journal)

    def test_recovery_journal_reads_legacy_single_object(self):
        journal = self.root / "legacy-recovery.json"
        journal.write_text('{"step":"legacy"}', encoding="utf-8")

        self.assertEqual(
            digital_workflow._read_recovery_journal(journal)["step"],
            "legacy",
        )

    def test_journal_unlink_uses_verified_handle_if_recovery_parent_swaps(self):
        if os.name != "nt":
            self.skipTest("Windows 文件句柄删除竞争回归")
        workbench = self.root / "runtime" / "workbench"
        recovery_root = workbench / "recovery"
        recovery_root.mkdir(parents=True)
        real_recovery = workbench / "recovery-before-unlink"
        outside_recovery = self.root / "outside-unlink"
        outside_recovery.mkdir()
        task_id = "task_" + "b" * 32
        journal = recovery_root / f"create-{task_id}.json"
        journal.write_text("owned journal", encoding="utf-8")
        outside_journal = outside_recovery / journal.name
        outside_journal.write_text("must survive", encoding="utf-8")
        real_delete = digital_workflow._windows_delete_verified_file
        swapped = False

        def swap_parent_then_delete(path, expected_stat, **kwargs):
            nonlocal swapped
            if not swapped and Path(path) == journal:
                swapped = True
                recovery_root.rename(real_recovery)
                if os.name == "nt":
                    completed = subprocess.run(
                        [
                            "cmd.exe", "/d", "/c", "mklink", "/J",
                            str(recovery_root), str(outside_recovery),
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if completed.returncode != 0:
                        self.skipTest("当前 Windows 环境无法创建 Junction")
                else:
                    recovery_root.symlink_to(outside_recovery, target_is_directory=True)
            return real_delete(path, expected_stat, **kwargs)

        try:
            with patch(
                "media_catalog.digital_workflow._windows_delete_verified_file",
                side_effect=swap_parent_then_delete,
            ):
                with self.assertRaises((OSError, ValueError)):
                    digital_workflow._unlink_recovery_journal(
                        journal, trusted_root=workbench, missing_ok=False
                    )
            self.assertEqual(
                outside_journal.read_text(encoding="utf-8"), "must survive"
            )
        finally:
            if os.path.lexists(recovery_root):
                if recovery_root.is_dir() and not recovery_root.is_symlink():
                    try:
                        os.rmdir(recovery_root)
                    except OSError:
                        pass
                else:
                    recovery_root.unlink()
            if real_recovery.exists():
                real_recovery.rename(recovery_root)

    def test_recovery_does_not_delete_reused_source_asset(self):
        first = create_digital_task(
            self.db,
            self.request(title="first task"),
            self.root,
            dependencies=self.dependencies(),
        )
        first_task_id = first["task_id"]
        first_asset_id = first["source_asset_id"]
        db_path = self.db.path

        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="f" * 32,
        ):
            self._fail_after_binding_with_closed_database(
                self.request(title="failed second task")
            )

        reopened = CatalogDatabase.open(db_path)
        try:
            result = recover_incomplete_digital_tasks(reopened, self.root)
            self.assertEqual(len(result["recovered"]), 1)
            self.assertIsNotNone(
                reopened.connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id=?", (first_task_id,)
                ).fetchone()
            )
            self.assertIsNotNone(
                reopened.connection.execute(
                    "SELECT 1 FROM assets WHERE asset_id=?", (first_asset_id,)
                ).fetchone()
            )
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT COUNT(*) FROM asset_locations WHERE asset_id=?",
                    (first_asset_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            reopened.close()

    def test_invalid_journal_path_fails_closed_without_blocking_valid_recovery(self):
        valid_task_id = "task_" + "1" * 32
        db_path = self.db.path
        with patch(
            "media_catalog.digital_workflow.secrets.token_hex",
            return_value="1" * 32,
        ):
            self._fail_after_binding_with_closed_database()

        recovery_root = self.root / "runtime" / "workbench" / "recovery"
        valid_path = next(recovery_root.glob("*.json"))
        malicious = digital_workflow._read_recovery_journal(valid_path)
        malicious_task_id = "task_" + "2" * 32
        malicious["task_id"] = malicious_task_id
        malicious["paths"]["staging"] = "../../outside"
        malicious["paths"]["final"] = "../../outside"
        malicious_path = recovery_root / f"create-{malicious_task_id}.json"
        malicious_path.write_text(
            json.dumps(malicious, ensure_ascii=False), encoding="utf-8"
        )
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("must survive", encoding="utf-8")

        reopened = CatalogDatabase.open(db_path)
        try:
            result = recover_incomplete_digital_tasks(reopened, self.root)
            self.assertIn(
                valid_task_id,
                [item["task_id"] for item in result["recovered"]],
            )
            self.assertIn(
                malicious_task_id,
                [item.get("task_id") for item in result["failed"]],
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive")
            self.assertTrue(malicious_path.exists())
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
