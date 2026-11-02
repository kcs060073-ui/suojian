# CUTROOM Agent MCP 客户端操作手册

适用客户端：Codex、WorkBuddy、豆包工作 Agent  
适用系统：Windows 64 位  
传输方式：Codex / WorkBuddy 使用本地 `stdio`；豆包工作使用仅回环的 Streamable HTTP

## 1. 能力边界

三个客户端共用同一个 CUTROOM MCP Server 和同一组 12 个工具。当前客户端的模型负责文案分析、重点词和分镜等文本规划；CUTROOM 继续负责素材、任务账本、队列、豆包 TTS、Seedance、FFmpeg、质检和发布包。

客户端身份由 MCP 服务端启动参数固定注入：

- Codex：`--client codex`
- WorkBuddy：`--client workbuddy`
- 豆包工作：`--client doubao_work`

模型看不到也不能提交 `source_agent`。该字段只由服务端写入规划产物，用于审计来源，不增加任何业务权限。

以下四道人工作业门禁只能在 CUTROOM 页面中确认：

1. 文案锁定；
2. 画面顺序锁定；
3. 包装确认；
4. 完整观看审片。

TTS、Seedance 等付费动作仍要求显式费用确认。状态查询、重连、上下文读取和规划提交都不得自动触发付费调用。

## 2. 前置检查

在 CUTROOM 项目根目录运行：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client codex
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client workbuddy
```

两条 stdio 探测都必须返回当前身份、`toolCount: 12` 和 `ok: true`。豆包工作 HTTP 探测见第 5 节。探测失败时先修复本地 Python、`requirements-mcp.txt` 或路径问题，不进入客户端配置。

## 3. Codex

预览并安装：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-codex-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-codex-mcp.ps1
```

新配置显式携带 `--client codex`。旧配置没有该参数时仍按 `codex` 运行。

安装与卸载都需要项目的 `.venv\Scripts\python.exe`（Python 3.11 或更新版本）来解析并校验 TOML。编辑前后会比较其他服务及配置；数组表、带点号的引号键和多行字符串会保留。如果现有配置使用无法安全替换的内联或点号赋值形式，脚本会报错并保留原文件，请维护人员先检查配置结构后再运行。不要通过删除整个 `config.toml` 解决。

卸载：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-codex-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-codex-mcp.ps1
```

卸载只移除 Codex 中的 `cutroom` 服务项，不删除 CUTROOM、SQLite、任务、素材或凭据。

## 4. WorkBuddy

默认配置文件为 `%USERPROFILE%\.workbuddy\mcp.json`。安装前先保留一个同目录备份，然后执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-workbuddy-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-workbuddy-mcp.ps1
```

WorkBuddy 只修改 `mcpServers.cutroom`：

- 保留 `zhaoshang_wechat_bridge`；
- 保留其他 MCP 服务和未知顶层字段；
- Dry Run 只显示 CUTROOM 项，不显示其他服务的命令、环境变量或凭据；
- 原文件不是有效 JSON 时停止，不覆盖；
- 重复安装只更新同一个 `cutroom` 项。

安装脚本只把 `cutroom` 写入 WorkBuddy 配置，不会替用户信任连接器，也不会让已经打开的旧会话自动获得工具。安装后：

1. 打开 WorkBuddy 的连接器或 MCP 管理页面；
2. 找到 `cutroom` 并点击“信任”，确认已启用；
3. 完全退出并重新打开 WorkBuddy，或重新加载连接器后新建会话；
4. 调用 `cutroom_get_system_status`，确认 `clientId = workbuddy`、`mcpToolCount = 12`、`allowedTaskActionCount = 4`。

若当前会话提示 `Tool ... not found`，但 `mcp.json` 中已有 `cutroom`，优先检查信任状态和会话是否在信任之前创建；不要改用直接导入 `AgentGateway` 作为 MCP 验收结果。

卸载与回滚：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-workbuddy-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-workbuddy-mcp.ps1
```

卸载只删除 `mcpServers.cutroom`。完成后检查 `zhaoshang_wechat_bridge` 和其他字段仍存在；CUTROOM 任务与规划产物不受影响。

## 5. 豆包工作 Agent

豆包工作必须通过公开的自定义连接器入口配置，不直接修改 Chromium LevelDB、IndexedDB、内部数据库或私有配置，也不使用 CDP。

先在一个单独的 PowerShell 窗口启动本地服务，并在豆包工作使用期间保持窗口开启：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start-doubao-work-mcp.ps1
```

服务只监听 `127.0.0.1:8766`，不会开放到局域网。首次启动会在 `runtime\mcp\doubao-work.token` 生成随机 Bearer Token；该文件不得提交、粘贴到日志或共享记忆。另开一个窗口，把令牌仅注入当前探针进程后运行真实协议探针：

