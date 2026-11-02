# CUTROOM WorkBuddy and Doubao Work MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the accepted CUTROOM Agent MCP to WorkBuddy and Doubao Work while preserving one 12-tool business surface, injecting a non-spoofable client identity, and retaining every existing human and paid-action gate.

**Architecture:** Keep `AgentGateway` and the 12 tools shared. Bind `codex`, `workbuddy`, or `doubao_work` once when the MCP process starts, close over that identity in the tool handlers, and remove `source_agent` from the model-visible schema. WorkBuddy receives an atomic JSON installer. The current Doubao Work public connector accepts HTTP only, so Doubao Work receives an authenticated Streamable HTTP endpoint bound exclusively to `127.0.0.1:8766`.

**Tech Stack:** Python 3.11, MCP Python SDK `mcp>=2,<3`, `unittest`, Windows PowerShell 5.1, JSON, SQLite, existing CUTROOM Agent Gateway and release builder.

**Spec:** `docs/superpowers/specs/2026-09-18-cutroom-workbuddy-doubao-work-mcp-design.md`

## Global Constraints

- The three allowed identities are exactly `codex`, `workbuddy`, and `doubao_work`; unknown values fail before the database or transport starts.
- `source_agent` is never exposed in the public schema of `cutroom_submit_planning_artifact` and cannot be overridden by tool input.
- All clients discover the same 12 tools and receive the existing Agent Gateway result/error envelope.
- Existing Codex startup without `--client` remains compatible and resolves to `codex`.
- Existing artifact rows are not migrated or rewritten.
- WorkBuddy installation changes only `mcpServers.cutroom`, preserves `zhaoshang_wechat_bridge` and unknown fields, and never prints unrelated configuration or secrets.
- Doubao Work is configured only through its public custom-connector interface; no Chromium LevelDB, IndexedDB, internal database, CDP, or private message format is modified.
- Doubao Work loopback HTTP requires a random Bearer token and never listens on a LAN or public address.
- MCP never approves brief lock, picture lock, packaging confirmation, or full review.
- Paid TTS and Seedance actions still require explicit cost confirmation; acceptance uses no paid provider call.
- Ordinary CUTROOM installation does not register Codex, WorkBuddy, or Doubao Work automatically.
- Release archives contain scripts and docs but no user MCP config, runtime database, logs, tokens, cookies, or credentials.

---

## File Map

- `cutroom_mcp/server.py`: client-ID allowlist, immutable server binding, status projection, and injected artifact provenance.
- `scripts/cutroom_mcp.py`: `--client` argument parsing with the backward-compatible `codex` default.
- `scripts/probe-cutroom-mcp.py`: protocol-clean stdio or authenticated Streamable HTTP smoke probe.
- `scripts/install-codex-mcp.ps1`: explicit Codex identity in newly generated registrations.
- `scripts/install-workbuddy-mcp.ps1`: atomic WorkBuddy JSON registration that preserves unrelated data.
- `scripts/uninstall-workbuddy-mcp.ps1`: removal of only `mcpServers.cutroom`.
- `scripts/show-doubao-work-mcp-config.ps1`: authenticated loopback-HTTP descriptor for manual entry in the public connector UI.
- `scripts/start-doubao-work-mcp.ps1`: foreground loopback service launcher and local token generation.
- `tests/test_mcp_server.py`: visible schema, immutable identity, 12-tool parity, and status tests.
- `tests/test_mcp_entrypoint.py`: CLI parsing and subprocess-level client validation.
- `tests/test_mcp_install_scripts.py`: Codex and WorkBuddy registration safety and PowerShell 5.1 parsing.
- `tests/test_doubao_work_mcp_config.py`: exact Doubao Work descriptor and non-mutation checks.
- `tests/test_release_packaging.py`: release inclusion and private-state exclusion.
- `README.md`: short client setup entry points and mode boundary.
- `docs/operations/cutroom-agent-mcp-clients.md`: operator procedures, rollback, and real-probe decision gate.
- `docs/user-guide/cutroom-user-guide.md`: client-neutral Agent workflow and UI-only gates.
- `docs/validation/2026-09-18-cutroom-workbuddy-doubao-work-mcp-acceptance.md`: command evidence and real-client acceptance record.
- `release/release-files.json`: remains directory-based; tests prove the new scripts and docs are present in the built archive.

