"""Audit the installed Cutroom boundary without assuming a live connection."""

from pathlib import Path
import sqlite3
from contextlib import closing
from .installation import installation_info

REQUIRED_CAPABILITIES = (
    "generate_tts",
    "apply_subtitles",
    "place_sfx",
    "render_preview",
)

MCP_TOOLS = (
    "cutroom_get_system_status", "cutroom_list_tasks", "cutroom_get_task",
    "cutroom_get_agent_context", "cutroom_get_job", "cutroom_get_result_links",
    "cutroom_preflight_task", "cutroom_create_task",
    "cutroom_submit_planning_artifact", "cutroom_start_task_action",
    "cutroom_cancel_job", "cutroom_resume_job",
)


def inspect_capabilities(tool_names: set[str], mapping: dict[str, str]) -> dict:
    """Report actual bindings; never infer a Cutroom tool name from a label."""
    return {
        name: {"available": bool(mapping.get(name) in tool_names), "tool": mapping.get(name)}
        for name in REQUIRED_CAPABILITIES
    }


def inspect_local_workbench(root: str | Path | None = None) -> dict:
    """Report files and protocol support; this does not claim the server is running."""
    project = Path(root or Path.home() / "CUTROOM")
    route_file = project / "workbench" / "api.py"
    mcp_file = project / "cutroom_mcp" / "server.py"
    if not route_file.is_file():
        return {"found": False, "transport": None, "capabilities": {},
                "message": "未找到本机 Cutroom 项目；请指定项目路径。"}
    profile_status = {}
    db_path = project / "runtime" / "workbench" / "catalog.sqlite"
    if db_path.is_file():
        try:
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                rows = db.execute("SELECT capability, availability_status FROM model_profiles WHERE enabled=1 AND is_default=1")
                profile_status = {name: status for name, status in rows}
        except sqlite3.Error:
            pass
    return {"found": True, "transport": "mcp_stdio" if mcp_file.is_file() else "rest_workbench",
            "project_path": str(project), "mcp_source_available": mcp_file.is_file(),
            "installation": installation_info(project),
            "mcp_tools": [],
            "mcp_tools_expected_legacy": list(MCP_TOOLS) if mcp_file.is_file() else [],
            "mcp_tools_verified": False,
            "profiles": profile_status,
            "capabilities": {
                "agent_task": mcp_file.is_file(),
                "storyboard_submission": mcp_file.is_file(),
                "render_action": mcp_file.is_file(),
                "native_jianying_draft": False,
            },
            "message": ("已发现 Cutroom Agent MCP 入口，但尚未验证协议及后台执行器；它提供任务、分镜和渲染操作，"
                        "不提供独立的 TTS／字幕／音效工具，也不直接生成剪映草稿。"
                        if mcp_file.is_file() else
                        "当前安装是旧版 REST 工作台，未发现新包中的 Agent MCP。")}
