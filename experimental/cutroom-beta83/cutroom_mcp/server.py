"""Typed stdio MCP tools backed exclusively by :class:`AgentGateway`."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from media_catalog.agent_gateway import AgentGateway


ALLOWED_CLIENT_IDS = frozenset({"codex", "workbuddy", "doubao_work"})
MCP_TOOL_NAMES = tuple(sorted((
    "cutroom_propose_edit",
    "cutroom_get_edit_proposal",
    "cutroom_cancel_edit_proposal",
    "cutroom_apply_edit_proposal",
    "cutroom_undo_edit_proposal",
    "cutroom_locate_rhythm_suggestion",
    "cutroom_build_rhythm_advice",
    "cutroom_get_rhythm_advice",
    "cutroom_preview_rhythm_edit",
    "cutroom_build_reshoot_list",
    "cutroom_get_reshoot_list",
    "cutroom_export_reshoot_list",
    "cutroom_cancel_job",
    "cutroom_create_task",
    "cutroom_get_agent_context",
    "cutroom_get_job",
    "cutroom_get_packaging_request",
    "cutroom_get_result_links",
    "cutroom_get_system_status",
    "cutroom_get_task",
    "cutroom_list_tasks",
    "cutroom_preflight_task",
    "cutroom_resume_job",
    "cutroom_start_task_action",
    "cutroom_submit_packaging_result",
    "cutroom_submit_planning_artifact",
)))


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

    @server.tool()
    async def cutroom_propose_edit(task_id: str, request_id: str, expected_revision: str, instruction: str, plan_id: str | None = None) -> dict[str, Any]:
        """限定文字指令形成差异草稿，不直接修改或调用模型；含糊要求先澄清。"""
        return gateway.propose_edit(task_id, request_id, expected_revision, instruction, plan_id)

    @server.tool()
    async def cutroom_get_edit_proposal(task_id: str, proposal_id: str | None = None) -> dict[str, Any]:
        """读取本任务的历史草稿/回执，当前旧草稿标记失效。"""
        return gateway.get_edit_proposal(task_id, proposal_id)

    @server.tool()
    async def cutroom_cancel_edit_proposal(task_id: str, proposal_id: str, request_id: str) -> dict[str, Any]:
        """取消未保存草稿，保持原任务。"""
        return gateway.cancel_edit_proposal(task_id, proposal_id, request_id)

    @server.tool()
    async def cutroom_apply_edit_proposal(task_id: str, proposal_id: str, request_id: str, expected_revision: str, confirm: bool = False) -> dict[str, Any]:
        """仅在用户明确确认差异后设置confirm=true；应用当前草稿并保留重复请求回执。"""
        return gateway.apply_edit_proposal(task_id, proposal_id, request_id, expected_revision, confirm)

    @server.tool()
    async def cutroom_undo_edit_proposal(task_id: str, proposal_id: str, request_id: str, expected_revision: str, confirm: bool = False) -> dict[str, Any]:
        """经明确确认撤销本次修改；拒绝覆盖后来的人工作业，不恢复旧批准。"""
        return gateway.undo_edit_proposal(task_id, proposal_id, request_id, expected_revision, confirm)

    @server.tool()
    async def cutroom_build_rhythm_advice(task_id: str, expected_revision: str) -> dict[str, Any]:
        """本地结构节奏建议草稿，不改口播、倍速或镜头，不调用模型。"""
        return gateway.build_rhythm_advice(task_id, expected_revision)

    @server.tool()
    async def cutroom_get_rhythm_advice(task_id: str, report_id: str | None = None) -> dict[str, Any]:
        """读取本任务节奏建议；旧报告标记失效，不代表业务通过。"""
        return gateway.get_rhythm_advice(task_id, report_id)

    @server.tool()
    async def cutroom_preview_rhythm_edit(task_id: str, report_id: str, suggestion_id: str, expected_revision: str) -> dict[str, Any]:
        """只读查看白名单修改草稿的前后差异和依赖影响，不应用修改。"""
        return gateway.preview_rhythm_edit(task_id, report_id, suggestion_id, expected_revision)

    @server.tool()
    async def cutroom_locate_rhythm_suggestion(task_id: str, report_id: str, suggestion_id: str, expected_revision: str) -> dict[str, Any]:
        """校验建议仍对应当前版本，返回当前镜头及时间位置，不修改任务。"""
        return gateway.locate_rhythm_suggestion(task_id, report_id, suggestion_id, expected_revision)

    @server.tool()
    async def cutroom_build_reshoot_list(task_id: str, expected_revision: str) -> dict[str, Any]:
        """本地核验素材缺口，生成草稿清单；不调用模型、不替换镜头。"""
        return gateway.build_reshoot_list(task_id, expected_revision)

    @server.tool()
    async def cutroom_get_reshoot_list(task_id: str, report_id: str | None = None) -> dict[str, Any]:
        """读取本任务缺口清单或历史报告；不是业务批准。"""
        return gateway.get_reshoot_list(task_id, report_id)

    @server.tool()
    async def cutroom_export_reshoot_list(task_id: str, report_id: str, expected_revision: str, format: str = 'text') -> dict[str, Any]:
        """校验当前任务及素材版本，返回可复制/本地保存的文本或JSON。"""
        return gateway.export_reshoot_list(task_id, report_id, expected_revision, format)

    @server.tool(structured_output=True)
    async def cutroom_get_agent_context(task_id: str) -> dict[str, Any]:
        """Return the bounded planning context and current input digest."""
        return gateway.get_agent_context(task_id)

    @server.tool(structured_output=True)
    async def cutroom_get_packaging_request(task_id: str, request_id: str) -> dict[str, Any]:
        """Claim a user-requested packaging suggestion; returns bounded context and reviewed prompt."""
        return gateway.get_packaging_request(task_id=task_id, request_id=request_id, source_agent=bound_client_id)

    @server.tool(structured_output=True)
    async def cutroom_submit_packaging_result(task_id: str, request_id: str, response: dict[str, Any]) -> dict[str, Any]:
        """Validate the claimed packaging result and create a separate draft; never approve a gate."""
        return gateway.submit_packaging_result(task_id=task_id, request_id=request_id, response=response, source_agent=bound_client_id)

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