### Task 1: Bind an immutable client identity inside the shared MCP server

**Files:**
- Modify: `cutroom_mcp/server.py`
- Modify: `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `ALLOWED_CLIENT_IDS: frozenset[str]` with `codex`, `workbuddy`, and `doubao_work`.
- Produces: `normalize_client_id(value: str) -> str` raising `ValueError("MCP_CLIENT_INVALID: ...")` for every other value.
- Produces: `create_mcp_server(gateway: AgentGateway, *, client_id: str = "codex") -> MCPServer`.
- Preserves: the exact 12 existing tool names and all existing gateway argument/result contracts except that model input no longer includes `source_agent`.

- [ ] **Step 1: Add failing schema and identity tests**

Add these cases to `tests/test_mcp_server.py`:

```python
    async def test_submit_schema_hides_source_agent_and_server_injects_identity(self):
        for client_id in ("codex", "workbuddy", "doubao_work"):
            server = _server_factory()(FakeGateway(), client_id=client_id)
            async with Client(server) as client:
                tools = await client.list_tools()
                submit_tool = next(
                    item for item in tools.tools
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
        gateway = FakeGateway()
        server = _server_factory()(gateway, client_id="workbuddy")
        async with Client(server) as client:
            result = await client.call_tool("cutroom_get_system_status", {})
        self.assertEqual(result.structured_content["data"]["clientId"], "workbuddy")
        self.assertEqual(result.structured_content["data"]["method"], "get_system_status")

    def test_unknown_client_identity_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "MCP_CLIENT_INVALID"):
            _server_factory()(FakeGateway(), client_id="doubao")
```

Also change the existing typed-forwarding assertion to require `source_agent == "codex"` so the backward-compatible default is explicit.

- [ ] **Step 2: Run the server tests and confirm the red state**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_server -v`

Expected: FAIL because `create_mcp_server` does not accept `client_id`, the public schema still exposes `source_agent`, and status lacks `clientId`.

- [ ] **Step 3: Implement allowlisted startup identity and server-side injection**

Add this boundary to `cutroom_mcp/server.py`:

```python
ALLOWED_CLIENT_IDS = frozenset({"codex", "workbuddy", "doubao_work"})


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
    data["clientId"] = client_id
    return {**result, "data": data}


def create_mcp_server(
    gateway: AgentGateway,
    *,
    client_id: str = "codex",
) -> MCPServer:
    bound_client_id = normalize_client_id(client_id)
```

Change the status handler to:

```python
    @server.tool(structured_output=True)
    async def cutroom_get_system_status() -> dict[str, Any]:
        """Return database, queue, route, Agent capability, and client status."""
        return _status_for_client(gateway.get_system_status(), bound_client_id)
```

Change the planning tool signature and call to:

```python
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
```

- [ ] **Step 4: Run the focused server and gateway tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_server tests.test_agent_gateway tests.test_agent_artifacts -v`

Expected: PASS; each server identity reaches the gateway as `source_agent`, tool discovery remains exactly 12, and existing persistence/idempotency tests remain green.

- [ ] **Step 5: Commit the identity boundary**

```powershell
git add cutroom_mcp/server.py tests/test_mcp_server.py
git commit -m "feat: bind CUTROOM MCP client identity"
```

### Task 2: Add client-aware stdio startup and a reusable transport probe

**Files:**
- Modify: `scripts/cutroom_mcp.py`
- Create: `scripts/probe-cutroom-mcp.py`
- Create: `tests/test_mcp_entrypoint.py`
- Modify: `scripts/install-codex-mcp.ps1`
- Modify: `tests/test_mcp_install_scripts.py`

**Interfaces:**
- Produces: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace` with `--client` choices from `ALLOWED_CLIENT_IDS` and default `codex`.
- Produces: `main(argv: Sequence[str] | None = None) -> None` that passes the parsed identity into `create_mcp_server`.
- Produces: `scripts/probe-cutroom-mcp.py --client <id>` which starts the real stdio entry point, verifies 12 tools, calls status, and exits nonzero unless `clientId` matches.
- Updates: generated Codex registration explicitly uses `--client codex`; existing registrations without the argument remain valid through the entry-point default.

- [ ] **Step 1: Write failing CLI and registration tests**

