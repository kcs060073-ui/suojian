from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "cutroom_mcp.py"
PROBE = ROOT / "scripts" / "probe-cutroom-mcp.py"


class McpEntrypointTests(unittest.TestCase):
    def run_entrypoint(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENTRYPOINT), *args],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    def test_unknown_client_is_rejected_before_server_start(self):
        completed = self.run_entrypoint("--client", "doubao")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid choice", completed.stderr)

    def test_help_lists_all_client_choices(self):
        completed = self.run_entrypoint("--help")

        self.assertEqual(completed.returncode, 0)
        for value in ("codex", "workbuddy", "doubao_work"):
            self.assertIn(value, completed.stdout)
        self.assertIn("streamable-http", completed.stdout)

    def test_http_transport_is_reserved_for_doubao_work(self):
        completed = self.run_entrypoint(
            "--client",
            "workbuddy",
            "--transport",
            "streamable-http",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("doubao_work", completed.stderr)

    def test_http_transport_requires_bearer_token(self):
        completed = self.run_entrypoint(
            "--client",
            "doubao_work",
            "--transport",
            "streamable-http",
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("CUTROOM_MCP_BEARER_TOKEN", completed.stderr)

    def test_real_stdio_probe_reports_each_bound_identity_and_twelve_tools(self):
        for client_id in ("codex", "workbuddy", "doubao_work"):
            completed = subprocess.run(
                [sys.executable, str(PROBE), "--client", client_id],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout),
                {"clientId": client_id, "toolCount": 12, "ok": True},
            )

    def test_real_streamable_http_probe_reports_doubao_identity_and_twelve_tools(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        environment = dict(os.environ)
        environment["CUTROOM_MCP_BEARER_TOKEN"] = "test-http-token"
        server = subprocess.Popen(
            [
                sys.executable,
                str(ENTRYPOINT),
                "--client",
                "doubao_work",
                "--transport",
                "streamable-http",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    _stdout, stderr = server.communicate()
                    self.fail(f"HTTP MCP exited before probe: {stderr}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("HTTP MCP did not bind its loopback port")

            endpoint = f"http://127.0.0.1:{port}/mcp"
            for authorization in (None, "Bearer wrong-token"):
                headers = {"Content-Type": "application/json"}
                if authorization is not None:
                    headers["Authorization"] = authorization
                request = Request(endpoint, data=b"{}", headers=headers, method="POST")
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(request, timeout=2)
                self.assertEqual(rejected.exception.code, 401)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROBE),
                    "--client",
                    "doubao_work",
                    "--transport",
                    "streamable-http",
                    "--url",
                    endpoint,
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout),
                {"clientId": "doubao_work", "toolCount": 12, "ok": True},
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
            server.communicate()


if __name__ == "__main__":
    unittest.main()
