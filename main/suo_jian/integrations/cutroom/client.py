"""剪辑工作台 MCP boundary. Bind actual tool names after capability discovery."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess

from .capability import REQUIRED_CAPABILITIES
from .installation import require_existing_database


class CutroomClient:
    def __init__(self, invoke_tool: Callable[[str, dict], dict], tool_mapping: dict[str, str]):
        missing = set(REQUIRED_CAPABILITIES) - set(tool_mapping)
        if missing:
            raise ValueError(f"剪辑工作台 缺少能力映射：{', '.join(sorted(missing))}")
        self._invoke_tool = invoke_tool
        self._tool_mapping = tool_mapping

    def _call(self, capability: str, payload: dict) -> dict:
        result = self._invoke_tool(self._tool_mapping[capability], payload)
        if not isinstance(result, dict):
            raise TypeError(f"剪辑工作台 {capability} 未返回结构化结果。")
        return result

    def generate_tts(self, payload: dict) -> dict:
        return self._call("generate_tts", payload)

    def apply_subtitles(self, payload: dict) -> dict:
        return self._call("apply_subtitles", payload)

    def place_sfx(self, payload: dict) -> dict:
        return self._call("place_sfx", payload)

    def render_preview(self, payload: dict) -> dict:
        return self._call("render_preview", payload)


def call_cutroom_mcp(project_root: str | Path, tool: str, arguments: dict | None = None) -> dict:
    """Call an explicitly allowed 剪辑工作台 tool using its own virtual environment."""
    root = Path(project_root).resolve()
    require_existing_database(root)
    python = root / ".venv" / "Scripts" / "python.exe"
    bridge = Path(__file__).with_name("mcp_bridge.py")
    if not python.is_file() or not (root / "cutroom_mcp" / "server.py").is_file():
        raise FileNotFoundError("剪辑工作台 MCP 或其 Python 环境尚未部署完整。")
    if tool not in {"cutroom_get_system_status", "cutroom_preflight_task", "cutroom_create_task",
                    "cutroom_get_task", "cutroom_get_result_links", "cutroom_get_agent_context",
                    "cutroom_submit_planning_artifact", "cutroom_start_task_action",
                    "cutroom_list_tasks", "cutroom_get_job"}:
        raise ValueError("溯剪尚未开放此 剪辑工作台 工具。")
    result = subprocess.run(
        [str(python), str(bridge), str(root), tool],
        input=json.dumps(arguments or {}, ensure_ascii=False), text=True,
        capture_output=True, timeout=30, check=False,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if result.returncode:
        raise RuntimeError(f"剪辑工作台 MCP 调用失败：{result.stderr[-700:]}")
    return json.loads(result.stdout)


def call_readonly_mcp(project_root: str | Path, tool: str, arguments: dict | None = None) -> dict:
    if tool not in {"cutroom_get_system_status", "cutroom_preflight_task",
                    "cutroom_get_task", "cutroom_get_result_links", "cutroom_get_agent_context",
                    "cutroom_list_tasks", "cutroom_get_job"}:
        raise ValueError("这项操作不是只读的。")
    return call_cutroom_mcp(project_root, tool, arguments)


def call_local_adapter(project_root: str | Path, operation: str, arguments: dict | None = None) -> dict:
    """Compatibility layer, not an invented MCP tool. Secrets stay in 剪辑工作台."""
    if operation not in {"model_settings", "catalog", "create", "preflight", "probe_tts", "inspect", "lock_brief", "sync_preview", "context",
                         "submit", "start", "job", "lock_picture", "packaging", "save_packaging", "packaging_options", "repackage", "reference_bind"}:
        raise ValueError("未开放的 剪辑工作台 适配操作。")
    root = Path(project_root).resolve()
    info = require_existing_database(root)
    if not info["legacy_automatic_supported"] and operation in {"create", "probe_tts", "lock_brief", "lock_picture", "packaging", "repackage", "reference_bind", "save_packaging"}:
        raise ValueError("新版 剪辑工作台 的文案、画面和包装确认请在其工作台完成；旧自动确认或参考扩展未经过新版验收，已停止本次操作。")
    executable = root / ".venv" / "Scripts" / "python.exe"
    if not executable.is_file():
        raise FileNotFoundError("找不到 剪辑工作台 的运行环境。")
    result = subprocess.run([str(executable), str(Path(__file__).with_name("local_bridge.py")),
                             str(root), operation], input=json.dumps(arguments or {}, ensure_ascii=False),
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=180, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("剪辑工作台 适配层没有返回有效结果；请检查本机部署版本。") from None
    if result.returncode or "bridge_error" in value:
        raise RuntimeError(value.get("bridge_error", "剪辑工作台 操作失败。"))
    if operation == "catalog":
        value["adapter"] = {"version": info["version"], "distribution": info["distribution"],
                            "automatic_pipeline_supported": info["legacy_automatic_supported"],
                            "reference_extension_available": info["reference_extension_available"]}
    return value