Create `tests/test_mcp_entrypoint.py` with subprocess-isolated argument checks:

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "cutroom_mcp.py"


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


if __name__ == "__main__":
    unittest.main()
```

Extend the Codex installer test with:

```python
        self.assertIn("--client", output)
        self.assertIn("codex", output)
```

- [ ] **Step 2: Run the CLI and installer tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_entrypoint tests.test_mcp_install_scripts -v`

Expected: FAIL because the entry point has no parser and the generated Codex configuration has no explicit identity.

- [ ] **Step 3: Implement parsing and pass the identity into the server**

Use this structure in `scripts/cutroom_mcp.py`:

```python
import argparse
import logging
from pathlib import Path
import sys
from collections.abc import Sequence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CUTROOM MCP over stdio.")
    parser.add_argument(
        "--client",
        choices=("codex", "workbuddy", "doubao_work"),
        default="codex",
        help="Immutable client identity recorded on Agent planning artifacts.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
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
    database_path = project_root / "runtime" / "workbench" / "catalog.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with CatalogDatabase.open(database_path) as db:
        db.migrate()
        gateway = AgentGateway(db, project_root)
        create_mcp_server(gateway, client_id=args.client).run(transport="stdio")
```

In `scripts/install-codex-mcp.ps1`, generate:

```powershell
$entry = [ordered]@{
    command = $pythonPath
    args = @($serverPath, "--client", "codex")
    cwd = $projectRoot
}
```

and include the same arguments in the TOML block.

- [ ] **Step 4: Add the real stdio probe**

Implement `scripts/probe-cutroom-mcp.py` with no stdout from the child other than MCP protocol traffic:

```python
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_TOOLS = {
    "cutroom_get_system_status", "cutroom_list_tasks", "cutroom_get_task",
    "cutroom_get_agent_context", "cutroom_get_job", "cutroom_get_result_links",
    "cutroom_preflight_task", "cutroom_create_task",
    "cutroom_submit_planning_artifact", "cutroom_start_task_action",
    "cutroom_cancel_job", "cutroom_resume_job",
}


async def probe(client_id: str) -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(root / "scripts" / "cutroom_mcp.py"), "--client", client_id],
        cwd=str(root),
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {item.name for item in tools.tools}
            if names != EXPECTED_TOOLS:
                raise RuntimeError(f"MCP_TOOL_SET_MISMATCH: {sorted(names)}")
            status = await session.call_tool("cutroom_get_system_status", {})
            payload = status.structured_content
            if payload["data"]["clientId"] != client_id:
                raise RuntimeError("MCP_CLIENT_ID_MISMATCH")
            return {"clientId": client_id, "toolCount": len(names), "ok": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--client",
        choices=("codex", "workbuddy", "doubao_work"),
        default="codex",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.client)), ensure_ascii=False))


if __name__ == "__main__":
    main()
```

During implementation, verify the installed MCP SDK result attribute name once with `model_dump(by_alias=True)` and use that exact SDK field consistently in both probe and test; do not add a compatibility branch that swallows malformed results.

