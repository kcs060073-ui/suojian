"""Probe the real 剪辑工作台 MCP transport for one client identity."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


EXPECTED_TOOLS = {
    "cutroom_get_system_status",
    "cutroom_list_tasks",
    "cutroom_get_task",
    "cutroom_get_agent_context",
    "cutroom_get_job",
    "cutroom_get_result_links",
    "cutroom_preflight_task",
    "cutroom_create_task",
    "cutroom_submit_planning_artifact",
    "cutroom_start_task_action",
    "cutroom_cancel_job",
    "cutroom_resume_job",
}


async def _probe_session(
    session: ClientSession,
    client_id: str,
) -> dict[str, object]:
    await session.initialize()
    tools = await session.list_tools()
    names = {item.name for item in tools.tools}
    if names != EXPECTED_TOOLS:
        raise RuntimeError(f"MCP_TOOL_SET_MISMATCH: {sorted(names)}")
    status = await session.call_tool("cutroom_get_system_status", {})
    payload = status.structured_content
    if not isinstance(payload, dict):
        raise RuntimeError("MCP_STATUS_INVALID: structured result is missing")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("clientId") != client_id:
        raise RuntimeError("MCP_CLIENT_ID_MISMATCH")
    return {"clientId": client_id, "toolCount": len(names), "ok": True}


async def probe_stdio(client_id: str) -> dict[str, object]:
    project_root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            str(project_root / "scripts" / "cutroom_mcp.py"),
            "--client",
            client_id,
        ],
        cwd=str(project_root),
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            return await _probe_session(session, client_id)


async def probe_http(client_id: str, url: str) -> dict[str, object]:
    token = os.environ.get("CUTROOM_MCP_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("CUTROOM_MCP_BEARER_TOKEN is required for HTTP probe")
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}
    ) as http_client:
        async with streamable_http_client(
            url, http_client=http_client
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                return await _probe_session(session, client_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe 剪辑工作台 MCP.")
    parser.add_argument(
        "--client",
        choices=("codex", "workbuddy", "doubao_work"),
        default="codex",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()
    if args.transport == "stdio":
        result = asyncio.run(probe_stdio(args.client))
    else:
        result = asyncio.run(probe_http(args.client, args.url))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