```powershell
$env:CUTROOM_MCP_BEARER_TOKEN = (Get-Content -Raw runtime\mcp\doubao-work.token).Trim()
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client doubao_work --transport streamable-http --url http://127.0.0.1:8766/mcp
Remove-Item Env:CUTROOM_MCP_BEARER_TOKEN
```

探针必须返回 `clientId: doubao_work`、`toolCount: 12` 和 `ok: true`。然后生成只读配置描述：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\show-doubao-work-mcp-config.ps1
```

脚本只输出以下字段，不写豆包工作用户数据：

- `name = cutroom`
- `transport = http`
- `url = http://127.0.0.1:8766/mcp`
- `headers.Authorization = Bearer <本机随机令牌>`

在豆包工作“插件 · 技能 · 伙伴 → 新建自定义连接器”的公开入口中填写：

| 豆包工作字段 | 配置值 |
| --- | --- |
| 服务器名称 | `cutroom` |
| 传输类型 | `HTTP` |
| 服务器 URL | `http://127.0.0.1:8766/mcp` |
| 自定义 Header 名称 | `Authorization` |
| 自定义 Header 值 | `Bearer <本机随机令牌>`，不带 JSON 引号，保留 `Bearer` 后的一个空格 |

不要把整段 JSON 粘进单个输入框。成功后确认：

1. 发现 12 个工具；
2. `cutroom_get_system_status` 返回 `clientId = doubao_work`、`mcpToolCount = 12`、`allowedTaskActionCount = 4`；
3. 客户端重启后仍能读取同一 CUTROOM 任务。

如果豆包工作连接失败：

1. 先确认豆包 HTTP 探针已成功；
2. 检查启动服务的窗口仍然开启、端口 `8766` 未被占用、Authorization Header 完整；
3. 保留客户端版本和可见错误证据；
4. 停止配置，不修改豆包内部数据，也不要把监听地址改成 `0.0.0.0`。

关闭启动服务的 PowerShell 窗口即可停用豆包连接。豆包工作中的连接器记录可从公开管理页面禁用或删除；这不会删除 CUTROOM 任务和规划产物。

若 Authorization 曾出现在截图、聊天、日志或共享文档中，应关闭豆包 MCP 服务，删除 `runtime\mcp\doubao-work.token`，重新运行启动脚本生成新令牌，并更新连接器。令牌文件不承载任务数据，可以安全重建。

## 6. 状态数量口径

`cutroom_get_system_status` 的成功结果明确包含：

- `mcpToolCount = 12` 与 `mcpTools`：MCP 真实工具总数和完整清单；
- `allowedTaskActionCount = 4` 与 `allowedTaskActions`：`cutroom_start_task_action` 接受的四种动作；
- `agentActions`：为兼容旧客户端保留，含义仍是允许动作，不是 MCP 工具。

客户端回答“可用工具数量”时必须读取 `mcpToolCount`，不得用 `agentActions.length` 或 `allowedTaskActionCount` 代替。只有 MCP 工具发现结果和 `mcpTools` 一致，才算工具面验收通过。

## 7. 三端统一工作流

1. 调用 `cutroom_get_system_status`，确认服务和 `clientId`。
2. 调用 `cutroom_preflight_task` 做只读预检。
3. 创建 `agent` 模式任务，或读取已有 Agent 任务。
4. 在文案锁定门禁等待用户到 CUTROOM 页面确认。
5. 调用 `cutroom_get_agent_context` 取得锁定文案、素材摘要、Schema 和 `inputDigest`。
6. 当前客户端模型生成结构化规划。
7. 调用 `cutroom_submit_planning_artifact`；来源身份由服务端固定注入。
8. 只从动作白名单启动后续作业。
9. 遇到画面锁定、包装确认和完整审片时停止，提示用户进入 CUTROOM 页面。
10. 使用作业与结果查询工具恢复进度；关闭客户端不会把任务切换成独立模式。

工作台任务地址为：

```text
http://127.0.0.1:8765/#/tasks/<task-id>
```

地址中不得拼接启动密钥。

## 8. 无费用验收

真实验收不得触发 TTS 或 Seedance 付费调用。每个客户端只执行：

1. 工具发现；
2. 系统状态；
3. 隔离任务预检或读取；
4. 上下文读取；
5. 一次规划产物提交；
6. 人工门禁阻断验证；
7. 未确认费用时的拒绝验证；
8. 重启后任务恢复。

只有系统状态、数据库规划产物来源和 UI 门禁三处证据一致，才算该客户端验收通过。