- [ ] **Step 5: Run all three process identities through real stdio**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_mcp_entrypoint tests.test_mcp_server tests.test_mcp_install_scripts -v
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client codex
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client workbuddy
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client doubao_work
```

Expected: tests PASS; each probe prints one JSON object with its requested `clientId`, `toolCount: 12`, and `ok: true`.

- [ ] **Step 6: Commit the entry point and stdio probe**

```powershell
git add scripts/cutroom_mcp.py scripts/probe-cutroom-mcp.py scripts/install-codex-mcp.ps1 tests/test_mcp_entrypoint.py tests/test_mcp_install_scripts.py
git commit -m "feat: add CUTROOM MCP client startup identities"
```

### Task 3: Install and uninstall WorkBuddy without exposing other configuration

**Files:**
- Create: `scripts/install-workbuddy-mcp.ps1`
- Create: `scripts/uninstall-workbuddy-mcp.ps1`
- Modify: `tests/test_mcp_install_scripts.py`

**Interfaces:**
- Produces: `install-workbuddy-mcp.ps1 [-ConfigPath <json>] [-DryRun]` defaulting to `%USERPROFILE%\.workbuddy\mcp.json`.
- Produces: `uninstall-workbuddy-mcp.ps1 [-ConfigPath <json>] [-DryRun]` with the same default.
- Writes exactly `mcpServers.cutroom = {type, command, args, cwd}` with `args = [cutroom_mcp.py, --client, workbuddy]`.
- Preserves the complete parsed JSON document except the selected `cutroom` entry and writes with UTF-8 no BOM through a same-directory atomic replacement.

- [ ] **Step 1: Add failing WorkBuddy install safety tests**

Extend `tests/test_mcp_install_scripts.py` with constants for the two new scripts and tests equivalent to:

```python
    def test_workbuddy_install_preserves_existing_servers_and_redacts_dry_run(self):
        config = self.root / "mcp.json"
        original = {
            "theme": "dark",
            "mcpServers": {
                "zhaoshang_wechat_bridge": {
                    "command": "bridge.exe",
                    "args": [],
                    "env": {"TOKEN": "TOP_SECRET_MARKER"},
                }
            },
        }
        config.write_text(json.dumps(original), encoding="utf-8")

        dry_run = self.run_script(WORKBUDDY_INSTALLER, config, "-DryRun")
        self.assertEqual(dry_run.returncode, 0)
        self.assertEqual(json.loads(config.read_text(encoding="utf-8")), original)
        preview = dry_run.stdout.decode("utf-8", errors="replace")
        self.assertIn('"cutroom"', preview)
        self.assertIn('"workbuddy"', preview)
        self.assertNotIn("TOP_SECRET_MARKER", preview)

        installed = self.run_script(WORKBUDDY_INSTALLER, config)
        self.assertEqual(installed.returncode, 0)
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(
            data["mcpServers"]["zhaoshang_wechat_bridge"],
            original["mcpServers"]["zhaoshang_wechat_bridge"],
        )
        self.assertEqual(
            data["mcpServers"]["cutroom"]["args"][-2:],
            ["--client", "workbuddy"],
        )

    def test_workbuddy_install_rejects_invalid_json_without_overwrite(self):
        config = self.root / "mcp.json"
        original = b'{"mcpServers": '
        config.write_bytes(original)
        completed = self.run_script(WORKBUDDY_INSTALLER, config)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(config.read_bytes(), original)

    def test_workbuddy_reinstall_is_idempotent_and_uninstall_removes_only_cutroom(self):
        config = self.root / "mcp.json"
        config.write_text(
            json.dumps({"mcpServers": {"other": {"command": "other.exe"}}}),
            encoding="utf-8",
        )
        self.assertEqual(self.run_script(WORKBUDDY_INSTALLER, config).returncode, 0)
        first = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(self.run_script(WORKBUDDY_INSTALLER, config).returncode, 0)
        second = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(first, second)
        self.assertEqual(self.run_script(WORKBUDDY_UNINSTALLER, config).returncode, 0)
        removed = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(set(removed["mcpServers"]), {"other"})
```

Add both scripts to the PowerShell 5.1 parser test and add a byte-for-byte no-op test when uninstall finds no `cutroom` entry.

- [ ] **Step 2: Run the install-script tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_install_scripts -v`

Expected: FAIL because the WorkBuddy scripts do not exist.

- [ ] **Step 3: Implement the WorkBuddy installer**

Use this exact entry and fail-closed parse flow in `scripts/install-workbuddy-mcp.ps1`:

```powershell
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE ".workbuddy\mcp.json"),
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$pythonPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".venv\Scripts\python.exe"))
$serverPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "scripts\cutroom_mcp.py"))
$targetPath = [System.IO.Path]::GetFullPath($ConfigPath)
$entry = [ordered]@{
    type = "stdio"
    command = $pythonPath
    args = @($serverPath, "--client", "workbuddy")
    cwd = $projectRoot
}

if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
    $raw = [System.IO.File]::ReadAllText($targetPath)
    try { $document = $raw | ConvertFrom-Json }
    catch { throw "WorkBuddy MCP JSON could not be parsed: $targetPath" }
} else {
    $document = [pscustomobject]@{}
}
if (-not $document.PSObject.Properties["mcpServers"]) {
    $document | Add-Member -MemberType NoteProperty -Name mcpServers -Value ([pscustomobject]@{})
}
$document.mcpServers | Add-Member -MemberType NoteProperty -Name cutroom -Value ([pscustomobject]$entry) -Force
$proposed = ($document | ConvertTo-Json -Depth 32) + [Environment]::NewLine
$preview = ([ordered]@{ mcpServers = [ordered]@{ cutroom = $entry } } | ConvertTo-Json -Depth 8)
```

