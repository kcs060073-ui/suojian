"""Digital-human task preflight and creation for the colleague workbench."""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import uuid

from .fingerprint import quick_fingerprint
from .job_timing import job_timing
from .jobs import JobManager
from .model_profiles import (
    ModelProfileUnavailable,
    resolve_task_profile_snapshots,
    save_task_profile_bindings,
)
from .task_sources import get_task_sources, set_task_sources
from .tasks import add_asset, create_task, get_task
from .tools import ToolNotFound, default_ffprobe_search_roots, find_ffmpeg, find_ffprobe
from .workflow import complete_stage, project_workflow


SUPPORTED_ROUTE = "digital_human"
SUPPORTED_SUBTYPES = {"zhaoshang", "caishui"}
SPLIT_TEXT_CAPABILITIES = ("copywriting", "storyboard", "packaging")
REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "node", "ts-node", "faster-whisper")
_SPACE_OVERHEAD_BYTES = 64 * 1024 * 1024
_RECOVERY_SCHEMA_VERSION = 1
_TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{32}$")
_OWNER_MARKER_NAME = ".creation-owner.json"
_SOURCE_RECOVERY_KEYS = (
    "library_id",
    "asset_id",
    "mount_path",
    "relative_path",
    "library_created",
    "asset_created",
    "location_id",
    "location_created",
)


def _selected_model_capabilities(selections: object) -> tuple[str, ...]:
    if not isinstance(selections, Mapping):
        return ("general_text",)
    if "planning" in selections or "broll" in selections:
        capabilities = ["planning"]
        if selections.get("broll"):
            capabilities.append("broll")
        return tuple(capabilities)
    if any(key in selections for key in SPLIT_TEXT_CAPABILITIES):
        capabilities = ["storyboard", "packaging"]
        if selections.get("copywriting"):
            capabilities.append("copywriting")
    else:
        capabilities = ["general_text"]
    if selections.get("video_generation"):
        capabilities.append("video_generation")
    return tuple(capabilities)


def _orchestration_mode(request: Mapping[str, object]) -> str:
    value = request.get("orchestration_mode", "standalone")
    if value not in {"standalone", "agent"}:
        raise ValueError(
            "ORCHESTRATION_MODE_INVALID: 编排模式必须是 standalone 或 agent"
        )
    return str(value)


def _agent_external_capabilities(selections: object) -> tuple[str, ...]:
    if not isinstance(selections, Mapping):
        return ()
    return tuple(
        name
        for name in ("video_generation", "broll")
        if selections.get(name)
    )


_TOOL_PROBE_TIMEOUT_SECONDS = 10
_WHISPER_PROBE_TIMEOUT_SECONDS = 20
_MEDIA_PROBE_TIMEOUT_SECONDS = 30
_PROCESS_OUTPUT_LIMIT_BYTES = 64 * 1024
_ATR_RESPONSE_OVERHEAD_BYTES = 64 * 1024
_ATR_MAX_TIMEOUT_SECONDS = 3600
_NODE_VERSION_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
_TS_NODE_VERSION_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[\\/])[^\s;，。]+"
)

_BASE_COST_STEPS = (
    {
        "key": "speech_timing",
        "label": "本地语音识别与时间轴",
        "execution": "local",
    },
    {
        "key": "ai_storyboard",
        "label": "分镜模型 + 包装模型：AI 规划",
        "execution": "remote",
    },
    {
        "key": "material_match",
        "label": "本地素材匹配与编排",
        "execution": "local",
    },
    {
        "key": "render_and_qa",
        "label": "本地渲染与技术质检",
        "execution": "local",
    },
)
_MISSING_SHOT_GENERATION_STEP = {
    "key": "missing_shot_generation",
    "label": "缺失画面视频生成",
    "execution": "remote",
}

CONFIRMATIONS = (
    {
        "key": "brief",
        "label": "文案锁定",
        "stage": "brief_lock",
        "description": "确认禅镜实际使用的最终口播文案；锁定后修改会使后续结果失效。",
    },
    {
        "key": "picture",
        "label": "画面和顺序锁定",
        "stage": "picture_lock",
        "description": "确认粗剪画面、B-roll 与顺序；数字人口播声音主轴保持连续。",
    },
    {
        "key": "packaging",
        "label": "包装方案确认",
        "stage": "packaging_confirm",
        "description": "确认本片字幕、声音、Logo、水印和其他包装组合。",
    },
    {
        "key": "full_review",
        "label": "完整审片",
        "stage": "full_review",
        "description": "从头到尾观看当前候选版本后，人工确认是否可发布。",
    },
)


@dataclass(frozen=True)
class DigitalWorkflowDependencies:
    """Injectable, read-only preflight observations."""

    file_snapshot: Callable[[Path], Mapping[str, object]]
    directory_status: Callable[[Path], Mapping[str, object]]
    tool_status: Callable[[str, Path], Mapping[str, object]]
    disk_free_bytes: Callable[[Path], int]
    estimate_source: Callable[
        [Mapping[str, object], Mapping[str, object]], Mapping[str, object]
    ]
    queue_observer: Callable[
        [object, Mapping[str, object]], Mapping[str, object]
    ]
    now: Callable[[], str]
    process_runner: Callable[..., Mapping[str, object]] | None = None


class DigitalTaskPreflightError(ValueError):
    """Creation was rejected without leaving a task or input directory."""

    code = "DIGITAL_TASK_PREFLIGHT_FAILED"

    def __init__(self, preflight: Mapping[str, object]):
        self.preflight = dict(preflight)
        messages = [
            str(item.get("message"))
            for item in self.preflight.get("blocking", [])
            if isinstance(item, Mapping)
        ]
        super().__init__("；".join(messages) or "数字人任务预检未通过")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unavailable_estimate(unit: str | None, reason: str) -> dict:
    return {
        "status": "unavailable",
        "min": None,
        "max": None,
        "unit": unit,
        "basis": None,
        "reason": reason,
    }


def _cost_scope(request: Mapping[str, object]) -> dict:
    """Build the system-owned cost scope for the supported digital route."""
    selected = [dict(item) for item in _BASE_COST_STEPS]
    excluded = []
    if request.get("enable_video_generation") is True:
        selected.append(dict(_MISSING_SHOT_GENERATION_STEP))
        generation_summary = "缺失画面视频生成已计入本次远程费用范围。"
    else:
        excluded.append(
            {
                **_MISSING_SHOT_GENERATION_STEP,
                "reason": "本次未启用；确认真实缺口后另行估算和批准",
            }
        )
        generation_summary = "缺失画面视频生成本次未启用，费用另行确认。"
    local_count = sum(item["execution"] == "local" for item in selected)
    remote_count = sum(item["execution"] == "remote" for item in selected)
    return {
        "summary": (
            f"本次包含 {local_count} 个本机步骤和 {remote_count} 个远程服务步骤；"
            f"{generation_summary}"
        ),
        "selected_steps": selected,
        "excluded_steps": excluded,
        "all_selected_steps_local": remote_count == 0,
    }


def _cost_scope_labels(
    cost_scope: Mapping[str, object],
) -> tuple[list[str], list[str]]:
    selected = cost_scope.get("selected_steps")
    excluded = cost_scope.get("excluded_steps")
    included_costs = [
        str(item["label"])
        for item in selected
        if (
            isinstance(item, Mapping)
            and item.get("execution") == "remote"
            and isinstance(item.get("label"), str)
        )
    ] if isinstance(selected, list) else []
    excluded_costs = [
        str(item["label"])
        for item in excluded
        if (
            isinstance(item, Mapping)
            and item.get("execution") == "remote"
            and isinstance(item.get("label"), str)
        )
    ] if isinstance(excluded, list) else []
    return included_costs, excluded_costs


def _default_file_snapshot(path: Path) -> dict:
    candidate = Path(path)
    if candidate.suffix.lower() != ".mp4":
        raise ValueError("禅镜最终文件必须是 MP4")
    if not candidate.is_file() or not os.access(candidate, os.R_OK):
        raise FileNotFoundError(f"禅镜最终 MP4 不存在或不可读：{candidate}")
    before = candidate.stat()
    fingerprint = quick_fingerprint(candidate)
    after = candidate.stat()
    before_identity = (
        before.st_size, before.st_mtime_ns,
        getattr(before, "st_dev", None), getattr(before, "st_ino", None),
    )
    after_identity = (
        after.st_size, after.st_mtime_ns,
        getattr(after, "st_dev", None), getattr(after, "st_ino", None),
    )
    if before_identity != after_identity:
        raise OSError("禅镜最终 MP4 在预检期间发生变化，请等待写入完成后重试")
    file_identity = ""
    if getattr(after, "st_ino", 0):
        file_identity = f"{after.st_dev}:{after.st_ino}"
    return {
        "path": str(candidate.resolve()),
        "size_bytes": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "quick_fingerprint": fingerprint,
        "file_identity": file_identity,
    }


def _default_directory_status(path: Path) -> dict:
    candidate = Path(path)
    return {
        "exists": candidate.exists(),
        "is_dir": candidate.is_dir(),
        "readable": candidate.is_dir() and os.access(candidate, os.R_OK),
    }


def _which_tool(name: str) -> str | None:
    found = shutil.which(name)
    return None if found is None else str(Path(found))


def _bounded_stream_reader(stream, limit: int, result: dict) -> None:
    captured = bytearray()
    truncated = False
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            remaining = max(0, limit - len(captured))
            if remaining:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
    finally:
        try:
            stream.close()
        except OSError:
            pass
    result["bytes"] = bytes(captured)
    result["truncated"] = truncated


def _failed_process_result(error: str, *, error_code: str | None = None) -> dict:
    result = {
        "started": False,
        "timed_out": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "error": error[:200],
    }
    if error_code is not None:
        result["error_code"] = error_code
    return result


def _default_atr_path() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    return (
        Path(local_app_data)
        / "PeterAgentRuntime"
        / "bin"
        / "atr.exe"
    )


