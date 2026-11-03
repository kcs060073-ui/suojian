"""Start the optional CUTROOM MCP server over stdio or loopback HTTP."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hmac
import logging
import os
from pathlib import Path
import sys
from typing import Any


class BearerAuthApp:
    """Minimal ASGI bearer guard for the loopback MCP endpoint."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._authorization = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"")
        if hmac.compare_digest(supplied, self._authorization):
            await self._app(scope, receive, send)
            return
        body = b'{"error":"MCP_AUTH_REQUIRED"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run CUTROOM MCP over stdio or loopback Streamable HTTP."
    )
    parser.add_argument(
        "--client",
        choices=("codex", "workbuddy", "doubao_work"),
        default="codex",
        help="Immutable client identity recorded on Agent planning artifacts.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="Use stdio for Codex/WorkBuddy or loopback HTTP for Doubao Work.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Loopback port for Streamable HTTP (default: 8765).",
    )
    parser.add_argument("--db", help="Explicit local catalog for an isolated candidate; default stays runtime/workbench/catalog.sqlite.")
    parser.add_argument("--workbench-origin", default="http://127.0.0.1:8765", help="Workbench origin for returned task links.")
    args = parser.parse_args(argv)
    if args.transport == "streamable-http" and args.client != "doubao_work":
        parser.error("streamable-http is reserved for --client doubao_work")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    bearer_token = ""
    if args.transport == "streamable-http":
        bearer_token = os.environ.get("CUTROOM_MCP_BEARER_TOKEN", "").strip()
        if not bearer_token:
            raise SystemExit(
                "CUTROOM_MCP_BEARER_TOKEN is required for streamable-http"
            )
    project_root = Path(__file__).resolve().parents[1]
    if (project_root / "runtime/software-updates/pending.json").exists():
        raise SystemExit("存在未完成的升级，请运行产品更新升级.bat恢复后再连接 MCP。")
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from cutroom_mcp.server import create_mcp_server
    from media_catalog.agent_gateway import AgentGateway
    from media_catalog.database import CatalogDatabase

    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    database_path = Path(args.db).resolve() if args.db else project_root / "runtime" / "workbench" / "catalog.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with CatalogDatabase.open(database_path) as db:
        db.migrate()
        gateway = AgentGateway(db, project_root, workbench_origin=args.workbench_origin)
        server = create_mcp_server(gateway, client_id=args.client)
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:
            import uvicorn

            app = server.streamable_http_app(
                streamable_http_path="/mcp",
                json_response=True,
                stateless_http=True,
                host="127.0.0.1",
            )
            uvicorn.run(
                BearerAuthApp(app, bearer_token),
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
            )


if __name__ == "__main__":
    main()