Copy the already-tested `Write-AtomicUtf8` function from `install-codex-mcp.ps1`, including same-directory temporary/backup cleanup. In `-DryRun`, print only `$preview`; never print `$raw`, `$document`, or `$proposed`.

- [ ] **Step 4: Implement the WorkBuddy uninstaller**

In `scripts/uninstall-workbuddy-mcp.ps1`, parse existing JSON before any write, return without writing if the file or `mcpServers.cutroom` is absent, remove only the selected property, and use the same atomic writer:

```powershell
if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
    Write-Output "[CUTROOM] WorkBuddy MCP config does not exist; nothing to remove."
    exit 0
}
$raw = [System.IO.File]::ReadAllText($targetPath)
try { $document = $raw | ConvertFrom-Json }
catch { throw "WorkBuddy MCP JSON could not be parsed: $targetPath" }
if (
    -not $document.PSObject.Properties["mcpServers"] -or
    -not $document.mcpServers.PSObject.Properties["cutroom"]
) {
    Write-Output "[CUTROOM] WorkBuddy MCP server 'cutroom' is not registered."
    exit 0
}
$document.mcpServers.PSObject.Properties.Remove("cutroom")
$proposed = ($document | ConvertTo-Json -Depth 32) + [Environment]::NewLine
```

For `-DryRun`, print only that `mcpServers.cutroom` will be removed; do not serialize the remaining document.

- [ ] **Step 5: Run registration, redaction, invalid-JSON, and rollback tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_install_scripts -v`

Expected: PASS under Windows PowerShell 5.1; the bridge config and unknown fields survive, dry runs omit the secret marker, reinstall is idempotent, invalid JSON is byte-preserved, and uninstall touches no CUTROOM data.

- [ ] **Step 6: Commit the WorkBuddy adapter**

```powershell
git add scripts/install-workbuddy-mcp.ps1 scripts/uninstall-workbuddy-mcp.ps1 tests/test_mcp_install_scripts.py
git commit -m "feat: add safe WorkBuddy MCP registration"
```

### Task 4: Generate a safe Doubao Work loopback-HTTP connector

> **2026-09-18 implementation amendment:** The real public connector form and current product documentation expose HTTP URL plus custom Headers, not `command` / `args` / `cwd`. The original stdio-first substeps below are superseded by the implemented contract: `http://127.0.0.1:8766/mcp`, mandatory Bearer authentication, `--client doubao_work`, a foreground launcher, real Streamable HTTP tool discovery, and no internal app-data writes.

**Files:**
- Create: `scripts/show-doubao-work-mcp-config.ps1`
- Create: `tests/test_doubao_work_mcp_config.py`

**Interfaces:**
- Produces: `show-doubao-work-mcp-config.ps1` writing one UTF-8 JSON descriptor to stdout and making no filesystem or application-state change.
- Descriptor keys are exactly `name`, `transport`, `command`, `args`, and `cwd`.
- Descriptor identity is exactly `args[-2:] == ["--client", "doubao_work"]`.
- The script exits nonzero when the project Python or MCP entry point is missing.

- [ ] **Step 1: Write the failing descriptor tests**

Create `tests/test_doubao_work_mcp_config.py`:

```python
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "show-doubao-work-mcp-config.ps1"


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 only")
class DoubaoWorkMcpConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.powershell = shutil.which("powershell.exe")
        if self.powershell is None:
            self.skipTest("Windows PowerShell 5.1 is unavailable")

    def test_descriptor_is_exact_local_stdio_and_does_not_touch_app_data(self):
        before = {
            path: path.stat().st_mtime_ns
            for path in (Path(os.environ["LOCALAPPDATA"]) / "DoubaoWork").glob("*")
            if path.is_file()
        }
        completed = subprocess.run(
            [
                self.powershell, "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        descriptor = json.loads(completed.stdout.decode("utf-8-sig"))
        self.assertEqual(
            set(descriptor),
            {"name", "transport", "command", "args", "cwd"},
        )
        self.assertEqual(descriptor["name"], "cutroom")
        self.assertEqual(descriptor["transport"], "stdio")
        self.assertEqual(descriptor["args"][-2:], ["--client", "doubao_work"])
        self.assertEqual(Path(descriptor["cwd"]), ROOT)
        after = {
            path: path.stat().st_mtime_ns
            for path in (Path(os.environ["LOCALAPPDATA"]) / "DoubaoWork").glob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
```