def _decode_process_bytes(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    return None


def _windows_atr_process_runner(
    command,
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    env=None,
    atr_path: Path | None = None,
    launcher=None,
) -> dict:
    """Run a probe under ATR's synchronous Windows Job Object ownership."""
    executable = Path(command[0])
    if not executable.is_absolute():
        return _failed_process_result(
            "无法保证硬超时：被探测程序必须使用绝对路径"
        )
    atr = Path(atr_path) if atr_path is not None else _default_atr_path()
    if atr is None or not atr.is_file():
        return _failed_process_result(
            "无法保证硬超时：本机 Agent Tool Runtime（ATR）不可用"
        )
    working_directory = Path(cwd).resolve()
    effective_timeout_seconds = min(
        timeout_seconds, _ATR_MAX_TIMEOUT_SECONDS
    )
    timeout_ms = max(1, int(effective_timeout_seconds * 1000))
    atr_command = [
        str(atr.resolve()),
        "run-process",
        "--exe",
        str(executable.resolve()),
    ]
    for argument in command[1:]:
        atr_command.extend(("--arg", argument))
    atr_command.extend(
        (
            "--cwd",
            str(working_directory),
            "--stdout-encoding",
            "utf-8",
            "--wait-ms",
            "0",
            "--timeout-ms",
            str(timeout_ms),
            "--max-output-bytes",
            str(max_output_bytes),
            "--caller-agent",
            "codex",
            "--json",
        )
    )
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    run = launcher or subprocess.run
    try:
        completed = run(
            atr_command,
            cwd=str(working_directory),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=effective_timeout_seconds + 10,
            env=env,
            startupinfo=startupinfo,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return _failed_process_result(
            "无法保证硬超时：ATR 自身无法启动或未返回确定终态"
        )
    raw_stdout = completed.stdout
    raw_stderr = completed.stderr
    stdout_size = len(raw_stdout.encode("utf-8")) if isinstance(
        raw_stdout, str
    ) else len(raw_stdout) if isinstance(raw_stdout, bytes) else -1
    stderr_size = len(raw_stderr.encode("utf-8")) if isinstance(
        raw_stderr, str
    ) else len(raw_stderr) if isinstance(raw_stderr, bytes) else -1
    envelope_limit = max_output_bytes * 2 + _ATR_RESPONSE_OVERHEAD_BYTES
    if (
        stdout_size < 0
        or stderr_size < 0
        or stdout_size > envelope_limit
        or stderr_size > _ATR_RESPONSE_OVERHEAD_BYTES
    ):
        return _failed_process_result("ATR 终态输出超过安全上限")
    stdout = _decode_process_bytes(raw_stdout)
    stderr = _decode_process_bytes(raw_stderr)
    if stdout is None or stderr is None or stderr.strip():
        return _failed_process_result("ATR 未返回可信的单一 JSON 终态")
    try:
        response = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return _failed_process_result("ATR 未返回合法 JSON 终态")
    if (
        not isinstance(response, Mapping)
        or response.get("schema_version") != "1.0"
        or response.get("status") not in {"success", "permanent_failure"}
    ):
        return _failed_process_result("ATR 返回了不确定的进程终态")
    result = response.get("result")
    if not isinstance(result, Mapping):
        if (
            response.get("status") == "permanent_failure"
            and completed.returncode != 0
            and response.get("phase") == "timeout"
            and isinstance(response.get("error"), Mapping)
            and response["error"].get("code") == "process.timeout"
        ):
            return {
                "started": True,
                "timed_out": True,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
        response_error = response.get("error")
        response_error_code = (
            response_error.get("code") if isinstance(response_error, Mapping) else None
        )
        return _failed_process_result(
            "ATR 未提供可核验的进程终态",
            error_code=(
                response_error_code
                if response_error_code == "runtime.internal_error"
                else None
            ),
        )
    exit_code = result.get("exit_code")
    target_stdout = result.get("stdout")
    target_stderr = result.get("stderr")
    stdout_truncated = result.get("stdout_truncated")
    stderr_truncated = result.get("stderr_truncated")
    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not isinstance(target_stdout, str)
        or not isinstance(target_stderr, str)
        or not isinstance(stdout_truncated, bool)
        or not isinstance(stderr_truncated, bool)
    ):
        return _failed_process_result("ATR 进程结果字段不完整")
    if stdout_truncated or stderr_truncated:
        return _failed_process_result("ATR 目标进程输出超过安全上限")
    status = response["status"]
    error = response.get("error")
    error_code = error.get("code") if isinstance(error, Mapping) else None
    if (
        status == "permanent_failure"
        and completed.returncode != 0
        and response.get("phase") == "timeout"
        and error_code == "process.timeout"
    ):
        return {
            "started": True,
            "timed_out": True,
            "returncode": None,
            "stdout": target_stdout,
            "stderr": target_stderr,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    if status == "success" and completed.returncode == 0 and exit_code == 0:
        pass
    elif (
        status == "permanent_failure"
        and completed.returncode != 0
        and error_code == "process.nonzero_exit"
        and exit_code != 0
    ):
        pass
    else:
        return _failed_process_result("ATR 返回了相互矛盾的进程终态")
    return {
        "started": True,
        "timed_out": False,
        "returncode": exit_code,
        "stdout": target_stdout,
        "stderr": target_stderr,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def _posix_process_runner(
    command,
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    env=None,
) -> dict:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(Path(cwd).resolve()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            env=env,
        )
    except (OSError, ValueError) as error:
        return _failed_process_result(
            f"{type(error).__name__}: process start failed"
        )

    stdout_result: dict = {}
    stderr_result: dict = {}
    readers = (
        threading.Thread(
            target=_bounded_stream_reader,
            args=(process.stdout, max_output_bytes, stdout_result),
            daemon=True,
        ),
        threading.Thread(
            target=_bounded_stream_reader,
            args=(process.stderr, max_output_bytes, stderr_result),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    for reader in readers:
        reader.join(timeout=5)
    stdout = stdout_result.get("bytes", b"").decode("utf-8", errors="replace")
    stderr = stderr_result.get("bytes", b"").decode("utf-8", errors="replace")
    return {
        "started": True,
        "timed_out": timed_out,
        "returncode": None if timed_out else process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": bool(stdout_result.get("truncated")),
        "stderr_truncated": bool(stderr_result.get("truncated")),
    }


def _default_process_runner(
    command,
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    env=None,
) -> dict:
    """Run one read-only probe with bounded output and a hard tree timeout."""
    if (
        not isinstance(command, (list, tuple))
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise ValueError("进程探测命令必须是非空字符串列表")
    if timeout_seconds <= 0 or max_output_bytes <= 0:
        raise ValueError("进程探测超时与输出上限必须为正数")
    if os.name == "nt":
        return _windows_atr_process_runner(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            env=env,
        )
    return _posix_process_runner(
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        env=env,
    )


def _process_failure_reason(result, action: str) -> str | None:
    if not isinstance(result, Mapping):
        return f"{action}返回了无效结果"
    if result.get("started") is not True:
        error = result.get("error")
        if isinstance(error, str) and "无法保证硬超时" in error:
            return f"{action}无法启动：无法保证硬超时（本机 ATR 不可用）"
        return f"{action}无法启动"
    if result.get("timed_out") is True:
        return f"{action}超时"
    if (
        result.get("stdout_truncated") is True
        or result.get("stderr_truncated") is True
    ):
        return f"{action}输出超过安全上限"
    returncode = result.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        return f"{action}没有返回有效退出码"
    if returncode != 0:
        return f"{action}返回退出码 {returncode}"
    return None


def _validate_tool_probe_output(name: str, result: Mapping[str, object]) -> str | None:
    if (
        result.get("stdout_truncated") is True
        or result.get("stderr_truncated") is True
    ):
        return f"{name} 版本输出超过安全上限"
    if name == "faster-whisper":
        return None
    stdout = result.get("stdout")
    stderr = result.get("stderr")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return f"{name} 版本输出字段无效"
    output = "\n".join(item for item in (stdout.strip(), stderr.strip()) if item)
    if name == "ffmpeg":
        first_line = next(
            (line.strip() for line in output.splitlines() if line.strip()), ""
        )
        valid = re.match(r"(?i)^ffmpeg version \S+", first_line) is not None
    elif name == "ffprobe":
        first_line = next(
            (line.strip() for line in output.splitlines() if line.strip()), ""
        )
        valid = re.match(r"(?i)^ffprobe version \S+", first_line) is not None
    elif name == "node":
        valid = _NODE_VERSION_PATTERN.fullmatch(output) is not None
    elif name == "ts-node":
        valid = _TS_NODE_VERSION_PATTERN.fullmatch(output) is not None
    else:
        valid = False
    return None if valid else f"{name} 未返回可识别的真实版本签名"


def _safe_probe_basis(value) -> str | None:
    if value is None:
        return None
    text = _WINDOWS_ABSOLUTE_PATH_PATTERN.sub(
        "[本机路径已隐藏]", str(value)
    )
    return text[:300]


def _windows_batch_command(path: str, arguments: list[str]) -> list[str]:
    candidate = Path(path)
    if os.name != "nt" or candidate.suffix.lower() not in {".cmd", ".bat"}:
        return [str(candidate), *arguments]
    command_interpreter = os.environ.get("ComSpec") or str(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "cmd.exe"
    )
    quoted = " ".join([f'"{candidate}"', *arguments])
    return [command_interpreter, "/d", "/s", "/c", quoted]


def _default_tool_status(
    name: str,
    project_root: Path,
    *,
    process_runner=None,
) -> dict:
    root = Path(project_root)
    runner = process_runner or _default_process_runner
    try:
        if name == "ffmpeg":
            path = find_ffmpeg(
                os.environ, search_roots=default_ffprobe_search_roots(),
            )
            command = [str(path), "-version"]
            action = "ffmpeg -version"
        elif name == "ffprobe":
            path = find_ffprobe(
                os.environ, search_roots=default_ffprobe_search_roots(),
            )
            command = [str(path), "-version"]
            action = "ffprobe -version"
        elif name == "node":
            path = _which_tool("node")
            if path is None:
                raise ToolNotFound("找不到 Node.js")
            command = [str(path), "--version"]
            action = "node --version"
        elif name == "ts-node":
            local_entry = root / "node_modules" / "ts-node" / "dist" / "bin.js"
            if local_entry.is_file():
                node_path = _which_tool("node")
                if node_path is None:
                    raise ToolNotFound("找不到用于启动 ts-node 的 Node.js")
                path = str(local_entry)
                command = [str(node_path), str(local_entry), "--version"]
            else:
                candidates = (
                    root / "node_modules" / ".bin" / "ts-node.cmd",
                    root / "node_modules" / ".bin" / "ts-node",
                )
                path = next(
                    (str(item) for item in candidates if item.is_file()), None
                )
                path = path or _which_tool("ts-node")
                if path is None:
                    raise ToolNotFound("找不到 ts-node")
                command = _windows_batch_command(str(path), ["--version"])
            action = "ts-node --version"
        elif name == "faster-whisper":
            python_path = (
                root / ".venv" / "Scripts" / "python.exe"
                if os.name == "nt"
                else root / ".venv" / "bin" / "python"
            )
            transcribe = root / "scripts" / "transcribe.py"
            if not python_path.is_file() or not transcribe.is_file():
                raise ToolNotFound("找不到项目 .venv 或 scripts/transcribe.py")
            path = str(python_path)
            command = [str(python_path), "-B", "-c", "import faster_whisper"]
            action = "faster-whisper 导入探测"
        else:
            raise ValueError(f"未知工具：{name}")
        result = runner(
            command,
            cwd=root,
            timeout_seconds=(
                _WHISPER_PROBE_TIMEOUT_SECONDS
                if name == "faster-whisper"
                else _TOOL_PROBE_TIMEOUT_SECONDS
            ),
            max_output_bytes=_PROCESS_OUTPUT_LIMIT_BYTES,
        )
        failure = _process_failure_reason(result, action)
        if failure is None:
            failure = _validate_tool_probe_output(name, result)
        if failure is not None:
            return {
                "available": False,
                "path": None,
                "basis": f"本机只读执行探测失败：{failure}"[:300],
            }
    except (OSError, subprocess.SubprocessError, ToolNotFound) as error:
        reason = str(error)
        if isinstance(error, ToolNotFound):
            reason = f"找不到 {name} 可执行入口"
        elif isinstance(error, OSError):
            reason = f"{name} 版本探测无法启动（{type(error).__name__}）"
        return {
            "available": False,
            "path": None,
            "basis": f"本机只读执行探测失败：{reason}"[:300],
        }
    return {
        "available": True,
        "path": str(path),
        "basis": f"本机只读执行探测通过：{action}，退出码 0",
    }


def _positive_finite_duration(value) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _positive_integer(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or number <= 0:
        return None
    return number


def _is_continuous_video_stream(stream: Mapping[str, object]) -> bool:
    disposition = stream.get("disposition")
    if not isinstance(disposition, Mapping):
        return False
    return disposition.get("attached_pic") in {0, "0", False}


def _probe_digital_human_mp4(
    source_path: Path,
    ffprobe_path: str,
    project_root: Path,
    process_runner,
) -> dict:
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,duration,channels,sample_rate:"
        "stream_disposition=attached_pic",
        "-of",
        "json",
        str(source_path.resolve()),
    ]
    try:
        result = process_runner(
            command,
            cwd=Path(project_root),
            timeout_seconds=_MEDIA_PROBE_TIMEOUT_SECONDS,
            max_output_bytes=_PROCESS_OUTPUT_LIMIT_BYTES,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return {
            "valid": False,
            "reason": f"ffprobe 媒体探测无法启动（{type(error).__name__}）",
        }
    failure = _process_failure_reason(result, "ffprobe 媒体探测")
    if failure is not None:
        return {"valid": False, "reason": failure}
    try:
        payload = json.loads(result.get("stdout", ""))
    except (TypeError, json.JSONDecodeError):
        return {"valid": False, "reason": "ffprobe 未返回合法 JSON"}
    if not isinstance(payload, Mapping):
        return {"valid": False, "reason": "ffprobe 媒体信息不是 JSON 对象"}
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return {"valid": False, "reason": "ffprobe 未返回媒体流列表"}
    video_streams = [
        stream for stream in streams
        if (
            isinstance(stream, Mapping)
            and stream.get("codec_type") == "video"
            and _is_continuous_video_stream(stream)
        )
    ]
    if not video_streams:
        return {"valid": False, "reason": "MP4 中没有连续的真实视频流"}
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        return {"valid": False, "reason": "数字人口播 MP4 中没有音频流"}
    narration_audio_streams = [
        stream for stream in audio_streams
        if (
            _positive_integer(stream.get("channels")) is not None
            and _positive_integer(stream.get("sample_rate")) is not None
        )
    ]
    if not narration_audio_streams:
        return {
            "valid": False,
            "reason": "数字人口播音轨缺少有效声道数或采样率",
        }
    format_data = payload.get("format")
    format_duration = (
        _positive_finite_duration(format_data.get("duration"))
        if isinstance(format_data, Mapping)
        else None
    )
    stream_durations = [
        duration
        for duration in (
            _positive_finite_duration(stream.get("duration"))
            for stream in [*video_streams, *narration_audio_streams]
        )
        if duration is not None
    ]
    duration = format_duration or (max(stream_durations) if stream_durations else None)
    if duration is None:
        return {"valid": False, "reason": "MP4 缺少正且有限的有效时长"}
    audio_duration = format_duration or next(
        (
            value
            for value in (
                _positive_finite_duration(stream.get("duration"))
                for stream in narration_audio_streams
            )
            if value is not None
        ),
        None,
    )
    if audio_duration is None:
        return {"valid": False, "reason": "数字人口播音轨缺少有效时长依据"}
    return {
        "valid": True,
        "duration_seconds": duration,
        "video_stream_count": len(video_streams),
        "audio_stream_count": len(narration_audio_streams),
        "basis": (
            f"ffprobe JSON 可解析；video_streams={len(video_streams)}；"
            f"audio_streams={len(narration_audio_streams)}；"
            f"duration_seconds={duration:.6f}"
        ),
    }


def _default_estimate_source(_request, _context) -> dict:
    return {
        "machine_seconds": _unavailable_estimate(
            "seconds", "尚无适用的本机处理耗时记录"
        ),
        "human_seconds": _unavailable_estimate(
            "seconds", "尚无适用的人工操作基线"
        ),
        "api_cost": {
            **_unavailable_estimate(
                None, "所选远程步骤的用量、价格或币种依据不完整"
            ),
            "included_costs": ["AI 分镜文本模型"],
            "excluded_costs": ["另行确认的缺失画面生成"],
        },
    }


def _default_queue_observer(db, request) -> dict:
    heavy_types = tuple(sorted(JobManager.HEAVY_TYPES))
    placeholders = ",".join("?" for _ in heavy_types)
    current_task_id = request.get("task_id")
    if not isinstance(current_task_id, str) or not current_task_id.strip():
        current_task_id = None
    running = db.connection.execute(
        f"""
        SELECT job_id, job_type, task_id FROM jobs
        WHERE status='running' AND job_type IN ({placeholders})
        ORDER BY updated_at, created_at, rowid LIMIT 1
        """,
        heavy_types,
    ).fetchone()
    queued = db.connection.execute(
        f"""
        SELECT j.job_id, j.job_type, j.task_id
        FROM job_queue_entries AS q
        JOIN jobs AS j ON j.job_id=q.job_id
        WHERE j.status='queued'
          AND j.cancel_requested=0
          AND j.job_type IN ({placeholders})
        ORDER BY q.queue_sequence
        """,
        heavy_types,
    ).fetchall()
    own_running = (
        running is not None
        and current_task_id is not None
        and running["task_id"] == current_task_id
    )
    visible_queued = [
        (position, row)
        for position, row in enumerate(queued, start=1)
        if current_task_id is not None and row["task_id"] == current_task_id
    ]
    unknown_outcomes = []
    if current_task_id is not None:
        unknown_outcomes = db.connection.execute(
            """
            SELECT j.job_id, j.job_type, p.outcome_reason
            FROM jobs AS j
            JOIN job_payloads AS p ON p.job_id=j.job_id
            WHERE j.task_id=? AND p.outcome_status='unknown'
            ORDER BY j.updated_at, j.job_id
            """,
            (current_task_id,),
        ).fetchall()
    total_waiting_count = len(queued)
    waiting_count = len(visible_queued)
    observed_at = _utc_now()
    if running is None and total_waiting_count == 0:
        wait = {
            "status": "known",
            "min": 0,
            "max": 0,
            "unit": "seconds",
            "basis": "SQLite 中无运行或排队的重任务",
            "reason": None,
        }
    else:
        wait = _unavailable_estimate(
            "seconds", "队列可确认位置，但尚无可靠的剩余时间基线"
        )
        if own_running:
            wait = {
                "status": "known",
                "min": 0,
                "max": 0,
                "unit": "seconds",
                "basis": "当前任务已经开始运行",
                "reason": None,
            }
        elif visible_queued:
            from .workbench_worker import get_job

            own_job = get_job(db, visible_queued[0][1]["job_id"])
            timing = None if own_job is None else job_timing(db, own_job)
            if timing is not None and timing["estimate_status"] == "estimated":
                start_at = datetime.fromisoformat(
                    timing["estimated_start_at"].replace("Z", "+00:00")
                )
                observed = datetime.fromisoformat(
                    timing["observed_at"].replace("Z", "+00:00")
                )
                seconds = max(0, int(round((start_at - observed).total_seconds())))
                wait = {
                    "status": "estimated",
                    "min": seconds,
                    "max": seconds,
                    "unit": "seconds",
                    "basis": "同类任务最近完成时长中位数与当前队列进度",
                    "reason": None,
                }
    queue_reason = None
    if running is not None and not own_running:
        queue_reason = "其他任务正在占用重任务队列"
    elif running is not None:
        queue_reason = "当前任务的重任务正在运行"
    elif total_waiting_count:
        queue_reason = "重任务将按持久队列顺序领取"
    return {
        "heavy_slot": "available" if running is None else "busy",
        "running_job_id": running["job_id"] if own_running else None,
        "running_job_type": running["job_type"] if own_running else None,
        "waiting_count": waiting_count,
        "queued_jobs": [
            {
                "job_id": row["job_id"],
                "job_type": row["job_type"],
                "position": position,
                "reason": (
                    "等待当前重任务完成"
                    if running is not None
                    else "等待工作进程领取"
                ),
            }
            for position, row in visible_queued
        ],
        "reason": queue_reason,
        "manual_check_required": bool(unknown_outcomes),
        "provider_outcome_unknown": [
            {
                "job_id": row["job_id"],
                "job_type": row["job_type"],
                "reason": (
                    row["outcome_reason"]
                    or "供应商结果未知，需人工核对"
                ),
            }
            for row in unknown_outcomes
        ],
        "observed_at": observed_at,
        "estimated_wait_seconds": wait,
    }


def _default_dependencies(process_runner=None) -> DigitalWorkflowDependencies:
    runner = process_runner or _default_process_runner
    return DigitalWorkflowDependencies(
        file_snapshot=_default_file_snapshot,
        directory_status=_default_directory_status,
        tool_status=lambda name, root: _default_tool_status(
            name, root, process_runner=runner
        ),
        disk_free_bytes=lambda path: shutil.disk_usage(path).free,
        estimate_source=_default_estimate_source,
        queue_observer=_default_queue_observer,
        now=_utc_now,
        process_runner=runner,
    )


def _valid_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _normalize_estimate(
    raw,
    *,
    unit: str | None,
    label: str,
    api_cost: bool = False,
    cost_scope: Mapping[str, object] | None = None,
) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}
    status = raw.get("status")
    raw_unit = raw.get("unit")
    metadata = {}
    if api_cost:
        included_costs, excluded_costs = _cost_scope_labels(cost_scope or {})
        metadata = {
            "included_costs": included_costs,
            "excluded_costs": excluded_costs,
        }
    if status == "unavailable":
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = f"{label}缺少可说明的依据"
        return {
            **_unavailable_estimate(
                unit if not api_cost else (
                    raw_unit if isinstance(raw_unit, str) and raw_unit.strip() else None
                ),
                reason,
            ),
            **metadata,
        }
    minimum = raw.get("min")
    maximum = raw.get("max")
    basis = raw.get("basis")
    valid = (
        status in {"known", "estimated"}
        and _valid_number(minimum)
        and _valid_number(maximum)
        and minimum <= maximum
        and isinstance(basis, str)
        and bool(basis.strip())
        and isinstance(raw_unit, str)
        and bool(raw_unit.strip())
        and (unit is None or raw_unit == unit)
    )
    invalid_reason = None
    exact_zero = minimum == 0 and maximum == 0
    all_selected_steps_local = bool(
        (cost_scope or {}).get("all_selected_steps_local")
    )
    if valid and api_cost and exact_zero and not all_selected_steps_local:
        valid = False
        invalid_reason = (
            "本次包含远程服务步骤，精确零费用无效；"
            "缺少价格或用量依据时应标为 unavailable"
        )
    elif valid and status == "estimated" and exact_zero:
        valid = False
        invalid_reason = "精确零只能标记为 known，不能标记为 estimated"
    if valid and api_cost and exact_zero:
        if status != "known":
            valid = False
            invalid_reason = "精确零费用只能标记为 known"
    if not valid:
        reason = invalid_reason or f"{label}来源返回的范围、单位或依据无效"
        return {
            **_unavailable_estimate(unit if not api_cost else None, reason),
            **metadata,
        }
    if api_cost and exact_zero:
        basis = "系统步骤清单确认本次全部所选步骤均在本机执行，不调用计费服务"
    return {
        "status": status,
        "min": minimum,
        "max": maximum,
        "unit": raw_unit,
        "basis": basis,
        "reason": None,
        **metadata,
    }


def _normalize_estimates(
    raw, *, cost_scope: Mapping[str, object] | None = None
) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}

    def normalize_item(key, *, unit, label, api_cost=False):
        try:
            value = raw.get(key)
            return _normalize_estimate(
                value,
                unit=unit,
                label=label,
                api_cost=api_cost,
                cost_scope=cost_scope,
            )
        except Exception as error:
            result = _unavailable_estimate(
                unit if not api_cost else None,
                f"{label}来源读取失败：{error}",
            )
            if api_cost:
                included_costs, excluded_costs = _cost_scope_labels(
                    cost_scope or {}
                )
                result.update(
                    included_costs=included_costs,
                    excluded_costs=excluded_costs,
                )
            return result

    return {
        "machine_seconds": normalize_item(
            "machine_seconds", unit="seconds", label="机器耗时",
        ),
        "human_seconds": normalize_item(
            "human_seconds", unit="seconds", label="人工耗时",
        ),
        "api_cost": normalize_item(
            "api_cost", unit=None, label="API 费用", api_cost=True,
        ),
    }


def _unknown_queue(now: str, reason: str) -> dict:
    return {
        "heavy_slot": "unknown",
        "running_job_id": None,
        "running_job_type": None,
        "waiting_count": None,
        "queued_jobs": [],
        "manual_check_required": False,
        "provider_outcome_unknown": [],
        "observed_at": now,
        "estimated_wait_seconds": _unavailable_estimate("seconds", reason),
        "reason": reason,
    }


def _normalize_queue(raw, now: str) -> dict:
    if not isinstance(raw, Mapping):
        return _unknown_queue(now, "队列来源返回无效数据")
    slot = raw.get("heavy_slot")
    running_job_id = raw.get("running_job_id")
    running_job_type = raw.get("running_job_type")
    waiting_count = raw.get("waiting_count")
    queued_jobs_provided = "queued_jobs" in raw
    queued_jobs = raw.get("queued_jobs", [])
    manual_check_required = raw.get("manual_check_required", False)
    provider_outcome_unknown = raw.get("provider_outcome_unknown", [])
    observed_at = raw.get("observed_at")
    reason = raw.get("reason")
    queue_positions = (
        [
            item.get("position")
            for item in queued_jobs
            if isinstance(item, Mapping)
        ]
        if isinstance(queued_jobs, list)
        else []
    )
    valid_queued_jobs = (
        isinstance(queued_jobs, list)
        and all(
            isinstance(item, Mapping)
            and isinstance(item.get("job_id"), str)
            and bool(item["job_id"])
            and isinstance(item.get("job_type"), str)
            and bool(item["job_type"])
            and isinstance(item.get("position"), int)
            and not isinstance(item["position"], bool)
            and item["position"] > 0
            and isinstance(item.get("reason"), str)
            and bool(item["reason"])
            for item in queued_jobs
        )
        and queue_positions == sorted(set(queue_positions))
        and (
            not queued_jobs_provided
            or waiting_count is None
            or waiting_count == len(queued_jobs)
        )
    )
    valid_unknown_outcomes = (
        isinstance(manual_check_required, bool)
        and isinstance(provider_outcome_unknown, list)
        and manual_check_required == bool(provider_outcome_unknown)
        and all(
            isinstance(item, Mapping)
            and isinstance(item.get("job_id"), str)
            and bool(item["job_id"])
            and isinstance(item.get("job_type"), str)
            and bool(item["job_type"])
            and isinstance(item.get("reason"), str)
            and bool(item["reason"])
            for item in provider_outcome_unknown
        )
    )
    valid = (
        slot in {"available", "busy", "unknown"}
        and (
            waiting_count is None
            if slot == "unknown"
            else isinstance(waiting_count, int)
            and not isinstance(waiting_count, bool)
            and waiting_count >= 0
        )
        and isinstance(observed_at, str)
        and bool(observed_at.strip())
        and (running_job_id is None or isinstance(running_job_id, str))
        and (running_job_type is None or isinstance(running_job_type, str))
        and (slot != "available" or running_job_id is None)
        and (slot != "available" or running_job_type is None)
        and (
            slot != "busy"
            or bool(running_job_id)
            or (isinstance(reason, str) and bool(reason.strip()))
        )
        and (running_job_id is not None or running_job_type is None)
        and (slot != "unknown" or running_job_id is None)
        and (slot != "unknown" or running_job_type is None)
        and valid_queued_jobs
        and valid_unknown_outcomes
    )
    if not valid:
        return _unknown_queue(now, "队列来源返回无效数据")
    if slot == "unknown":
        return _unknown_queue(
            observed_at,
            reason
            if isinstance(reason, str) and reason.strip()
            else "队列来源暂不可用",
        )
    result = {
        "heavy_slot": slot,
        "running_job_id": running_job_id,
        "running_job_type": running_job_type,
        "waiting_count": waiting_count,
        "queued_jobs": [dict(item) for item in queued_jobs],
        "manual_check_required": manual_check_required,
        "provider_outcome_unknown": [
            dict(item) for item in provider_outcome_unknown
        ],
        "observed_at": observed_at,
        "estimated_wait_seconds": _normalize_estimate(
            raw.get("estimated_wait_seconds"),
            unit="seconds",
            label="预计等待时间",
        ),
    }
    reason = raw.get("reason")
    if isinstance(reason, str) and reason.strip():
        result["reason"] = reason
    return result


def _check(code: str, status: str, message: str, basis: str | None = None) -> dict:
    return {"code": code, "status": status, "message": message, "basis": basis}


def _append_check(checks, blocking, warnings, item) -> None:
    checks.append(item)
    if item["status"] == "blocking":
        blocking.append(item)
    elif item["status"] == "warning":
        warnings.append(item)


def _blocked_route_response(dependencies, message) -> dict:
    item = _check("DIGITAL_ROUTE_NOT_AVAILABLE", "blocking", message)
    return {
        "checks": [item],
        "blocking": [item],
        "warnings": [],
        "estimated_space_bytes": None,
        "next_action": "fix_blocking_checks",
        "estimates": _normalize_estimates({}),
        "queue": _unknown_queue(
            dependencies.now(), "路线未通过，未读取队列"
        ),
        "confirmations": [dict(item) for item in CONFIRMATIONS],
    }


def _library_rows(db, ids: list[str]) -> dict[str, object]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = db.connection.execute(
        f"""
        SELECT library_id, name, mount_path, online, purpose,
               is_default_generic
        FROM libraries WHERE library_id IN ({placeholders})
        """,
        tuple(ids),
    ).fetchall()
    return {row["library_id"]: row for row in rows}


def _observe_directory(dependencies, path: Path) -> dict:
    try:
        raw = dependencies.directory_status(path)
        if not isinstance(raw, Mapping):
            raise ValueError("目录探测没有返回结构化结果")
        return {
            "exists": bool(raw.get("exists")),
            "is_dir": bool(raw.get("is_dir")),
            "readable": bool(raw.get("readable")),
            "reason": raw.get("reason"),
        }
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return {
            "exists": False,
            "is_dir": False,
            "readable": False,
            "reason": f"目录探测失败：{error}",
        }


def preflight_digital_task(
    db,
    request: Mapping[str, object],
    project_root: Path | str,
    *,
    dependencies: DigitalWorkflowDependencies | None = None,
) -> dict:
    """Read local reality and report whether a Phase-1 digital task can be created."""
    deps = dependencies or _default_dependencies()
    root = Path(project_root)
    orchestration_mode = _orchestration_mode(request)
    route = request.get("route")
    subtype = request.get("subtype")
    if route != SUPPORTED_ROUTE:
        return _blocked_route_response(
            deps, "Phase 1 只开放数字人混剪路线"
        )
    if subtype not in SUPPORTED_SUBTYPES:
        return _blocked_route_response(
            deps, "数字人子类型只能选择城市矩阵或财税业务"
        )

    checks: list[dict] = []
    blocking: list[dict] = []
    warnings: list[dict] = []
    _append_check(
        checks, blocking, warnings,
        _check(
            "DIGITAL_ROUTE_SUPPORTED", "passed", "数字人路线与子类型已开放",
            f"route={route}; subtype={subtype}",
        ),
    )

    if request.get("model_profile_ids") is not None:
        selections = request.get("model_profile_ids")
        model_capabilities = (
            _selected_model_capabilities(selections)
            if orchestration_mode == "standalone"
            else _agent_external_capabilities(selections)
        )
        try:
            if model_capabilities:
                resolve_task_profile_snapshots(
                    db,
                    selections,
                    required_capabilities=model_capabilities,
                )
        except (ModelProfileUnavailable, TypeError, ValueError) as exc:
            _append_check(
                checks,
                blocking,
                warnings,
                _check(
                    "MODEL_PROFILE_UNAVAILABLE",
                    "blocking",
                    "所选文案模型当前不可用",
                    str(exc),
                ),
            )
        else:
            _append_check(
                checks,
                blocking,
                warnings,
                _check(
                    "MODEL_PROFILES_READY",
                    "passed",
                    "所选分镜与包装模型已通过连接检测",
                    ", ".join(model_capabilities),
                ),
            )

    title = request.get("title")
    if not isinstance(title, str) or not title.strip():
        _append_check(
            checks, blocking, warnings,
            _check("TASK_TITLE_REQUIRED", "blocking", "任务名称不能为空"),
        )

    script = request.get("final_script")
    if not isinstance(script, str) or not script.strip():
        _append_check(
            checks, blocking, warnings,
            _check(
                "FINAL_SCRIPT_REQUIRED", "blocking",
                "必须提供禅镜实际使用的最终口播文案",
            ),
        )
    else:
        _append_check(
            checks, blocking, warnings,
            _check(
                "FINAL_SCRIPT_READY", "passed", "最终口播文案已提供",
                f"UTF-8 字符数={len(script)}",
            ),
        )

    source_snapshot = None
    source_value = request.get("digital_human_mp4")
    try:
        if not isinstance(source_value, (str, os.PathLike)):
            raise ValueError("未提供禅镜最终 MP4")
        source_path = Path(source_value)
        if source_path.suffix.lower() != ".mp4":
            raise ValueError("禅镜最终文件必须是 MP4")
        source_snapshot = dict(deps.file_snapshot(source_path))
        required = {"path", "size_bytes", "mtime_ns", "quick_fingerprint"}
        if (
            not required.issubset(source_snapshot)
            or not _valid_number(source_snapshot.get("size_bytes"))
            or not isinstance(source_snapshot.get("quick_fingerprint"), str)
            or not source_snapshot["quick_fingerprint"]
        ):
            raise ValueError("禅镜最终 MP4 检查结果缺少身份依据")
    except (OSError, TypeError, ValueError) as error:
        _append_check(
            checks, blocking, warnings,
            _check("DIGITAL_HUMAN_MP4_INVALID", "blocking", str(error)),
        )

    primary_id = request.get("primary_library_id")
    selected_value = request.get("selected_generic_ids", [])
    include_defaults = request.get("include_default_generic", False)
    if not isinstance(primary_id, str) or not primary_id.strip():
        _append_check(
            checks, blocking, warnings,
            _check(
                "PRIMARY_LIBRARY_REQUIRED", "blocking",
                "必须且只能选择一个主素材目录",
            ),
        )
        primary_id = None
    if not (
        isinstance(selected_value, (list, tuple))
        and all(
            isinstance(item, str) and item.strip() for item in selected_value
        )
    ):
        _append_check(
            checks, blocking, warnings,
            _check(
                "GENERIC_LIBRARY_INVALID", "blocking",
                "本次指定通用目录必须是素材库 ID 列表",
            ),
        )
        selected_ids = []
    else:
        selected_ids = list(dict.fromkeys(selected_value))
    if not isinstance(include_defaults, bool):
        _append_check(
            checks, blocking, warnings,
            _check(
                "GENERIC_LIBRARY_INVALID", "blocking",
                "include_default_generic 必须是布尔值",
            ),
        )
        include_defaults = False

    default_ids = []
    if include_defaults:
        default_ids = [
            row["library_id"]
            for row in db.connection.execute(
                """
                SELECT library_id FROM libraries
                WHERE purpose='generic' AND is_default_generic=1
                ORDER BY library_id
                """
            ).fetchall()
            if row["library_id"] not in selected_ids
        ]
    requested_ids = [
        item for item in [primary_id, *selected_ids, *default_ids] if item
    ]
    libraries = _library_rows(db, list(dict.fromkeys(requested_ids)))

    if primary_id is not None:
        primary = libraries.get(primary_id)
        if primary is None or primary["purpose"] != "task_specific":
            _append_check(
                checks, blocking, warnings,
                _check(
                    "PRIMARY_LIBRARY_INVALID", "blocking",
                    "主素材目录不存在或不是任务专属镜头素材库",
                ),
            )
        else:
            directory = _observe_directory(
                deps, Path(primary["mount_path"])
            )
            online = (
                bool(primary["online"])
                and bool(directory.get("exists"))
                and bool(directory.get("is_dir"))
                and bool(directory.get("readable"))
            )
            _append_check(
                checks, blocking, warnings,
                _check(
                    "PRIMARY_LIBRARY_ONLINE" if online else "PRIMARY_LIBRARY_OFFLINE",
                    "passed" if online else "blocking",
                    "主素材目录在线且可读"
                    if online else "主素材目录当前离线或不可读",
                    (
                        f"library_id={primary_id}; mount_path={primary['mount_path']}; "
                        f"reason={directory.get('reason')}"
                    ),
                ),
            )

    for library_id in [*selected_ids, *default_ids]:
        library = libraries.get(library_id)
        if library is None or library["purpose"] != "generic":
            _append_check(
                checks, blocking, warnings,
                _check(
                    "GENERIC_LIBRARY_INVALID", "blocking",
                    f"通用素材目录不存在或用途不合法：{library_id}",
                ),
            )
            continue
        directory = _observe_directory(deps, Path(library["mount_path"]))
        online = (
            bool(library["online"])
            and bool(directory.get("exists"))
            and bool(directory.get("is_dir"))
            and bool(directory.get("readable"))
        )
        _append_check(
            checks, blocking, warnings,
            _check(
                "GENERIC_LIBRARY_ONLINE" if online else "GENERIC_LIBRARY_OFFLINE",
                "passed" if online else "warning",
                f"通用素材目录可用：{library['name']}"
                if online else f"通用素材目录离线，将跳过：{library['name']}",
                (
                    f"library_id={library_id}; mount_path={library['mount_path']}; "
                    f"reason={directory.get('reason')}"
                ),
            ),
        )

    tool_observations: dict[str, Mapping[str, object]] = {}
    for name in REQUIRED_TOOLS:
        try:
            observation = deps.tool_status(name, root)
        except (OSError, RuntimeError, ValueError) as error:
            observation = {
                "available": False,
                "path": None,
                "basis": f"本机只读工具探测失败：{error}",
            }
        observation_path = (
            observation.get("path") if isinstance(observation, Mapping) else None
        )
        available = (
            isinstance(observation, Mapping)
            and observation.get("available") is True
            and isinstance(observation_path, str)
            and bool(observation_path.strip())
        )
        if isinstance(observation, Mapping):
            tool_observations[name] = observation
        basis = observation.get("basis") if isinstance(observation, Mapping) else None
        if (
            isinstance(observation, Mapping)
            and observation.get("available") is True
            and not (
                isinstance(observation_path, str) and observation_path.strip()
            )
        ):
            basis = "工具探测未提供已验证的可执行路径"
        _append_check(
            checks, blocking, warnings,
            _check(
                "REQUIRED_TOOL_READY" if available else "REQUIRED_TOOL_MISSING",
                "passed" if available else "blocking",
                f"{name} 可用" if available else f"{name} 不可用",
                _safe_probe_basis(basis),
            ),
        )

    if source_snapshot is not None:
        ffprobe = tool_observations.get("ffprobe", {})
        ffprobe_path = ffprobe.get("path")
        if not ffprobe.get("available") or not isinstance(ffprobe_path, str) or not ffprobe_path:
            _append_check(
                checks, blocking, warnings,
                _check(
                    "DIGITAL_HUMAN_MP4_INVALID",
                    "blocking",
                    "ffprobe 不可用，无法确认禅镜 MP4 是否可解码",
                    "必须先通过 ffprobe 版本探测，再验证容器、视频流与时长",
                ),
            )
        else:
            media_probe = _probe_digital_human_mp4(
                Path(str(source_snapshot["path"])),
                ffprobe_path,
                root,
                deps.process_runner or _default_process_runner,
            )
            changed_reason = None
            try:
                after_probe = dict(
                    deps.file_snapshot(Path(str(source_snapshot["path"])))
                )
                if _source_snapshot_identity(after_probe) != _source_snapshot_identity(
                    source_snapshot
                ):
                    changed_reason = "禅镜最终 MP4 在媒体探测期间发生变化，请等待写入完成后重试"
            except (OSError, TypeError, ValueError) as error:
                changed_reason = (
                    "媒体探测后无法再次确认禅镜 MP4 身份，请重新预检："
                    f"{type(error).__name__}"
                )
            if changed_reason is not None:
                _append_check(
                    checks, blocking, warnings,
                    _check(
                        "DIGITAL_HUMAN_MP4_CHANGED",
                        "blocking",
                        changed_reason,
                        "探测前后 path/size/mtime/file identity/quick fingerprint 必须一致",
                    ),
                )
            elif not media_probe.get("valid"):
                _append_check(
                    checks, blocking, warnings,
                    _check(
                        "DIGITAL_HUMAN_MP4_INVALID",
                        "blocking",
                        "禅镜最终 MP4 无法通过只读媒体检查",
                        str(media_probe.get("reason") or "ffprobe 媒体检查失败")[:300],
                    ),
                )
            else:
                source_snapshot["media"] = {
                    "duration_seconds": media_probe["duration_seconds"],
                    "video_stream_count": media_probe["video_stream_count"],
                    "audio_stream_count": media_probe["audio_stream_count"],
                }
                _append_check(
                    checks, blocking, warnings,
                    _check(
                        "DIGITAL_HUMAN_MP4_READY",
                        "passed",
                        "禅镜最终 MP4 可解析、含连续视频和口播音轨、时长有效且身份稳定",
                        (
                            f"{media_probe['basis']}; size={source_snapshot['size_bytes']}; "
                            f"mtime_ns={source_snapshot['mtime_ns']}"
                        ),
                    ),
                )

    estimated_space_bytes = None
    if source_snapshot is not None:
        source_size = int(source_snapshot["size_bytes"])
        estimated_space_bytes = source_size * 3 + _SPACE_OVERHEAD_BYTES
        try:
            free_bytes = deps.disk_free_bytes(root)
            if not isinstance(free_bytes, int) or isinstance(free_bytes, bool):
                raise ValueError("磁盘可用空间不是整数")
            enough = free_bytes >= estimated_space_bytes
        except (OSError, RuntimeError, ValueError) as error:
            enough = False
            free_bytes = None
            disk_message = f"无法核验任务磁盘空间：{error}"
        else:
            disk_message = "任务空间充足" if enough else "任务空间不足，不能安全创建"
        _append_check(
            checks, blocking, warnings,
            _check(
                "DISK_SPACE" if enough else "INSUFFICIENT_DISK_SPACE",
                "passed" if enough else "blocking",
                disk_message,
                (
                    f"按源 MP4 实际大小 {source_size} 字节，预留原片等价中间产物、"
                    f"候选成片与 64MiB 工作空间；free_bytes={free_bytes}"
                ),
            ),
        )

    cost_scope = _cost_scope(request)
    estimate_context = {
        "source": source_snapshot,
        "estimated_space_bytes": estimated_space_bytes,
        "primary_library_id": primary_id,
        "selected_generic_ids": selected_ids,
        "default_generic_ids": default_ids,
        "cost_scope": copy.deepcopy(cost_scope),
    }
    try:
        estimates = _normalize_estimates(
            deps.estimate_source(request, estimate_context),
            cost_scope=cost_scope,
        )
    except Exception as error:
        estimates = _normalize_estimates(
            {
                "machine_seconds": _unavailable_estimate(
                    "seconds", f"机器耗时来源不可用：{error}"
                ),
                "human_seconds": _unavailable_estimate(
                    "seconds", f"人工耗时来源不可用：{error}"
                ),
                "api_cost": {
                    **_unavailable_estimate(
                        None, f"API 费用来源不可用：{error}"
                    ),
                },
            },
            cost_scope=cost_scope,
        )

    observed_at = deps.now()
    try:
        queue = _normalize_queue(deps.queue_observer(db, request), observed_at)
    except Exception as error:
        queue = _unknown_queue(
            observed_at, f"队列来源暂不可用：{error}"
        )

    return {
        "checks": checks,
        "blocking": blocking,
        "warnings": warnings,
        "estimated_space_bytes": estimated_space_bytes,
        "next_action": "create_task" if not blocking else "fix_blocking_checks",
        "estimates": estimates,
        "cost_scope": cost_scope,
        "queue": queue,
        "confirmations": [dict(item) for item in CONFIRMATIONS],
        "source": source_snapshot,
    }


def _source_snapshot_identity(snapshot: Mapping[str, object]) -> tuple:
    return (
        os.path.normcase(str(Path(str(snapshot["path"])).resolve())),
        snapshot.get("size_bytes"),
        snapshot.get("mtime_ns"),
        snapshot.get("quick_fingerprint"),
        snapshot.get("file_identity", ""),
    )


def _new_task_id(db, tasks_root: Path) -> str:
    for _ in range(32):
        task_id = "task_" + secrets.token_hex(16)
        exists = db.connection.execute(
            "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if (
            exists is None
            and not (tasks_root / task_id).exists()
            and not (tasks_root / f".{task_id}.staging").exists()
        ):
            return task_id
    raise RuntimeError("无法生成唯一任务 ID，请重试")


def _source_catalog_plan(db, snapshot: Mapping[str, object]) -> dict:
    source_path = Path(str(snapshot["path"])).resolve()
    identity_seed = (
        os.path.normcase(str(source_path))
        + "\0"
        + str(snapshot["quick_fingerprint"])
    )
    library_id = "lib_digital_" + hashlib.sha256(
        identity_seed.encode("utf-8")
    ).hexdigest()[:24]
    library = db.connection.execute(
        """
        SELECT library_id, mount_path, purpose FROM libraries
        WHERE library_id=?
        """,
        (library_id,),
    ).fetchone()
    if library is not None:
        if (
            os.path.normcase(str(Path(library["mount_path"]).resolve()))
            != os.path.normcase(str(source_path.parent))
            or library["purpose"] != "task_specific"
        ):
            raise RuntimeError("数字人来源素材库身份冲突")
    location = db.connection.execute(
        """
        SELECT al.location_id, al.asset_id, a.size_bytes, a.quick_fingerprint
        FROM asset_locations al
        JOIN assets a USING(asset_id)
        WHERE al.library_id=? AND al.relative_path=?
        """,
        (library_id, source_path.name),
    ).fetchone()
    if location is not None:
        if (
            location["size_bytes"] != snapshot["size_bytes"]
            or location["quick_fingerprint"] != snapshot["quick_fingerprint"]
        ):
            raise RuntimeError("已登记数字人来源与当前文件身份不一致")
        asset_id = location["asset_id"]
    else:
        asset_id = "ast_" + uuid.uuid4().hex
    return {
        "library_id": library_id,
        "asset_id": asset_id,
        "mount_path": str(source_path.parent),
        "relative_path": source_path.name,
        "library_created": library is None,
        "asset_created": location is None,
        "location_id": None if location is None else location["location_id"],
        "location_created": location is None,
    }


def _register_source_plan(db, plan: Mapping[str, object], snapshot) -> dict:
    registered = dict(plan)
    with db.transaction() as connection:
        if plan["library_created"]:
            now = connection.execute("SELECT datetime('now')").fetchone()[0]
            connection.execute(
                """
                INSERT INTO libraries(
                  library_id, name, kind, mount_path, online,
                  purpose, is_default_generic, created_at, updated_at
                ) VALUES(?, ?, 'local', ?, 1, 'task_specific', 0, ?, ?)
                """,
                (
                    plan["library_id"],
                    f"数字人输入·{plan['relative_path']}",
                    plan["mount_path"],
                    now,
                    now,
                ),
            )
        if plan["asset_created"]:
            connection.execute(
                """
                INSERT INTO assets(
                  asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                ) VALUES(?, 'video', ?, ?, datetime('now'))
                """,
                (
                    plan["asset_id"],
                    snapshot["size_bytes"],
                    snapshot["quick_fingerprint"],
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns,
                  file_identity, state
                ) VALUES(?, ?, ?, ?, ?, 'available')
                """,
                (
                    plan["asset_id"],
                    plan["library_id"],
                    plan["relative_path"],
                    snapshot["mtime_ns"],
                    snapshot.get("file_identity", ""),
                ),
            )
            registered["location_id"] = cursor.lastrowid
    return registered


def _atomic_rename(staging: Path, final: Path) -> None:
    staging.rename(final)


def _mark_read_only(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(
        mode
        & ~stat.S_IWUSR
        & ~stat.S_IWGRP
        & ~stat.S_IWOTH
    )


def _is_reparse_stat(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse_flag)


def _assert_no_reparse_path(path: Path, trusted_root: Path) -> None:
    root = Path(os.path.abspath(trusted_root))
    target = Path(os.path.abspath(path))
    if not _path_is_within(target, root) or target == root:
        raise ValueError("待清理目录不在可信工作台根内")
    root_stat = os.lstat(root)
    if _is_reparse_stat(root_stat):
        raise ValueError("可信工作台根不能是重解析点")
    resolved_root = root.resolve(strict=True)
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            raise ValueError("待清理目录父链在验证期间发生变化") from None
        if _is_reparse_stat(current_stat):
            raise ValueError("拒绝清理包含符号链接或重解析点的路径")
        if not _path_is_within(current.resolve(strict=True), resolved_root):
            raise ValueError("待清理目录实际路径越出可信工作台根")


def _assert_tree_has_no_reparse(path: Path) -> None:
    pending = [path]
    while pending:
        current = pending.pop()
        current_stat = os.lstat(current)
        if _is_reparse_stat(current_stat):
            raise ValueError("拒绝递归清理符号链接或重解析点")
        if not stat.S_ISDIR(current_stat.st_mode):
            continue
        with os.scandir(current) as entries:
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                if _is_reparse_stat(entry_stat):
                    raise ValueError("拒绝递归清理包含重解析点的目录")
                if stat.S_ISDIR(entry_stat.st_mode):
                    pending.append(Path(entry.path))


def _remove_tree(path: Path, *, trusted_root: Path) -> None:
    if not os.path.lexists(path):
        return
    _assert_no_reparse_path(path, trusted_root)
    _assert_tree_has_no_reparse(path)
    _assert_no_reparse_path(path, trusted_root)

    def make_writable_then_retry(function, value, _error):
        os.chmod(value, stat.S_IWRITE)
        function(value)

    shutil.rmtree(path, onerror=make_writable_then_retry)


def _error_summary(error: BaseException) -> dict:
    return {
        "type": type(error).__name__,
        "message": str(error)[:1000],
    }


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(path)), os.path.normcase(str(parent)))
        ) == os.path.normcase(str(parent))
    except ValueError:
        return False


def _workbench_roots(project_root: Path | str) -> tuple[Path, Path, Path]:
    workbench = (Path(project_root).resolve() / "runtime" / "workbench")
    tasks = workbench / "tasks"
    recovery = workbench / "recovery"
    resolved_workbench = workbench.resolve(strict=False)
    for candidate in (tasks, recovery):
        if not _path_is_within(candidate.resolve(strict=False), resolved_workbench):
            raise ValueError("工作台任务或恢复目录越界")
    return resolved_workbench, tasks, recovery


def _journal_path(recovery_root: Path, task_id: str) -> Path:
    return recovery_root / f"create-{task_id}.json"


def _ensure_directory_chain_no_reparse(path: Path, trusted_root: Path) -> None:
    root = Path(os.path.abspath(trusted_root))
    target = Path(os.path.abspath(path))
    if not _path_is_within(target, root) or target == root:
        raise ValueError("恢复日志目录不在可信项目根内")
    root_stat = os.lstat(root)
    if _is_reparse_stat(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("可信项目根不是普通目录")
    resolved_root = root.resolve(strict=True)
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        current_stat = os.lstat(current)
        if _is_reparse_stat(current_stat) or not stat.S_ISDIR(current_stat.st_mode):
            raise ValueError("恢复日志目录包含符号链接或重解析点")
        if not _path_is_within(current.resolve(strict=True), resolved_root):
            raise ValueError("恢复日志目录实际路径越出可信项目根")


def _assert_safe_file_operation(
    path: Path, trusted_root: Path
) -> os.stat_result | None:
    root = Path(os.path.abspath(trusted_root))
    target = Path(os.path.abspath(path))
    if not _path_is_within(target, root) or target == root:
        raise ValueError("恢复日志文件不在可信工作台根内")
    try:
        _assert_no_reparse_path(target.parent, root)
    except (OSError, ValueError) as error:
        raise ValueError(f"恢复日志路径不安全：{error}") from error
    if not os.path.lexists(target):
        return None
    target_stat = os.lstat(target)
    if _is_reparse_stat(target_stat) or not stat.S_ISREG(target_stat.st_mode):
        raise ValueError("恢复日志文件不是普通文件")
    try:
        _assert_no_reparse_path(target, root)
    except (OSError, ValueError) as error:
        raise ValueError(f"恢复日志路径不安全：{error}") from error
    return target_stat


def _windows_open_file_descriptor(
    path: Path,
    *,
    desired_access: int,
    creation_disposition: int,
    flags: int,
    descriptor_flags: int,
    share_mode: int = 0x0001 | 0x0002 | 0x0004,
) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        desired_access,
        share_mode,
        None,
        creation_disposition,
        flags,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, descriptor_flags)
    except BaseException:
        close_handle(handle)
        raise


def _windows_final_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    needed = get_final_path(handle, None, 0, 0)
    if needed == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = get_final_path(handle, buffer, len(buffer), 0)
    if written == 0 or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(os.path.normcase(os.path.abspath(value)))


def _windows_mark_file_for_delete(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = (("DeleteFile", ctypes.c_ubyte),)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_file_information.restype = wintypes.BOOL
    disposition = FileDispositionInfo(1)
    if not set_file_information(
        handle,
        4,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _parse_recovery_journal_bytes(
    data: bytes,
) -> tuple[dict[str, object], int]:
    if not data:
        raise ValueError("恢复记录没有完整的 JSON 对象")

    try:
        complete_text = data.decode("utf-8")
    except UnicodeDecodeError:
        complete_text = None
    if complete_text is not None:
        try:
            single = json.loads(complete_text)
        except json.JSONDecodeError:
            single = None
        if single is not None:
            if not isinstance(single, dict):
                raise ValueError("恢复记录必须是 JSON 对象")
            return single, len(data)

    complete_end = data.rfind(b"\n") + 1
    if complete_end == 0:
        raise ValueError("恢复记录没有完整的 JSON 对象")
    try:
        text = data[:complete_end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("恢复记录包含损坏的完整 UTF-8 日志行") from error

    decoder = json.JSONDecoder()
    start = len(text) - len(text.lstrip())
    try:
        first, first_end = decoder.raw_decode(text, start)
    except json.JSONDecodeError as error:
        raise ValueError("恢复记录包含损坏的完整日志行") from error
    if not isinstance(first, dict):
        raise ValueError("恢复记录必须是 JSON 对象")

    tail = text[first_end:]
    before_newline, separator, remaining = tail.partition("\n")
    if before_newline.strip():
        raise ValueError("恢复记录包含未分隔的完整日志数据")
    if not separator:
        if tail.strip():
            raise ValueError("恢复记录包含损坏的完整日志行")
        return first, complete_end

    latest = first
    for line in remaining.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("恢复记录包含损坏的完整日志行") from error
        if not isinstance(record, dict):
            raise ValueError("恢复记录必须是 JSON 对象")
        latest = record
    return latest, complete_end


def _windows_write_json_record(
    descriptor: int, payload: Mapping[str, object]
) -> None:
    record = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    existing = b"".join(chunks)
    append_at = 0
    if existing:
        _, append_at = _parse_recovery_journal_bytes(existing)
        if append_at != len(existing):
            os.ftruncate(descriptor, append_at)
        if existing[append_at - 1 : append_at] not in {b"\n", b"\r"}:
            record = b"\n" + record
    os.lseek(descriptor, append_at, os.SEEK_SET)
    written = 0
    while written < len(record):
        count = os.write(descriptor, record[written:])
        if count <= 0:
            raise OSError("恢复日志写入未取得进展")
        written += count
    os.fsync(descriptor)


def _windows_append_json_record(
    path: Path,
    payload: Mapping[str, object],
    *,
    trusted_root: Path,
) -> None:
    import msvcrt

    expected_stat = _assert_safe_file_operation(path, trusted_root)
    parent_stat = os.lstat(path.parent)
    parent_fd = -1
    journal_fd = -1
    created = False
    try:
        parent_fd = _windows_open_file_descriptor(
            path.parent,
            desired_access=0x80000000,
            creation_disposition=3,
            flags=0x02000000 | 0x00200000,
            descriptor_flags=os.O_RDONLY,
            share_mode=0x0001 | 0x0002,
        )
        opened_parent_stat = os.fstat(parent_fd)
        if (
            opened_parent_stat.st_dev,
            opened_parent_stat.st_ino,
        ) != (parent_stat.st_dev, parent_stat.st_ino):
            raise ValueError("恢复日志父目录在写入前被替换")

        journal_fd = _windows_open_file_descriptor(
            path,
            desired_access=0x80000000 | 0x40000000 | 0x00010000,
            creation_disposition=1 if expected_stat is None else 3,
            flags=0x00200000,
            descriptor_flags=os.O_RDWR,
        )
        created = expected_stat is None
        parent_handle = msvcrt.get_osfhandle(parent_fd)
        journal_handle = msvcrt.get_osfhandle(journal_fd)
        opened_stat = os.fstat(journal_fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError("恢复日志文件不是普通文件")
        if expected_stat is not None and (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ) != (expected_stat.st_dev, expected_stat.st_ino):
            raise ValueError("恢复日志在写入前被替换")
        if _windows_final_path(journal_handle).parent != _windows_final_path(
            parent_handle
        ):
            raise ValueError("恢复日志不属于已验证父目录")

        _windows_write_json_record(journal_fd, payload)
        _assert_safe_file_operation(path, trusted_root)
    finally:
        if journal_fd >= 0:
            if created:
                try:
                    if _windows_final_path(
                        msvcrt.get_osfhandle(journal_fd)
                    ).parent != _windows_final_path(
                        msvcrt.get_osfhandle(parent_fd)
                    ):
                        _windows_mark_file_for_delete(
                            msvcrt.get_osfhandle(journal_fd)
                        )
                except BaseException:
                    pass
            os.close(journal_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _read_recovery_journal(path: Path) -> dict[str, object]:
    latest, _ = _parse_recovery_journal_bytes(path.read_bytes())
    return latest


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, object],
    *,
    trusted_root: Path | None = None,
) -> None:
    if trusted_root is not None and os.name == "nt":
        _windows_append_json_record(
            path, payload, trusted_root=trusted_root
        )
        return
    if trusted_root is None:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        _assert_safe_file_operation(path, trusted_root)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        if trusted_root is not None:
            _assert_safe_file_operation(temporary, trusted_root)
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if trusted_root is not None:
            _assert_safe_file_operation(temporary, trusted_root)
            _assert_safe_file_operation(path, trusted_root)
        os.replace(temporary, path)
        if trusted_root is not None:
            _assert_safe_file_operation(path, trusted_root)
    finally:
        if os.path.lexists(temporary):
            if trusted_root is not None:
                _assert_safe_file_operation(temporary, trusted_root)
            temporary.unlink()


def _windows_delete_verified_file(
    path: Path,
    expected_stat: os.stat_result,
    *,
    missing_ok: bool,
) -> None:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    delete_access = 0x00010000
    file_read_attributes = 0x0080
    share_all = 0x0001 | 0x0002 | 0x0004
    open_existing = 3
    open_reparse_point = 0x00200000
    invalid_handle = wintypes.HANDLE(-1).value
    handle = create_file(
        str(path),
        delete_access | file_read_attributes,
        share_all,
        None,
        open_existing,
        open_reparse_point,
        None,
    )
    if handle == invalid_handle:
        error_code = ctypes.get_last_error()
        if missing_ok and error_code in {2, 3}:
            return
        raise ctypes.WinError(error_code)

    descriptor = -1
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        opened_stat = os.fstat(descriptor)
        expected_identity = (expected_stat.st_dev, expected_stat.st_ino)
        opened_identity = (opened_stat.st_dev, opened_stat.st_ino)
        if opened_identity != expected_identity or not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError("恢复日志在删除前被替换")

        raw_handle = msvcrt.get_osfhandle(descriptor)
        _windows_mark_file_for_delete(raw_handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        else:
            close_handle(handle)


def _unlink_recovery_journal(
    path: Path,
    *,
    trusted_root: Path,
    missing_ok: bool = True,
) -> None:
    expected_stat = _assert_safe_file_operation(path, trusted_root)
    if expected_stat is None:
        if missing_ok:
            return
        raise FileNotFoundError(path)
    if os.name == "nt":
        _windows_delete_verified_file(
            path, expected_stat, missing_ok=missing_ok
        )
        return
    _assert_safe_file_operation(path, trusted_root)
    path.unlink(missing_ok=missing_ok)


def _write_owner_marker(path: Path, task_id: str, owner_token: str) -> None:
    _atomic_write_json(
        path / _OWNER_MARKER_NAME,
        {"task_id": task_id, "owner_token": owner_token},
    )


def _owner_marker_matches(path: Path, state: Mapping[str, object]) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    marker = path / _OWNER_MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        payload.get("task_id") == state.get("task_id")
        and payload.get("owner_token") == state.get("owner_token")
    )


def _validate_recovery_state(
    state: Mapping[str, object],
    project_root: Path | str,
    *,
    journal: Path | None = None,
) -> tuple[Path, Path, Path]:
    task_id = state.get("task_id")
    if not isinstance(task_id, str) or _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise ValueError("恢复记录 task_id 非法")
    if (
        state.get("schema_version") != _RECOVERY_SCHEMA_VERSION
        or state.get("operation") != "create_digital_task"
    ):
        raise ValueError("恢复记录版本或操作类型非法")
    owner_token = state.get("owner_token")
    if (
        not isinstance(owner_token, str)
        or re.fullmatch(r"[0-9a-f]{32}", owner_token) is None
    ):
        raise ValueError("恢复记录 owner_token 非法")
    if state.get("step") not in {
        "planned",
        "staging_created",
        "task_created",
        "source_registered",
        "rename_pending",
        "final_owned",
        "completed",
    }:
        raise ValueError("恢复记录步骤非法")
    paths = state.get("paths")
    if not isinstance(paths, Mapping):
        raise ValueError("恢复记录缺少路径")
    expected_staging = f"tasks/.{task_id}.staging"
    expected_final = f"tasks/{task_id}"
    if (
        paths.get("staging") != expected_staging
        or paths.get("final") != expected_final
        or not isinstance(paths.get("staging_owned"), bool)
        or not isinstance(paths.get("final_owned"), bool)
    ):
        raise ValueError("恢复记录路径与 task_id 不匹配")
    source = state.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("恢复记录缺少来源所有权")
    for key in ("library_id", "asset_id", "relative_path"):
        if not isinstance(source.get(key), str) or not source.get(key):
            raise ValueError(f"恢复记录来源字段非法：{key}")
    for key in ("library_created", "asset_created", "location_created"):
        if not isinstance(source.get(key), bool):
            raise ValueError(f"恢复记录来源所有权非法：{key}")
    location_id = source.get("location_id")
    if location_id is not None and (
        not isinstance(location_id, int) or location_id <= 0
    ):
        raise ValueError("恢复记录 location_id 非法")

    _workbench, tasks_root, recovery_root = _workbench_roots(project_root)
    expected_journal = _journal_path(recovery_root, task_id)
    if journal is not None:
        if journal.is_symlink() or journal.resolve(strict=False) != expected_journal.resolve(
            strict=False
        ):
            raise ValueError("恢复记录文件名或位置非法")
    staging = tasks_root / f".{task_id}.staging"
    final = tasks_root / task_id
    return staging, final, expected_journal


def _task_owns_recovery(
    connection, task_id: str, owner_token: str, source: Mapping[str, object]
) -> bool:
    row = connection.execute(
        "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    recovery = brief.get("creation_recovery", {})
    return (
        recovery.get("owner_token") == owner_token
        and recovery.get("source") == _source_recovery_claim(source)
        and brief.get("digital_human_source", {}).get("asset_id")
        == source.get("asset_id")
        and brief.get("digital_human_source", {}).get("library_id")
        == source.get("library_id")
    )


def _completed_task_matches(
    connection, task_id: str, owner_token: str, source: Mapping[str, object]
) -> bool:
    row = connection.execute(
        "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    recovery = brief.get("creation_recovery")
    if recovery is not None:
        if recovery.get("owner_token") != owner_token:
            return False
        if recovery.get("source") != _source_recovery_claim(source):
            return False
    return (
        brief.get("digital_human_source", {}).get("asset_id")
        == source.get("asset_id")
        and brief.get("digital_human_source", {}).get("library_id")
        == source.get("library_id")
    )


def _source_recovery_claim(source: Mapping[str, object]) -> dict:
    return {key: source.get(key) for key in _SOURCE_RECOVERY_KEYS}


def _update_creation_recovery_source(
    db,
    task_id: str,
    owner_token: str,
    source: Mapping[str, object],
) -> None:
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError("待恢复任务不存在")
        brief = json.loads(row["brief_json"] or "{}")
        recovery = brief.get("creation_recovery", {})
        if recovery.get("owner_token") != owner_token:
            raise ValueError("任务恢复令牌不匹配")
        recovery["source"] = _source_recovery_claim(source)
        brief["creation_recovery"] = recovery
        connection.execute(
            """
            UPDATE tasks SET brief_json=?, updated_at=datetime('now')
            WHERE task_id=?
            """,
            (json.dumps(brief, ensure_ascii=False), task_id),
        )


def _final_source_matches(path: Path, source: Mapping[str, object]) -> bool:
    record_path = path / "inputs" / "source.json"
    if not record_path.is_file() or record_path.is_symlink():
        return False
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    expected_path = Path(str(source.get("mount_path", ""))) / str(
        source.get("relative_path", "")
    )
    return (
        record.get("asset_id") == source.get("asset_id")
        and record.get("library_id") == source.get("library_id")
        and Path(str(record.get("path", ""))).resolve(strict=False)
        == expected_path.resolve(strict=False)
    )


def _clear_creation_recovery_owner(db, state: Mapping[str, object]) -> None:
    task_id = str(state["task_id"])
    owner_token = str(state["owner_token"])
    source = state["source"]
    with db.transaction() as connection:
        if not _completed_task_matches(connection, task_id, owner_token, source):
            raise ValueError("完成态任务所有权或来源校验失败")
        row = connection.execute(
            "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        brief = json.loads(row["brief_json"] or "{}")
        recovery = brief.get("creation_recovery")
        if recovery is None:
            return
        if recovery.get("owner_token") != owner_token:
            raise ValueError("完成态任务恢复令牌不匹配")
        brief.pop("creation_recovery", None)
        connection.execute(
            """
            UPDATE tasks SET brief_json=?, updated_at=datetime('now')
            WHERE task_id=?
            """,
            (json.dumps(brief, ensure_ascii=False), task_id),
        )


def _finalize_completed_recovery(
    db,
    state: Mapping[str, object],
    final: Path,
    journal: Path,
) -> None:
    if state.get("step") != "completed":
        raise ValueError("恢复记录不是完成态")
    if state["paths"].get("staging_owned") or not state["paths"].get(
        "final_owned"
    ):
        raise ValueError("完成态目录所有权标记非法")
    trusted_root = final.parents[3]
    _assert_no_reparse_path(final, trusted_root)
    _assert_tree_has_no_reparse(final)
    if not _owner_marker_matches(final, state):
        raise ValueError("完成态 final 所有权标记不匹配")
    if not _final_source_matches(final, state["source"]):
        raise ValueError("完成态 final 来源记录不匹配")
    if not _completed_task_matches(
        db.connection,
        str(state["task_id"]),
        str(state["owner_token"]),
        state["source"],
    ):
        raise ValueError("完成态任务或来源不匹配")
    _clear_creation_recovery_owner(db, state)
    _assert_no_reparse_path(final, trusted_root)
    (final / _OWNER_MARKER_NAME).unlink(missing_ok=True)
    _unlink_recovery_journal(
        journal, trusted_root=trusted_root, missing_ok=True
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _asset_has_database_references(connection, asset_id: str) -> bool:
    tables = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    for table_row in tables:
        table = str(table_row["name"])
        foreign_keys = connection.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"
        ).fetchall()
        for foreign_key in foreign_keys:
            if foreign_key["table"] != "assets":
                continue
            column = str(foreign_key["from"])
            referenced = connection.execute(
                f"SELECT 1 FROM {_quote_identifier(table)} "
                f"WHERE {_quote_identifier(column)}=? LIMIT 1",
                (asset_id,),
            ).fetchone()
            if referenced is not None:
                return True
    for row in connection.execute(
        "SELECT brief_json FROM tasks WHERE brief_json IS NOT NULL"
    ).fetchall():
        try:
            brief = json.loads(row["brief_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if brief.get("digital_human_source", {}).get("asset_id") == asset_id:
            return True
    return False


def _cleanup_database_records(
    db,
    state: Mapping[str, object],
    *,
    expected_task_exists: bool,
) -> dict:
    rollback_error = None
    try:
        db.connection.rollback()
    except BaseException as error:
        rollback_error = _error_summary(error)
    try:
        with db.transaction() as connection:
            task_id = str(state["task_id"])
            source = state["source"]
            task_exists = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone() is not None
            if task_exists != expected_task_exists:
                raise ValueError("恢复期间任务状态发生变化")
            if task_exists and not _task_owns_recovery(
                connection, task_id, str(state["owner_token"]), source
            ):
                raise ValueError("任务恢复所有权校验失败")
            if task_exists:
                connection.execute(
                    "DELETE FROM task_source_snapshots WHERE task_id=?", (task_id,)
                )
                connection.execute(
                    "DELETE FROM task_stage_states WHERE task_id=?", (task_id,)
                )
                connection.execute(
                    "DELETE FROM task_source_settings WHERE task_id=?", (task_id,)
                )
                connection.execute(
                    "DELETE FROM task_source_scopes WHERE task_id=?", (task_id,)
                )
                connection.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            if task_exists and source.get("location_created"):
                if source.get("location_id") is not None:
                    connection.execute(
                        """
                        DELETE FROM asset_locations
                        WHERE location_id=? AND asset_id=? AND library_id=?
                          AND relative_path=?
                        """,
                        (
                            source["location_id"],
                            source["asset_id"],
                            source["library_id"],
                            source["relative_path"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM asset_locations
                        WHERE asset_id=? AND library_id=? AND relative_path=?
                        """,
                        (
                            source["asset_id"],
                            source["library_id"],
                            source["relative_path"],
                        ),
                    )
            if (
                task_exists
                and source.get("asset_created")
                and not _asset_has_database_references(connection, str(source["asset_id"]))
            ):
                connection.execute(
                    "DELETE FROM assets WHERE asset_id=?", (source["asset_id"],)
                )
            if task_exists and source.get("library_created"):
                connection.execute(
                    """
                    DELETE FROM libraries
                    WHERE library_id=?
                      AND NOT EXISTS(
                        SELECT 1 FROM asset_locations
                        WHERE library_id=libraries.library_id
                      )
                      AND NOT EXISTS(
                        SELECT 1 FROM task_source_scopes
                        WHERE library_id=libraries.library_id
                      )
                    """,
                    (source["library_id"],),
                )
    except BaseException as error:
        return {
            "status": "failed",
            "error": _error_summary(error),
            "rollback_error": rollback_error,
        }
    result = {"status": "cleaned"}
    if rollback_error is not None:
        result["rollback_warning"] = rollback_error
    return result


def _inspect_owned_directory(
    path: Path,
    state: Mapping[str, object],
    *,
    ownership_key: str,
    trusted_root: Path,
) -> dict:
    if not os.path.lexists(path):
        return {"status": "absent", "remove": False}
    try:
        _assert_no_reparse_path(path, trusted_root)
        _assert_tree_has_no_reparse(path)
    except BaseException as error:
        return {"status": "failed", "error": _error_summary(error), "remove": False}
    marker_matches = _owner_marker_matches(path, state)
    declared_owned = bool(state["paths"].get(ownership_key))
    if declared_owned and not marker_matches:
        return {
            "status": "failed",
            "reason": "ownership marker mismatch",
            "remove": False,
        }
    if not marker_matches:
        return {"status": "not_owned", "remove": False}
    return {"status": "owned", "remove": True}


def _cleanup_prepared_directory(plan: Mapping[str, object]) -> dict:
    if not plan.get("remove"):
        return {key: value for key, value in plan.items() if key != "remove"}
    try:
        _remove_tree(
            Path(str(plan["path"])),
            trusted_root=Path(str(plan["trusted_root"])),
        )
    except BaseException as error:
        return {"status": "failed", "error": _error_summary(error)}
    return {"status": "cleaned"}


def _prepare_rollback_recovery(
    db,
    state: Mapping[str, object],
    project_root: Path | str,
    *,
    journal: Path,
) -> dict:
    staging, final, expected_journal = _validate_recovery_state(
        state, project_root, journal=journal
    )
    if state.get("step") == "completed":
        raise ValueError("完成态恢复不得进入回滚")
    task_exists = db.connection.execute(
        "SELECT 1 FROM tasks WHERE task_id=?", (state["task_id"],)
    ).fetchone() is not None
    if task_exists and not _task_owns_recovery(
        db.connection,
        str(state["task_id"]),
        str(state["owner_token"]),
        state["source"],
    ):
        raise ValueError("任务恢复所有权校验失败")
    if not task_exists and state.get("step") in {
        "task_created",
        "source_registered",
        "rename_pending",
        "final_owned",
    }:
        raise ValueError("恢复记录声称任务已创建但数据库中不存在")
    workbench, _tasks_root, _recovery_root = _workbench_roots(project_root)
    staging_plan = _inspect_owned_directory(
        staging,
        state,
        ownership_key="staging_owned",
        trusted_root=workbench,
    )
    final_plan = _inspect_owned_directory(
        final,
        state,
        ownership_key="final_owned",
        trusted_root=workbench,
    )
    failures = [
        plan for plan in (staging_plan, final_plan) if plan["status"] == "failed"
    ]
    if failures:
        raise ValueError("目录所有权或路径安全校验失败")
    if (
        state.get("step") == "rename_pending"
        and staging_plan["status"] == "absent"
        and final_plan["status"] != "owned"
    ):
        raise ValueError("rename_pending 的 final 缺少可验证所有权")
    for plan, path in ((staging_plan, staging), (final_plan, final)):
        plan["path"] = str(path)
        plan["trusted_root"] = str(workbench)
    return {
        "staging": staging_plan,
        "final": final_plan,
        "journal": expected_journal,
        "task_exists": task_exists,
    }


def _cleanup_complete(recovery: Mapping[str, object]) -> bool:
    return (
        recovery["database"]["status"] == "cleaned"
        and recovery["staging"]["status"] in {"cleaned", "absent", "not_owned"}
        and recovery["final"]["status"] in {"cleaned", "absent", "not_owned"}
    )


def _execute_rollback_recovery(
    db,
    state: Mapping[str, object],
    project_root: Path | str,
    *,
    journal: Path,
) -> dict:
    prepared = _prepare_rollback_recovery(
        db, state, project_root, journal=journal
    )
    staging_result = _cleanup_prepared_directory(prepared["staging"])
    final_result = _cleanup_prepared_directory(prepared["final"])
    if staging_result["status"] == "failed" or final_result["status"] == "failed":
        database_result = {
            "status": "deferred",
            "reason": "目录清理未完整完成，数据库回滚未执行",
        }
    else:
        database_result = _cleanup_database_records(
            db,
            state,
            expected_task_exists=bool(prepared["task_exists"]),
        )
    return {
        "database": database_result,
        "staging": staging_result,
        "final": final_result,
    }


def _record_failed_cleanup(
    db,
    journal: Path,
    state: dict,
    original_error: BaseException,
    staging: Path,
    final: Path,
) -> None:
    trusted_root = staging.parents[3]
    state["error"] = _error_summary(original_error)
    if state.get("step") == "completed":
        try:
            _finalize_completed_recovery(db, state, final, journal)
        except BaseException as error:
            state["recovery"] = {
                "completion": {"status": "failed", "error": _error_summary(error)}
            }
            try:
                _atomic_write_json(journal, state, trusted_root=trusted_root)
            except BaseException:
                pass
        return
    project_root = staging.parent.parent.parent.parent
    try:
        state["recovery"] = _execute_rollback_recovery(
            db, state, project_root, journal=journal
        )
    except BaseException as error:
        state["recovery"] = {
            "database": {
                "status": "deferred",
                "reason": "恢复预验证失败，数据库回滚未执行",
            },
            "staging": {"status": "not_attempted"},
            "final": {"status": "not_attempted"},
            "validation": {"status": "failed", "error": _error_summary(error)},
        }
    if _cleanup_complete(state["recovery"]):
        try:
            _unlink_recovery_journal(
                journal, trusted_root=trusted_root, missing_ok=True
            )
        except BaseException:
            try:
                _atomic_write_json(journal, state, trusted_root=trusted_root)
            except BaseException:
                pass
    else:
        try:
            _atomic_write_json(journal, state, trusted_root=trusted_root)
        except BaseException:
            pass


def recover_incomplete_digital_tasks(
    db, project_root: Path | str
) -> dict:
    """Replay incomplete digital-task cleanup journals without trusting their paths."""
    _workbench, _tasks_root, recovery_root = _workbench_roots(project_root)
    trusted_root = Path(project_root).resolve(strict=True)
    result = {"processed": 0, "recovered": [], "failed": []}
    if not recovery_root.exists():
        return result
    _assert_no_reparse_path(recovery_root, trusted_root)
    if recovery_root.is_symlink():
        raise ValueError("恢复目录不能是符号链接")
    for journal in sorted(recovery_root.glob("create-*.json")):
        result["processed"] += 1
        state = None
        task_id = None
        try:
            _assert_safe_file_operation(journal, trusted_root)
            state = _read_recovery_journal(journal)
            if not isinstance(state, dict):
                raise ValueError("恢复记录必须是 JSON 对象")
            task_id = state.get("task_id")
            staging, final, expected_journal = _validate_recovery_state(
                state, project_root, journal=journal
            )
            if state.get("step") == "completed":
                _finalize_completed_recovery(
                    db, state, final, expected_journal
                )
                result["recovered"].append(
                    {
                        "task_id": task_id,
                        "journal": str(expected_journal),
                        "action": "completed_finalized",
                    }
                )
                continue
            recovery = _execute_rollback_recovery(
                db, state, project_root, journal=expected_journal
            )
            state["recovery"] = recovery
            if not _cleanup_complete(recovery):
                _atomic_write_json(
                    expected_journal, state, trusted_root=trusted_root
                )
                raise RuntimeError("恢复清理未完整完成")
            _unlink_recovery_journal(
                expected_journal, trusted_root=trusted_root, missing_ok=True
            )
            result["recovered"].append(
                {"task_id": task_id, "journal": str(expected_journal)}
            )
        except BaseException as error:
            result["failed"].append(
                {
                    "task_id": task_id,
                    "journal": str(journal),
                    "error": _error_summary(error),
                }
            )
    return result


def _source_changed_preflight(preflight: Mapping[str, object]) -> dict:
    result = dict(preflight)
    item = _check(
        "DIGITAL_HUMAN_MP4_CHANGED",
        "blocking",
        "禅镜最终 MP4 在任务创建期间发生变化，请等待文件稳定后重新预检",
    )
    result["checks"] = [*preflight.get("checks", []), item]
    result["blocking"] = [*preflight.get("blocking", []), item]
    result["next_action"] = "fix_blocking_checks"
    return result


def create_digital_task(
    db,
    request: Mapping[str, object],
    project_root: Path | str,
    *,
    dependencies: DigitalWorkflowDependencies | None = None,
) -> dict:
    """Create a preflighted task with compensating cleanup across SQLite and files."""
    deps = dependencies or _default_dependencies()
    root = Path(project_root)
    orchestration_mode = _orchestration_mode(request)
    preflight = preflight_digital_task(
        db, request, root, dependencies=deps
    )
    if preflight["blocking"]:
        raise DigitalTaskPreflightError(preflight)

    profile_snapshots = None
    if request.get("model_profile_ids") is not None:
        selections = request.get("model_profile_ids")
        model_capabilities = (
            _selected_model_capabilities(selections)
            if orchestration_mode == "standalone"
            else _agent_external_capabilities(selections)
        )
        if model_capabilities:
            profile_snapshots = resolve_task_profile_snapshots(
                db,
                selections,
                required_capabilities=model_capabilities,
            )

    _workbench, tasks_root, recovery_root = _workbench_roots(root)
    trusted_root = root.resolve(strict=True)
    _ensure_directory_chain_no_reparse(
        recovery_root, trusted_root
    )
    _assert_no_reparse_path(recovery_root, trusted_root)
    task_id = _new_task_id(db, tasks_root)
    staging = tasks_root / f".{task_id}.staging"
    final = tasks_root / task_id
    source_snapshot = dict(preflight["source"])
    source_plan = _source_catalog_plan(db, source_snapshot)
    owner_token = uuid.uuid4().hex
    recovery_state = {
        "schema_version": _RECOVERY_SCHEMA_VERSION,
        "operation": "create_digital_task",
        "task_id": task_id,
        "owner_token": owner_token,
        "paths": {
            "staging": f"tasks/.{task_id}.staging",
            "final": f"tasks/{task_id}",
            "staging_owned": False,
            "final_owned": False,
        },
        "source": dict(source_plan),
        "step": "planned",
        "error": None,
        "recovery": {},
    }
    journal = _journal_path(recovery_root, task_id)
    _atomic_write_json(journal, recovery_state, trusted_root=trusted_root)
    failure = None
    try:
        source_record = {
            "read_only": True,
            "path": source_snapshot["path"],
            "size_bytes": source_snapshot["size_bytes"],
            "mtime_ns": source_snapshot["mtime_ns"],
            "quick_fingerprint": source_snapshot["quick_fingerprint"],
            "file_identity": source_snapshot.get("file_identity", ""),
            "asset_id": source_plan["asset_id"],
            "library_id": source_plan["library_id"],
        }
        script = str(request["final_script"])
        script_bytes = script.encode("utf-8")
        script_digest = hashlib.sha256(script_bytes).hexdigest()
        brief = {
            "route": SUPPORTED_ROUTE,
            "subtype": request["subtype"],
            "digital_human_source": source_record,
            "final_script": {
                "relative_path": "inputs/final-script.txt",
                "sha256": script_digest,
            },
            "materials": {
                "primary_library_id": request["primary_library_id"],
                "selected_generic_ids": list(
                    request.get("selected_generic_ids", [])
                ),
                "include_default_generic": request.get(
                    "include_default_generic", False
                ),
            },
            "creation_recovery": {
                "owner_token": owner_token,
                "source": _source_recovery_claim(source_plan),
            },
        }

        tasks_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        recovery_state["paths"]["staging_owned"] = True
        _write_owner_marker(staging, task_id, owner_token)
        recovery_state["step"] = "staging_created"
        _atomic_write_json(journal, recovery_state, trusted_root=trusted_root)
        (staging / "inputs").mkdir()
        (staging / "inputs" / "final-script.txt").write_bytes(script_bytes)
        (staging / "inputs" / "source.json").write_text(
            json.dumps(
                source_record,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        create_task(
            db,
            task_id,
            str(request["title"]).strip(),
            task_type=SUPPORTED_ROUTE,
            brief=brief,
            orchestration_mode=orchestration_mode,
        )
        if profile_snapshots is not None:
            save_task_profile_bindings(db, task_id, profile_snapshots)
        recovery_state["step"] = "task_created"
        _atomic_write_json(journal, recovery_state, trusted_root=trusted_root)
        source_plan = _register_source_plan(db, source_plan, source_snapshot)
        _update_creation_recovery_source(
            db, task_id, owner_token, source_plan
        )
        recovery_state["source"] = dict(source_plan)
        recovery_state["step"] = "source_registered"
        _atomic_write_json(journal, recovery_state, trusted_root=trusted_root)
        set_task_sources(
            db,
            task_id,
            primary_library_id=str(request["primary_library_id"]),
            selected_generic_ids=list(
                request.get("selected_generic_ids", [])
            ),
            include_default_generic=bool(
                request.get("include_default_generic", False)
            ),
        )
        add_asset(
            db,
            task_id,
            source_plan["asset_id"],
            "digital_human",
        )
        stage_digest = hashlib.sha256(
            json.dumps(
                {
                    "source": source_record,
                    "script_sha256": script_digest,
                    "materials": brief["materials"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        complete_stage(
            db,
            task_id,
            "create_preflight",
            input_digest=stage_digest,
            details={
                "source_asset_id": source_plan["asset_id"],
                "input_directory": f"runtime/workbench/tasks/{task_id}/inputs",
            },
        )

        final_snapshot = dict(
            deps.file_snapshot(Path(str(source_snapshot["path"])))
        )
        if _source_snapshot_identity(final_snapshot) != _source_snapshot_identity(
            source_snapshot
        ):
            raise DigitalTaskPreflightError(
                _source_changed_preflight(preflight)
            )
        recovery_state["step"] = "rename_pending"
        _atomic_write_json(journal, recovery_state, trusted_root=trusted_root)
        _atomic_rename(staging, final)
        recovery_state["paths"]["staging_owned"] = False
        recovery_state["paths"]["final_owned"] = True
        recovery_state["step"] = "final_owned"
        _atomic_write_json(journal, recovery_state, trusted_root=trusted_root)
        _mark_read_only(final / "inputs" / "source.json")
        recovery_state["step"] = "completed"
        _atomic_write_json(journal, recovery_state, trusted_root=trusted_root)
        _clear_creation_recovery_owner(db, recovery_state)
        _unlink_recovery_journal(
            journal, trusted_root=trusted_root, missing_ok=False
        )
        try:
            (final / _OWNER_MARKER_NAME).unlink(missing_ok=True)
        except OSError:
            pass
    except BaseException as error:
        failure = error
        raise
    finally:
        if failure is not None:
            _record_failed_cleanup(
                db, journal, recovery_state, failure, staging, final
            )

    return {
        "task_id": task_id,
        "task": get_task(db, task_id),
        "source_asset_id": source_plan["asset_id"],
        "source_library_id": source_plan["library_id"],
        "input_dir": str(final / "inputs"),
        "sources": get_task_sources(db, task_id),
        "workflow": project_workflow(db, task_id),
        "preflight": preflight,
    }
