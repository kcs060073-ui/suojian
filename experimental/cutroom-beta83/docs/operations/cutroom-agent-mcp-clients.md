# 剪辑工作台 Agent MCP 客户端操作手册

适用客户端：Codex、WorkBuddy、豆包工作 Agent  
适用系统：Windows 64 位  
传输方式：Codex / WorkBuddy 使用本地 `stdio`；豆包工作使用仅回环的 Streamable HTTP

普通使用者先看[操作手册中的 WorkBuddy / 豆包接入步骤](../user-guide/cutroom-user-guide.md#agent-mode)。本页供维护人员检查协议、任务库绑定、权限和回滚。

**从实际使用的剪辑工作台安装目录运行脚本。安装器与豆包启动器支持 `-DatabasePath` 和 `-WorkbenchOrigin`，并把任务库及工作台地址显式传给服务；省略时使用该安装目录的默认库和端口 8765。独立库或自定义端口必须提供这两个参数，避免重装后连到另一份任务库。工具 26 个且任务正确时无需重装。**

## 1. 能力边界

三个客户端共用同一个剪辑工作台 MCP Server 和同一组 26 个工具。当前客户端的模型负责文案分析、重点词和分镜等文本规划；剪辑工作台继续负责素材、任务账本、队列、豆包 TTS、Seedance、FFmpeg、质检和发布包。

客户端身份由 MCP 服务端启动参数固定注入：

- Codex：`--client codex`
- WorkBuddy：`--client workbuddy`
- 豆包工作：`--client doubao_work`

模型看不到也不能提交 `source_agent`。该字段只由服务端写入规划产物，用于审计来源，不增加任何业务权限。

以下四道人工作业门禁只能在剪辑工作台页面中确认：

1. 文案锁定；
2. 画面顺序锁定；
3. 包装确认；
4. 完整观看审片。

TTS、Seedance 等付费动作仍要求显式费用确认。状态查询、重连、上下文读取和规划提交都不得自动触发付费调用。

## 2. 前置检查

在剪辑工作台项目根目录运行：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client codex
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client workbuddy
```

两条 stdio 探测都必须返回当前身份、`toolCount: 26`、`allowedTaskActionCount: 4` 和 `ok: true`。探针默认启动独立进程并使用自动清理的临时空库，验证当前目录代码和解释器的协议能力，不打开或迁移已有工作台数据库，也不代表已打开的 WorkBuddy 会话已经重连。豆包工作 HTTP 探测见第 5 节；HTTP 探针只查询已运行服务。探测失败时先修复本地 Python、`requirements-mcp.txt` 或路径问题，不进入客户端配置。

## 3. Codex

预览并安装：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-codex-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-codex-mcp.ps1
```

新配置显式携带 `--client codex`。旧配置没有该参数时仍按 `codex` 运行。

自定义任务库或端口时，预览和实际安装须使用相同参数（下面是路径格式示例，替换为已核实的真实库）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-codex-mcp.ps1 -DatabasePath "D:\CUTROOM-data\catalog.sqlite" -WorkbenchOrigin "http://127.0.0.1:8792" -DryRun
```

核对输出的 command/cwd 是当前安装、args 包含正确库和工作台来源，再移除 `-DryRun` 安装。WorkBuddy 安装器及豆包启动器使用相同参数名。配置生成不会创建或迁移数据库；启动 MCP 服务会迁移指定库。已有正式库应沿既有升级流程保全，不能通过启动默认空库代替正式验证。


安装与卸载都需要项目的 `.venv\Scripts\python.exe`（Python 3.11 或更新版本）来解析并校验 TOML。编辑前后会比较其他服务及配置；数组表、带点号的引号键和多行字符串会保留。如果现有配置使用无法安全替换的内联或点号赋值形式，脚本会报错并保留原文件，请维护人员先检查配置结构后再运行。不要通过删除整个 `config.toml` 解决。

卸载：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-codex-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-codex-mcp.ps1
```

卸载只移除 Codex 中的 `cutroom` 服务项，不删除剪辑工作台、SQLite、任务、素材或凭据。

## 4. WorkBuddy

默认配置文件为 `%USERPROFILE%\.workbuddy\mcp.json`。安装前先保留一个同目录备份，然后执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-workbuddy-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-workbuddy-mcp.ps1
```

WorkBuddy 只修改 `mcpServers.cutroom`：

- 保留 `zhaoshang_wechat_bridge`；
- 保留其他 MCP 服务和未知顶层字段；
- Dry Run 只显示剪辑工作台项，不显示其他服务的命令、环境变量或凭据；
- 原文件不是有效 JSON 时停止，不覆盖；
- 重复安装只更新同一个 `cutroom` 项。

安装脚本只把 `cutroom` 写入 WorkBuddy 配置，不会替用户信任连接器，也不会让已经打开的旧会话自动获得工具。安装后：

1. 打开 WorkBuddy 的连接器或 MCP 管理页面；
2. 找到 `cutroom`，确认已启用；如出现“信任”提示则确认；
3. 完全退出并重新打开 WorkBuddy，或重新加载连接器后新建会话；
4. 调用 `cutroom_get_system_status`，确认 `clientId = workbuddy`、`mcpToolCount = 26`、`allowedTaskActionCount = 4`。

若当前会话提示 `Tool ... not found`，但 `mcp.json` 中已有 `cutroom`，优先检查信任状态和会话是否在信任之前创建；不要改用直接导入 `AgentGateway` 作为 MCP 验收结果。

升级前核对该配置的 `command`、`args`、`cwd` 与实际工作台使用的数据库。GitHub 或另一个工作树已更新，不会自动更新此配置或旧进程已导入的 Python 模块。安装脚本始终显式携带 `--db` 和 `--workbench-origin`；未提供自定义参数时绑定脚本所在项目的 `runtime/workbench/catalog.sqlite` 及 8765；已有任务库必须先备份并按正式升级流程验证，不能把 MCP 指向空库或联验库冒充升级。需要使用独立数据库时，服务入口支持 `--db <绝对路径> --workbench-origin <对应工作台地址>`；先确认源码、数据库、工作台三者一致，再重新加载连接器并在 WorkBuddy 新会话中验收。

卸载与回滚：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-workbuddy-mcp.ps1 -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-workbuddy-mcp.ps1
```

卸载只删除 `mcpServers.cutroom`。完成后检查 `zhaoshang_wechat_bridge` 和其他字段仍存在；剪辑工作台任务与规划产物不受影响。

## 5. 豆包工作 Agent

豆包工作必须通过公开的自定义连接器入口配置，不直接修改 Chromium LevelDB、IndexedDB、内部数据库或私有配置，也不使用 CDP。

以下启动脚本支持 `-DatabasePath` 与 `-WorkbenchOrigin`；省略时使用该安装目录的默认任务库及工作台地址 8765。MCP 服务端口仍为 8766，与工作台端口不同。独立任务库使用与 Codex/WorkBuddy 相同的两个参数；令牌仍由本机启动器管理。

默认安装在一个单独的 PowerShell 窗口启动本地服务，并在豆包工作使用期间保持窗口开启：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start-doubao-work-mcp.ps1
```

服务只监听 `127.0.0.1:8766`，不会开放到局域网。首次启动会在 `runtime\mcp\doubao-work.token` 生成随机 Bearer Token；该文件不得提交、粘贴到日志或共享记忆。另开一个窗口，把令牌仅注入当前探针进程后运行真实协议探针：

```powershell
$env:CUTROOM_MCP_BEARER_TOKEN = (Get-Content -Raw runtime\mcp\doubao-work.token).Trim()
.venv\Scripts\python.exe scripts\probe-cutroom-mcp.py --client doubao_work --transport streamable-http --url http://127.0.0.1:8766/mcp
Remove-Item Env:CUTROOM_MCP_BEARER_TOKEN
```

探针必须返回 `clientId: doubao_work`、`toolCount: 26` 和 `ok: true`。然后生成只读配置描述：

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

1. 发现 26 个工具；
2. `cutroom_get_system_status` 返回 `clientId = doubao_work`、`mcpToolCount = 26`、`allowedTaskActionCount = 4`；
3. 客户端重启后仍能读取同一剪辑工作台任务。

如果豆包工作连接失败：

1. 先确认豆包 HTTP 探针已成功；
2. 检查启动服务的窗口仍然开启、端口 `8766` 未被占用、Authorization Header 完整；
3. 保留客户端版本和可见错误证据；
4. 停止配置，不修改豆包内部数据，也不要把监听地址改成 `0.0.0.0`。

关闭启动服务的 PowerShell 窗口即可停用豆包连接。豆包工作中的连接器记录可从公开管理页面禁用或删除；这不会删除剪辑工作台任务和规划产物。

若 Authorization 曾出现在截图、聊天、日志或共享文档中，应关闭豆包 MCP 服务，删除 `runtime\mcp\doubao-work.token`，重新运行启动脚本生成新令牌，并更新连接器。令牌文件不承载任务数据，可以安全重建。

## 6. 状态数量口径

`cutroom_get_system_status` 的成功结果明确包含：

- `mcpToolCount = 26` 与 `mcpTools`：MCP 真实工具总数和完整清单；
- `allowedTaskActionCount = 4` 与 `allowedTaskActions`：`cutroom_start_task_action` 接受的四种动作；
- `agentActions`：为兼容旧客户端保留，含义仍是允许动作，不是 MCP 工具。

客户端回答“可用工具数量”时必须读取 `mcpToolCount`，不得用 `agentActions.length` 或 `allowedTaskActionCount` 代替。只有 MCP 工具发现结果和 `mcpTools` 一致，才算工具面验收通过。

## 7. 三端统一工作流

1. 调用 `cutroom_get_system_status`，确认服务和 `clientId`。
2. 调用 `cutroom_preflight_task` 做只读预检。
3. 创建 `agent` 模式任务，或读取已有 Agent 任务。
4. 在文案锁定门禁等待用户到剪辑工作台页面确认。
5. 调用 `cutroom_get_agent_context` 取得锁定文案、素材摘要、Schema 和 `inputDigest`。
6. 当前客户端模型按返回的 `planningSchema.contentSchema` 生成结构化规划，并遵守 `semanticRules`。广告包含 `plan.ratio/segments`，每段有 `text/query/prompt/type`；数字人包含 `brief.broll` 和 `plan`，可用 `brief.scriptSegments` 提供逐字覆盖的分句。原文覆盖、真实时码、素材范围和旧输入仍由提交端核验，Schema 合法不等于业务验收。
7. 调用 `cutroom_submit_planning_artifact`；来源身份由服务端固定注入。
8. 只从动作白名单启动后续作业。
9. 遇到画面锁定、包装确认和完整审片时停止，提示用户进入剪辑工作台页面。
10. 使用作业与结果查询工具恢复进度；关闭客户端不会把任务切换成独立模式。

`render_and_qa` 只有在页面完成包装确认后才能入队；`build_publish_package` 只有在页面批准当前候选的完整审片后才能入队。发布作业入队时绑定该候选版本，执行时若候选已变化则拒绝。MCP 不提供代替用户确认这四道门禁的工具。

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

## 9. 包装建议与候选环境

新增cutroom_get_packaging_request(task_id, request_id)和cutroom_submit_packaging_result(task_id, request_id, response)。从cutroom_get_task的packagingSuggestion取得待处理请求；领取结果包含本次请求冻结的提示词、有限上下文和输出Schema。客户端身份由启动参数固定，不能在工具入参伪造。提交只生成建议草案，四道人审门禁仍在网页；过期、取消、输入变化的结果不应用。用户可以在05页取消等待后重发。

本版共26个工具，允许的任务动作仍4个。旧安装若只返回12或14工具，应先核对实际源码/数据库/宿主会话，不能把旧服务当新候选。

隔离候选使用候选工作树内的脚本，显式指定库及网页来源（从该工作树执行）：

```powershell
python scripts/cutroom_mcp.py --client codex --db runtime/product-acceptance/catalog.sqlite --workbench-origin http://127.0.0.1:8788
```

这不会更改全局安装配置，但启动服务会迁移指定数据库，不能把启动服务当只读检查。省略参数仍使用原默认workbench库及8765。2026-09-29的schema75/14工具候选和schema39/12工具旧安装仅是历史快照；当前候选协议为26工具，已安装环境须按实际配置、运行进程和客户端状态另行核验。P09真实调用由05页的显式费用确认形成技术执行许可，绑定本次任务、请求、当前协议版本/哈希、输入摘要和实际模型配置；不依赖维护人员手填全局批准JSON。领取与返回仍校验冻结协议和当前输入。该许可不批准包装内容、素材版权、观看或发布，也不把测试回执当正式批准。

## 10. 编辑、节奏与补拍工具

除原有14个任务、规划、包装和作业工具，当前还提供12个工具：文字修改草稿的提出、读取、取消、应用和撤销；节奏建议的生成、读取、差异预览和镜头定位；补拍清单的生成、读取和导出。它们只覆盖服务端允许的修改和建议流程，不能代替网页四道人审门禁。应用或撤销文字修改需要用户明确确认，并核验任务版本；生成建议会保存草稿，不应当作纯只读查询。

验收时比较 `tools/list` 的26个工具与状态中的 `mcpTools`，同时核对独立的4个 `allowedTaskActions`。工具数量相同仍不足以证明源码版本或数据库相同；本版状态尚未返回发行版本、源码提交或实例标识，因此还需配置路径与实际运行证据。

## 11. beta78 实际绑定与更新边界（2026-10-20）

Codex 当前会话已通过正式安装的系统状态调用：26工具、4类动作、schema96，正式数据库可用；实际注册路径指向安装目录及8792，不再依赖旧开发解释器。状态本身不含发行版本，部署核验仍需安装清单和配置路径。

独立任务的 get_task 可读，get_result_links 仍限 Agent 模式；此限制不能当作独立产品交付失败。新配音恢复、换底片、转写修复不要求立刻新增MCP工具：先保持安装和现有协议兼容；新增写操作须接相同后台版本/费用/结果未知/重复请求规则，再增量验证。不得用MCP代替真实人工批准。其他客户端未在本轮实际重新打开，协议级证据不能冒充宿主联调。