Also parse the PowerShell source with the same PowerShell 5.1 parser command used by `test_mcp_install_scripts.py`, and assert the source contains none of `LevelDB`, `IndexedDB`, `--remote-debugging-port`, or `D:\\DoubaoWork`.

- [ ] **Step 2: Run the descriptor tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_doubao_work_mcp_config -v`

Expected: FAIL because the descriptor script does not exist.

- [ ] **Step 3: Implement the read-only descriptor generator**

Create `scripts/show-doubao-work-mcp-config.ps1`:

```powershell
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$pythonPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".venv\Scripts\python.exe"))
$serverPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "scripts\cutroom_mcp.py"))
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "CUTROOM Python environment was not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw "CUTROOM MCP entry point was not found: $serverPath"
}
$descriptor = [ordered]@{
    name = "cutroom"
    transport = "stdio"
    command = $pythonPath
    args = @($serverPath, "--client", "doubao_work")
    cwd = $projectRoot
}
$descriptor | ConvertTo-Json -Depth 8
```

The generator must not search for the Doubao Work install, inspect user-data directories, read a connector database, launch the app, or write a file.

- [ ] **Step 4: Run the descriptor and stdio-identity tests green**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_doubao_work_mcp_config tests.test_mcp_entrypoint -v
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\show-doubao-work-mcp-config.ps1
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client doubao_work
```

Expected: tests PASS; the script prints a single credential-free JSON object; the probe reports `doubao_work` and 12 tools.

- [ ] **Step 5: Commit the Doubao Work stdio descriptor**

```powershell
git add scripts/show-doubao-work-mcp-config.ps1 tests/test_doubao_work_mcp_config.py
git commit -m "feat: add Doubao Work MCP connector descriptor"
```

### Task 5: Document all client operations and prove release-package boundaries

**Files:**
- Create: `docs/operations/cutroom-agent-mcp-clients.md`
- Modify: `README.md`
- Modify: `docs/user-guide/cutroom-user-guide.md`
- Modify: `tests/test_mcp_install_scripts.py`
- Modify: `tests/test_release_packaging.py`
- Verify: `release/release-files.json`

**Interfaces:**
- Produces: one operator guide with separate Codex, WorkBuddy, and Doubao Work setup, status check, rollback, and no-paid-call acceptance sections.
- Preserves: regular release installation does not install MCP dependencies or register any client.
- Proves: the release ZIP contains all five client scripts (`cutroom_mcp.py`, probe, WorkBuddy install/uninstall, Doubao descriptor) and excludes client configuration/state paths.

- [ ] **Step 1: Add failing documentation and release assertions**

Extend the documentation test with these exact required phrases:

```python
        for phrase in (
            "--client codex",
            "--client workbuddy",
            "--client doubao_work",
            "WorkBuddy 只修改 mcpServers.cutroom",
            "豆包工作必须通过公开的自定义连接器入口配置",
            "source_agent 由 MCP 服务端固定注入",
            "真实验收不得触发 TTS 或 Seedance 付费调用",
        ):
            self.assertIn(phrase, documents)
```

Extend `tests/test_release_packaging.py` after building the ZIP:

```python
        for relative in (
            "scripts/cutroom_mcp.py",
            "scripts/probe-cutroom-mcp.py",
            "scripts/install-workbuddy-mcp.ps1",
            "scripts/uninstall-workbuddy-mcp.ps1",
            "scripts/show-doubao-work-mcp-config.ps1",
            "docs/operations/cutroom-agent-mcp-clients.md",
        ):
            self.assertIn(prefix + relative, names)
        self.assertFalse(any(".workbuddy" in name.lower() for name in names))
        self.assertFalse(any(".doubaowork" in name.lower() for name in names))
        self.assertFalse(any(name.lower().endswith("mcp.json") for name in names))
```

