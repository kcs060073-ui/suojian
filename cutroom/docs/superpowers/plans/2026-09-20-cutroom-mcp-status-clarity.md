# CUTROOM MCP Status Clarity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 CUTROOM MCP 状态明确区分 12 个 MCP 工具与 4 个允许任务动作，并把三端连接方法写入用户可见手册。

**Architecture:** MCP 服务端维护唯一工具名常量，并在成功的系统状态 payload 中注入工具清单、工具数量、允许动作清单和允许动作数量；保留旧字段兼容既有调用。普通同事手册和内置帮助承担首次连接与日常使用，维护手册保留探针、回滚和安全细节，README 提供正确入口顺序。

**Tech Stack:** Python 3.11、MCP Python SDK、unittest、Markdown、静态 HTML

**Spec:** `docs/superpowers/specs/2026-09-20-cutroom-mcp-status-clarity-design.md`

## Global Constraints

- 保留 `agentActions`，不得破坏现有客户端。
- MCP 工具仍固定为现有 12 个，不新增业务权限，不增加人工门禁绕过能力。
- 任何文档、测试输出和 Git 文件不得包含真实 Authorization Token。
- WorkBuddy 继续使用 `stdio`；豆包工作继续使用仅监听 `127.0.0.1:8766` 的 Streamable HTTP。

## Review Focus

- 状态工具失败时不得把成功态元数据注入错误 payload。
- 工具清单常量与真实注册工具发生漂移时测试必须失败。
- `agentActions` 缺失或不是列表时，兼容字段处理不得抛出异常。
- 三种客户端身份必须只改变 `clientId`，工具和动作口径保持一致。
- 文档不得指导用户将真实 Authorization Token 截图、分享或提交 Git。

---

### Task 1: 明确 MCP 状态契约

**Files:**
- Modify: `tests/test_mcp_server.py`
- Modify: `cutroom_mcp/server.py`

**Interfaces:**
- Consumes: `AgentGateway.get_system_status()` 返回的 `{"ok", "data", "error"}` 和既有 `data.agentActions`。
- Produces: `data.mcpToolCount: int`、`data.mcpTools: list[str]`、`data.allowedTaskActionCount: int`、`data.allowedTaskActions: list[str]`，并保留 `data.agentActions`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_mcp_server.py` 增加一个真实 MCP 调用测试，手工断言 `mcpToolCount == 12`、`mcpTools == sorted(EXPECTED_TOOLS)`、四个允许动作及兼容字段。

- [ ] **Step 2: 运行测试确认 RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_server.McpServerTests.test_status_distinguishes_mcp_tools_from_allowed_task_actions -v`

Expected: FAIL，原因是 `mcpToolCount` 等新字段尚不存在。

- [ ] **Step 3: 最小实现**

在 `cutroom_mcp/server.py` 定义不可变 `MCP_TOOL_NAMES`，让 `_status_for_client()` 仅在成功字典 payload 中注入四个新字段和 `clientId`；允许动作从现有 `agentActions` 复制，非列表时按空列表处理。

- [ ] **Step 4: 运行 MCP 测试确认 GREEN**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_server -v`

Expected: 该模块全部 PASS，工具发现仍严格等于 12 个。

### Task 2: 同步三端操作说明

**Files:**
- Modify: `docs/user-guide/cutroom-user-guide.md`
- Modify: `web-ui/public/help.html`
- Modify: `docs/operations/cutroom-agent-mcp-clients.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1 的状态字段和现行安装脚本。
- Produces: 面向普通同事的首次连接、每天启动、验证、安全与排障说明，以及维护人员的准确验收口径。

- [ ] **Step 1: 更新普通同事手册与内置帮助**

增加 WorkBuddy 信任/重启、豆包先启动后显示配置、Authorization 保密、验证提示词及 12/4 口径表；将内置帮助中的 Codex 单端表述改为三端。

- [ ] **Step 2: 更新维护手册和 README**

维护手册补充连接器字段映射、WorkBuddy 信任门禁和新状态字段；README 修正豆包脚本顺序并链接详细手册。

- [ ] **Step 3: 文档自检**

Run: `git diff --check`

Expected: exit 0，无空白错误；diff 中无真实 Bearer Token。

### Task 3: 全量验证

**Files:**
- Verify only

**Interfaces:**
- Consumes: Task 1 与 Task 2 的全部修改。
- Produces: 可发布的测试与文档证据。

- [ ] **Step 1: 运行 Python 全量测试**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部 PASS。

- [ ] **Step 2: 运行 TypeScript 与 Web 测试**

Run: `npm test`

Expected: 全部 PASS。

- [ ] **Step 3: 最终安全与差异检查**

Run: `git diff --check` 与 `git status --short`

Expected: 仅包含本计划文件、MCP 状态代码、测试和四份文档；不存在真实 Token。
