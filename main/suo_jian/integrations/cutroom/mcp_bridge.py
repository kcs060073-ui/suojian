"""Small stdio bridge, executed with Cutroom's Python (which owns `mcp`).

Task creation and explicitly confirmed actions are exposed; no gate approval is.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import os
if __package__:
    from .installation import mcp_arguments
else:
    from installation import mcp_arguments

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ALLOWED = {
    "cutroom_get_system_status", "cutroom_preflight_task",
    "cutroom_create_task", "cutroom_get_task", "cutroom_get_result_links",
    "cutroom_get_agent_context", "cutroom_submit_planning_artifact",
    "cutroom_start_task_action",
    "cutroom_list_tasks", "cutroom_get_job",
}


async def run(root: Path, tool: str, arguments: dict) -> dict:
    if tool not in ALLOWED:
        raise ValueError("Cutroom tool is not enabled by SuoJian")
    params = StdioServerParameters(
        command=sys.executable,
        args=mcp_arguments(root, os.environ.get("SUOJIAN_CUTROOM_ORIGIN")),
        cwd=str(root),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {entry.name for entry in tools.tools}
            if tool not in names:
                raise ValueError("当前 Cutroom 版本没有提供请求的 MCP 工具：" + tool)
            result = await session.call_tool(tool, arguments)
            if result.is_error:
                raise RuntimeError("Cutroom MCP 拒绝执行请求；请在工作台检查任务状态和门禁。")
            if not isinstance(result.structured_content, dict):
                raise RuntimeError("Cutroom did not return a structured result")
            payload = result.structured_content
            if tool == "cutroom_get_system_status":
                payload = {**payload, "suojian_protocol": {"verified": True, "tool_count": len(names), "tools": sorted(names)}}
            return payload


if __name__ == "__main__":
    project_root = Path(sys.argv[1]).resolve()
    tool_name = sys.argv[2]
    args = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(asyncio.run(run(project_root, tool_name, args)), ensure_ascii=False))
