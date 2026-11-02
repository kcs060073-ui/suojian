import importlib
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from media_catalog.agent_gateway import AgentGateway
from media_catalog.database import CatalogDatabase


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


class FakeGateway:
    def __getattr__(self, name):
        def call(*args, **kwargs):
            if name == "get_task" and args == ("missing",):
                return {
                    "ok": False,
                    "data": None,
                    "error": {
                        "code": "TASK_NOT_FOUND",
                        "message": "任务不存在",
                        "retryable": False,
                        "nextAction": None,
                        "fields": [],
                    },
                }
            return {
                "ok": True,
                "data": {"method": name, "args": args, "kwargs": kwargs},
                "error": None,
            }

        return call


class StatusGateway(FakeGateway):
    def get_system_status(self):
        actions = [
            "build_publish_package",
            "recommend_storyboard",
            "render_and_qa",
            "sync_sources",
        ]
        return {
            "ok": True,
            "data": {
                "method": "get_system_status",
                "agentActions": actions,
            },
            "error": None,
        }


def _server_factory():
    try:
        module = importlib.import_module("cutroom_mcp.server")
    except ModuleNotFoundError:
        raise AssertionError("cutroom_mcp.server must exist") from None
    return module.create_mcp_server


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_discovery_is_exact_and_gate_safe(self):
        server = _server_factory()(FakeGateway())
        async with Client(server) as client:
            tools = await client.list_tools()

        self.assertEqual({item.name for item in tools.tools}, EXPECTED_TOOLS)
        self.assertTrue(
            {
                "lock_brief",
                "lock_picture",
                "confirm_packaging",
                "approve_review",
                "save_credentials",
            }.isdisjoint({item.name for item in tools.tools})
        )

    async def test_structured_tool_result_preserves_gateway_error(self):
        server = _server_factory()(FakeGateway())
        async with Client(server) as client:
            result = await client.call_tool(
                "cutroom_get_task",
                {"task_id": "missing"},
            )

        self.assertEqual(
            result.structured_content["error"]["code"],
            "TASK_NOT_FOUND",
        )

    async def test_submit_and_action_tools_forward_only_typed_arguments(self):
        server = _server_factory()(FakeGateway())
        async with Client(server) as client:
            submitted = await client.call_tool(
                "cutroom_submit_planning_artifact",
                {
                    "task_id": "task-1",
                    "artifact_type": "digital_storyboard",
                    "schema_version": 1,
                    "input_digest": "digest",
                    "content": {"brief": {"broll": []}, "plan": {}},
                },
            )
            action = await client.call_tool(
                "cutroom_start_task_action",
                {
                    "task_id": "task-1",
                    "action": "render_and_qa",
                },
            )

        submit_data = submitted.structured_content["data"]
        self.assertEqual(submit_data["method"], "submit_planning_artifact")
        self.assertEqual(submit_data["kwargs"]["source_agent"], "codex")
        self.assertNotIn("project_root", submit_data["kwargs"])
        self.assertNotIn("database", submit_data["kwargs"])
        action_data = action.structured_content["data"]
        self.assertEqual(action_data["method"], "start_task_action")
        self.assertNotIn("command", action_data["kwargs"])

    async def test_submit_schema_hides_source_agent_and_server_injects_identity(self):
        for client_id in ("codex", "workbuddy", "doubao_work"):
            server = _server_factory()(FakeGateway(), client_id=client_id)
            async with Client(server) as client:
                tools = await client.list_tools()
                submit_tool = next(
                    item
                    for item in tools.tools
                    if item.name == "cutroom_submit_planning_artifact"
                )
                schema = submit_tool.model_dump(by_alias=True)["inputSchema"]
                self.assertNotIn("source_agent", schema["properties"])
                submitted = await client.call_tool(
                    "cutroom_submit_planning_artifact",
                    {
                        "task_id": "task-1",
                        "artifact_type": "digital_storyboard",
                        "schema_version": 1,
                        "input_digest": "digest",
                        "content": {"brief": {"broll": []}, "plan": {}},
                    },
                )

            self.assertEqual(
                submitted.structured_content["data"]["kwargs"]["source_agent"],
                client_id,
            )

    async def test_status_reports_bound_client_without_mutating_gateway_payload(self):
        server = _server_factory()(FakeGateway(), client_id="workbuddy")

        async with Client(server) as client:
            result = await client.call_tool("cutroom_get_system_status", {})

        self.assertEqual(result.structured_content["data"]["clientId"], "workbuddy")
        self.assertEqual(
            result.structured_content["data"]["method"],
            "get_system_status",
        )

    async def test_status_distinguishes_mcp_tools_from_allowed_task_actions(self):
        server = _server_factory()(StatusGateway(), client_id="doubao_work")

        async with Client(server) as client:
            result = await client.call_tool("cutroom_get_system_status", {})

        data = result.structured_content["data"]
        actions = [
            "build_publish_package",
            "recommend_storyboard",
            "render_and_qa",
            "sync_sources",
        ]
        self.assertEqual(data["mcpToolCount"], 12)
        self.assertEqual(data["mcpTools"], sorted(EXPECTED_TOOLS))
        self.assertEqual(data["allowedTaskActionCount"], 4)
        self.assertEqual(data["allowedTaskActions"], actions)
        self.assertEqual(data["agentActions"], actions)

    def test_unknown_client_identity_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "MCP_CLIENT_INVALID"):
            _server_factory()(FakeGateway(), client_id="doubao")

    async def test_real_database_tool_runs_on_server_event_loop_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = CatalogDatabase.open(root / "catalog.sqlite")
            try:
                db.migrate()
                server = _server_factory()(AgentGateway(db, root))
                async with Client(server) as client:
                    result = await client.call_tool(
                        "cutroom_get_system_status",
                        {},
                    )
            finally:
                db.close()

        self.assertTrue(result.structured_content["ok"], result.structured_content)
        self.assertEqual(
            result.structured_content["data"]["database"],
            "available",
        )


if __name__ == "__main__":
    unittest.main()