- [ ] **Step 2: Run the docs and release tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_install_scripts tests.test_release_packaging -v`

Expected: FAIL because the new procedures and release assertions are not yet satisfied.

- [ ] **Step 3: Write the client-neutral operating guide and update entry docs**

The new guide must contain these executable commands:

```powershell
# Codex registration
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-codex-mcp.ps1 -DryRun

# WorkBuddy registration and rollback
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-workbuddy-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-workbuddy-mcp.ps1 -DryRun

# Doubao Work public-UI descriptor
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\show-doubao-work-mcp-config.ps1

# Transport checks
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client codex
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client workbuddy
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client doubao_work
```

Document the shared ten-step Agent workflow from the spec, the four UI-only human gates, explicit paid confirmation, client identity semantics, safe WorkBuddy rollback, Doubao public-UI-only rule, and the decision gate: if Doubao Work rejects local stdio after configuration errors are excluded, capture the visible error and stop; do not edit its private state or start an unauthenticated HTTP server.

- [ ] **Step 4: Confirm release directory coverage rather than adding per-script allowlist drift**

Verify `release/release-files.json` already includes `scripts`, `docs/operations`, and `cutroom_mcp`. Do not add per-file entries while those directory contracts remain present. Keep `requirements-mcp.txt` optional and keep `release/install.ps1` free of automatic MCP registration.

- [ ] **Step 5: Run documentation and release-package tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_install_scripts tests.test_release_packaging -v`

Expected: PASS; the built archive contains the new scripts and guide, contains no client configuration/state, and the normal installer remains MCP-optional.

- [ ] **Step 6: Commit documentation and release assertions**

```powershell
git add README.md docs/operations/cutroom-agent-mcp-clients.md docs/user-guide/cutroom-user-guide.md tests/test_mcp_install_scripts.py tests/test_release_packaging.py
git commit -m "docs: add CUTROOM MCP client operations"
```

### Task 6: Run security regression and real-client acceptance without paid calls

**Files:**
- Create: `docs/validation/2026-09-18-cutroom-workbuddy-doubao-work-mcp-acceptance.md`
- Modify only if a discovered product defect requires it: files from Tasks 1-5 plus their focused tests.

**Interfaces:**
- Consumes: the three stdio identities, WorkBuddy installer/uninstaller, Doubao descriptor, the existing isolated Agent task fixtures, and CUTROOM UI human gates.
- Produces: evidence for tool count, `clientId`, artifact `source_agent`, config preservation, no-paid-call behavior, restart recovery, and Codex regression.
- Decision result: `doubao_stdio_supported` or `doubao_stdio_unsupported_with_evidence`. The latter ends this phase and triggers a separate authenticated-loopback spec/plan; it does not authorize private-state modification.

- [ ] **Step 1: Run focused identity, install, security, and release regression**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_mcp_server tests.test_mcp_entrypoint tests.test_mcp_install_scripts tests.test_doubao_work_mcp_config tests.test_agent_gateway tests.test_agent_context tests.test_agent_artifacts tests.test_credentials tests.test_release_packaging -v
```

Expected: PASS with no skipped Windows tests on this machine.

- [ ] **Step 2: Run the existing complete Python, TypeScript, and UI regression commands**

Run the repository's documented full verification commands from `README.md` and the prior acceptance document. Record exact counts, elapsed time, and any environment-only PATH additions in the new acceptance document. A failing test must be diagnosed and fixed with its focused regression before continuing to real clients.

- [ ] **Step 3: Install WorkBuddy configuration with a recoverable before-image and verify preservation**

Execute in this order:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-workbuddy-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-workbuddy-mcp.ps1
```

Before the real write, copy `%USERPROFILE%\.workbuddy\mcp.json` to a timestamped sibling `.bak` file. After installation, parse both JSON files and record only these non-sensitive assertions: `zhaoshang_wechat_bridge` is structurally equal before/after, unknown top-level keys are equal, one `cutroom` entry exists, and its identity is `workbuddy`. Do not place either full config or its environment values in the acceptance document.

- [ ] **Step 4: Validate WorkBuddy through its MCP UI**

Reload WorkBuddy connector discovery, then use a no-cost isolated Agent task to record:

