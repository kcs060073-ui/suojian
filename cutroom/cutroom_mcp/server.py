"""Typed stdio MCP tools backed exclusively by :class:`AgentGateway`."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from media_catalog.agent_gateway import AgentGateway


ALLOWED_CLIENT_IDS = frozenset({"codex", "workbuddy", "doubao_work"})
MCP_TOOL_NAMES = (
    "cutroom_cancel_job",
    "cutroom_create_task",
    "cutroom_get_agent_context",
    "cutroom_get_job",
    "cutroom_get_result_links",
    "cutroom_get_system_status",
    "cutroom_get_task",
    "cutroom_list_tasks",
    "cutroom_preflight_task",
    "cutroom_resume_job",
    "cutroom_start_task_action",
    "cutroom_submit_planning_artifact",
)


def normalize_client_id(value: str) -> str:
    if value not in ALLOWED_CLIENT_IDS:
        allowed = ", ".join(sorted(ALLOWED_CLIENT_IDS))
        raise ValueError(
            f"MCP_CLIENT_INVALID: client must be one of {allowed}"
        )
    return value


def _status_for_client(result: dict[str, Any], client_id: str) -> dict[str, Any]:
    if not result.get("ok") or not isinstance(result.get("data"), dict):
        return result
    data = dict(result["data"])
    raw_actions = data.get("agentActions", [])
    allowed_actions = list(raw_actions) if isinstance(raw_actions, list) else []
    data["clientId"] = client_id
    data["mcpToolCount"] = len(MCP_TOOL_NAMES)
    data["mcpTools"] = list(MCP_TOOL_NAMES)
    data["allowedTaskActionCount"] = len(allowed_actions)
    data["allowedTaskActions"] = allowed_actions
    return {**result, "data": data}


def create_mcp_server(
    gateway: AgentGateway,
    *,
    client_id: str = "codex",
) -> MCPServer:
    bound_client_id = normalize_client_id(client_id)
    server = MCPServer(
        "cutroom",
        description="Control CUTROOM Agent-mode video tasks without exposing credentials or gate approvals.",
    )

    @server.tool(structured_output=True)
    async def cutroom_get_system_status() -> dict[str, Any]:
        """Return service status with distinct MCP-tool and task-action counts."""
        return _status_for_client(gateway.get_system_status(), bound_client_id)

    @server.tool(structured_output=True)
    async def cutroom_list_tasks(limit: int = 50) -> dict[str, Any]:
        """List up to 100 CUTROOM task summaries."""
        return gateway.list_tasks(limit=limit)

    @server.tool(structured_output=True)
    async def cutroom_get_task(task_id: str) -> dict[str, Any]:
        """Return one CUTROOM task without credentials or raw logs."""
        return gateway.get_task(task_id)

    @server.tool(structured_output=True)
    async def cutroom_get_agent_context(task_id: str) -> dict[str, Any]:
        """Return the bounded planning context and current input digest."""
        return gateway.get_agent_context(task_id)

    @server.tool(structured_output=True)
    async def cutroom_get_job(job_id: str) -> dict[str, Any]:
        """Return bounded progress for one persistent CUTROOM job."""
        return gateway.get_job(job_id)

    @server.tool(structured_output=True)
    async def cutroom_get_result_links(task_id: str) -> dict[str, Any]:
        """Return the safe workbench task URL and verified result files."""
        return gateway.get_result_links(task_id)

    @server.tool(structured_output=True)
    async def cutroom_preflight_task(request: dict[str, Any]) -> dict[str, Any]:
        """Run read-only creation checks for an Agent-mode CUTROOM task."""
        return gateway.preflight_task(request)

    @server.tool(structured_output=True)
    async def cutroom_create_task(request: dict[str, Any]) -> dict[str, Any]:
        """Create a preflighted Agent-mode CUTROOM task."""
        return gateway.create_task(request)

    @server.tool(structured_output=True)
    async def cutroom_submit_planning_artifact(
        task_id: str,
        artifact_type: str,
        schema_version: int,
        input_digest: str,
        content: dict[str, Any],
        source_model_label: str | None = None,
    ) -> dict[str, Any]:
        """Submit a versioned planning result bound to this client and task input."""
        return gateway.submit_planning_artifact(
            task_id=task_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            input_digest=input_digest,
            content=content,
            source_agent=bound_client_id,
            source_model_label=source_model_label,
        )

    @server.tool(structured_output=True)
    async def cutroom_start_task_action(
        task_id: str,
        action: str,
        confirm_cost: bool = False,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start one allowlisted task action; no gate approval is available."""
        return gateway.start_task_action(
            task_id=task_id,
            action=action,
            confirm_cost=confirm_cost,
            request_id=request_id,
            payload=payload,
        )

    @server.tool(structured_output=True)
    async def cutroom_cancel_job(
        job_id: str,
        reason: str = "Agent 请求取消",
    ) -> dict[str, Any]:
        """Request cancellation of an Agent task job at its next safe point."""
        return gateway.cancel_job(job_id, reason=reason)

    @server.tool(structured_output=True)
    async def cutroom_resume_job(job_id: str) -> dict[str, Any]:
        """Resume a deterministic Agent task job when queue rules allow it."""
        return gateway.resume_job(job_id)

    return server