1. `cutroom` is visible alongside `zhaoshang_wechat_bridge`.
2. Tool discovery contains exactly the 12 expected names.
3. `cutroom_get_system_status` returns `clientId = workbuddy`.
4. Context retrieval and one valid planning artifact submission succeed.
5. The stored artifact row reports `source_agent = workbuddy`.
6. The next UI-only gate returns `awaiting_human_confirmation` instead of advancing.
7. A paid action without confirmation returns `PAID_CONFIRMATION_REQUIRED` and creates no paid job.

Do not invoke a confirmed TTS, Seedance, or other paid production action.

- [ ] **Step 5: Validate Doubao Work through the public custom-connector UI**

Generate the descriptor, open “技能·连接器·伙伴”, and enter only the generated `name`, `transport`, `command`, `args`, and `cwd`. Record:

1. Whether the UI accepts a local stdio custom connector.
2. If accepted, exactly 12 discovered tools and `clientId = doubao_work`.
3. One no-cost isolated context retrieval and planning submission.
4. Stored `source_agent = doubao_work`.
5. Correct stop at the next human gate and correct rejection of an unconfirmed paid action.
6. After restarting Doubao Work, successful re-read of the same task and artifact.

If the UI rejects local stdio, first re-check path existence, argument quoting, cwd, MCP dependencies, and the successful `probe-cutroom-mcp.py --client doubao_work` result. If rejection remains, capture the exact visible error and app version, set the decision result to `doubao_stdio_unsupported_with_evidence`, leave private app state untouched, and stop this phase before claiming completion.

- [ ] **Step 6: Regress Codex and verify source separation**

Run the Codex stdio probe and one no-cost status/context call. Query the isolated artifact ledger through existing CUTROOM code and record that the three artifact records retain their actual source values and that no tool schema contains `source_agent`. Confirm the historical Codex acceptance path still reports 12 tools and `clientId = codex`.

- [ ] **Step 7: Prove WorkBuddy rollback is scoped**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-workbuddy-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-workbuddy-mcp.ps1
```

Verify only `mcpServers.cutroom` disappears, the existing bridge and unknown keys remain, and CUTROOM tasks/artifacts still exist. Reinstall after the rollback proof only if Peter wants WorkBuddy left enabled.

- [ ] **Step 8: Write the acceptance document with an explicit outcome**

Record versions, commit, commands, pass counts, real-client observations, no-paid-call boundary, sanitized config-preservation assertions, source identity evidence, restart behavior, and one of these conclusions:

```text
ACCEPTED: WorkBuddy and Doubao Work both use native local stdio and satisfy the full design completion definition.
```

or:

```text
PHASE ACCEPTED / DOUBAO BLOCKED: Core identity and WorkBuddy are accepted; Doubao Work 2.29.10 rejects public custom local stdio after configuration checks. The attached visible error evidence authorizes a separate authenticated 127.0.0.1 Streamable HTTP design, not private-state modification.
```

- [ ] **Step 9: Run final diff and verification checks**

Run:

```powershell
git diff --check
git status --short
.venv\Scripts\python.exe -m unittest tests.test_mcp_server tests.test_mcp_entrypoint tests.test_mcp_install_scripts tests.test_doubao_work_mcp_config tests.test_release_packaging -v
```

Expected: no whitespace errors, only intended acceptance-document or defect-fix changes, and all focused tests PASS.

- [ ] **Step 10: Commit the verified acceptance evidence**

```powershell
git add docs/validation/2026-09-18-cutroom-workbuddy-doubao-work-mcp-acceptance.md
git commit -m "test: validate WorkBuddy and Doubao Work MCP clients"
```

If implementation files changed during defect correction, include only each corrected file and its focused regression test in a preceding defect-specific commit.

---

## Self-Review Result

- Spec coverage: Tasks 1-6 cover immutable identity, all 12 tools, Codex compatibility, WorkBuddy atomic install/rollback, Doubao public connector configuration, human/paid gates, release packaging, restart recovery, and real-client evidence.
- Conditional transport boundary: authenticated loopback HTTP is intentionally not implemented without the real failure evidence required by the approved spec; a failed native probe creates a second design/plan rather than an unreviewed architecture branch.
- Placeholder scan: every implementation task names exact files, commands, assertions, interfaces, and expected outcomes; no implementation placeholder remains.
- Type consistency: `client_id` is the startup/server variable, MCP status exposes camel-case `clientId`, and SQLite artifact persistence continues to use `source_agent` with the exact stable values `codex`, `workbuddy`, and `doubao_work`.
