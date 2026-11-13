# 剪辑工作台 WorkBuddy 与豆包工作 Agent MCP 适配设计

日期：2026-09-18

状态：设计与书面规格已由 Peter 确认；待实施计划与执行

前置设计：`docs/superpowers/specs/2026-09-16-cutroom-agent-mcp-design.md`

适用范围：剪辑工作台已验收的 Agent 模式向 WorkBuddy 5.5.6 和豆包工作 2.30.1 扩展。Codex、WorkBuddy、豆包工作分别使用各自当前会话模型完成文本规划，共用同一套剪辑工作台业务层、任务账本和安全门禁。

## 1. 背景与目标

剪辑工作台已具备一个本地 `stdio` MCP Server、12 个受控工具、Agent Gateway、不可变规划产物和双模式任务。Codex 真实验收已证明：当前 Agent 模型可以完成文案分析、重点词和分镜规划，剪辑工作台无需为这些文本步骤调用自己的大模型；剪辑工作台继续负责素材、任务、队列、豆包 TTS、Seedance、FFmpeg、质检、发布包和四道人工作门禁。

本设计不新建两套视频业务，也不让 Agent 相互转发任务。目标是让 WorkBuddy 和豆包工作成为与 Codex 并列的 MCP 客户端：

1. Peter 在任一客户端中直接下达剪辑工作台视频任务。
2. 该客户端的当前模型读取剪辑工作台提供的有界上下文并生成结构化规划产物。
3. 同一 Agent Gateway 校验、持久化并推进现有工作流。
4. 剪辑工作台 UI 继续承担人工门禁、预览、质检和成片查看。
5. 产物准确记录来源客户端，不能由模型伪造来源身份。

## 2. 已核实基线

- 剪辑工作台 `main` 已包含 12 个本地 `stdio` MCP 工具，当前入口为项目虚拟环境 Python 加 `scripts/cutroom_mcp.py`。
- WorkBuddy 5.5.6 已安装，用户级 MCP 配置位于 `%USERPROFILE%\.workbuddy\mcp.json`；现有 `zhaoshang_wechat_bridge` 配置必须保留。
- WorkBuddy 官方连接器格式支持本地 `stdio`，包含 `command`、`args`、`env` 和可选 `cwd`。
- 豆包工作 2.30.1 已安装于 `D:\DoubaoWork`，安装包包含 `mcp_helper.dll`；本机历史任务已有 MCP 工具结果，证明该客户端具备 MCP 调用能力。
- 豆包工作支持通过“技能·连接器·伙伴”创建自定义连接器；公开界面只提供 HTTP 传输、服务器 URL 和自定义 Headers，不提供本地命令、参数或工作目录字段。
- 现有 MCP 的 `cutroom_submit_planning_artifact` 把 `source_agent` 暴露为模型参数且默认值为 `codex`。直接复用会造成来源误标，也允许模型伪造客户端身份，必须先修正。

## 3. 方案选择

### 3.1 采用：单一 MCP 核心加客户端启动身份

所有客户端启动同一个 MCP Server，通过只读启动参数选择 `codex`、`workbuddy` 或 `doubao_work`。服务器在注册工具时闭包绑定客户端身份；提交规划产物时由服务器注入 `source_agent`，不再把该字段暴露给模型。

优点：

- 只有一套工具契约、业务校验、数据库和安全边界。
- 三个客户端的行为差异仅位于安装、配置和展示层。
- 修复与升级只发生一次，不会形成三套漂移实现。
- 审计记录能可靠区分来源客户端。

### 3.2 不采用：为每个客户端复制 MCP Server

复制服务器看似隔离清楚，但会复制工具定义、错误映射、测试和安全修复；三个客户端很快出现行为漂移，因此不采用。

### 3.3 采用：仅为豆包工作增加受限回环 HTTP

WorkBuddy 继续使用本地 `stdio`。豆包工作的公开自定义连接器只接受 HTTP，因此为它复用同一 MCP Server，并增加只监听 `127.0.0.1:8766`、要求随机 Bearer Token 的 Streamable HTTP 入口。该入口显式前台启动，不随普通安装静默常驻。

### 3.4 不采用：CDP 或界面逆向桥接

CDP、内部消息格式和私有数据库都不是稳定公共接口。版本升级后易失效，也会扩大凭据与聊天数据暴露面，因此不进入正式方案。

## 4. 总体架构

系统保持四层：

1. **客户端配置层**：Codex、WorkBuddy 保存 MCP 启动配置；豆包工作保存回环 URL 和 Authorization Header。
2. **MCP 传输与工具层**：同一个 `cutroom_mcp` 包；Codex、WorkBuddy 使用 `stdio`，豆包工作使用受限回环 Streamable HTTP。
3. **Agent Gateway**：继续负责白名单、模式、阶段、指纹、幂等、付费确认、错误裁剪和门禁保护。
4. **剪辑工作台核心**：SQLite 主账本、素材库、任务队列、外部生产服务、渲染、QA、发布包和 UI。

数据流：

`用户指令 → 当前客户端模型 → 剪辑工作台 MCP → Agent Gateway → 剪辑工作台核心 → 有界状态/结果 → 当前客户端模型`

客户端之间不通信，不共享聊天记录，也不通过 MCP 调用彼此。它们只共享剪辑工作台中已经持久化的任务状态和规划产物。

## 5. 客户端身份与工具契约

### 5.1 启动身份

`scripts/cutroom_mcp.py` 增加受限参数：

- `--client codex`
- `--client workbuddy`
- `--client doubao_work`

缺省值仅为向后兼容保留 `codex`。未知值启动失败，不能由工具调用动态修改。

### 5.2 服务器注入

`create_mcp_server` 接受不可变 `client_id`。`cutroom_submit_planning_artifact` 的公开参数删除 `source_agent`，服务器调用 Gateway 时固定注入 `client_id`。`source_model_label` 仍是可选说明字段，但只能作为非安全审计标签，不能参与权限判断。

### 5.3 工具面

三端继续发现相同的 12 个工具：

1. `cutroom_get_system_status`
2. `cutroom_list_tasks`
3. `cutroom_get_task`
4. `cutroom_get_agent_context`
5. `cutroom_get_job`
6. `cutroom_get_result_links`
7. `cutroom_preflight_task`
8. `cutroom_create_task`
9. `cutroom_submit_planning_artifact`
10. `cutroom_start_task_action`
11. `cutroom_cancel_job`
12. `cutroom_resume_job`

`cutroom_get_system_status` 增加非敏感 `clientId`，便于验收当前连接使用了正确启动身份。

### 5.4 兼容性

- 旧 Codex 配置未传 `--client` 时行为保持不变。
- 数据库 `source_agent` 使用稳定值 `codex`、`workbuddy`、`doubao_work`。
- 已保存的历史 `codex` 产物不迁移、不改写。
- 工具返回结构和现有 Agent Gateway 错误码不因客户端变化而分叉。

## 6. WorkBuddy 适配

### 6.1 配置方式

新增 WorkBuddy 安装与卸载脚本，默认操作 `%USERPROFILE%\.workbuddy\mcp.json`，写入：

- 服务名：`cutroom`
- 类型：`stdio`
- 命令：剪辑工作台项目 `.venv\Scripts\python.exe`
- 参数：`scripts\cutroom_mcp.py --client workbuddy`
- 工作目录：剪辑工作台项目根目录

### 6.2 安全写入

- 安装前解析现有 JSON；格式无效时停止，不覆盖。
- 只新增或替换 `mcpServers.cutroom`。
- 保留 `zhaoshang_wechat_bridge` 和其他未知字段。
- 使用同目录临时文件和原子替换。
- Dry Run 只显示剪辑工作台项，不回显其他服务的命令、环境变量或凭据。
- 卸载只移除 `mcpServers.cutroom`，不删除剪辑工作台、任务或凭据。

### 6.3 使用提示

首版依靠稳定工具描述和项目操作文档，不新增独立 Skill。只有真实验收证明 WorkBuddy 无法稳定遵循“先读上下文、再提交规划、门禁处停止”的顺序时，才把客户端指导升级为单独 Skill；该升级另走 Skill 设计与版本门禁。

## 7. 豆包工作适配

### 7.1 公开 HTTP 自定义连接器

先生成豆包工作专用连接器配置：

- 服务名：`cutroom`
- 类型：`http`
- URL：`http://127.0.0.1:8766/mcp`
- Header：`Authorization: Bearer <本机随机令牌>`

通过豆包工作的公开自定义连接器入口导入，不直接修改 Chromium LevelDB、IndexedDB、内部数据库或私有配置。

### 7.2 协议与身份探测

真实验收按以下顺序执行：

1. 前台启动 `scripts\start-doubao-work-mcp.ps1`。
2. 用官方 MCP 客户端通过带 Authorization Header 的 Streamable HTTP 探测。
3. 在豆包工作中创建 HTTP 自定义连接器。
4. 确认工具发现数量为 12。
5. 调用系统状态并确认 `clientId = doubao_work`。
6. 执行无费用的隔离任务探测。

若客户端连接失败，先核对前台服务、端口、URL 和 Authorization Header；不得修改豆包内部数据库绕过公开入口。

### 7.3 受限回环 Streamable HTTP

HTTP 入口满足以下约束：

- 只监听 `127.0.0.1`，不监听局域网或公网地址。
- 复用同一个 `create_mcp_server` 和 Agent Gateway，不复制工具实现。
- 首次启动生成高熵本机令牌；连接器通过自定义 Header 注入。
- 校验授权头；拒绝无令牌或错误令牌请求。
- 不把令牌写入项目仓库、日志、共享记忆或 MCP 返回。
- 提供显式启动、状态检查和停止方式，不随普通剪辑工作台安装静默常驻。

豆包工作连接器固定使用 `http://127.0.0.1:8766/mcp`，避开剪辑工作台工作台的 `8765` 端口。

### 7.4 使用提示

与 WorkBuddy 相同，首版不新建独立 Skill。若豆包工作实际调用顺序不稳定，再单独设计客户端 Skill；不能把未验证需求提前并入本次 MCP 主线。

## 8. Agent 工作流

三端行为一致：

1. 调用 `cutroom_get_system_status` 检查服务与客户端身份。
2. 调用 `cutroom_preflight_task` 做只读预检。
3. 创建 `agent` 模式任务，或读取已有 Agent 任务。
4. 在文案锁定门禁等待用户在剪辑工作台 UI 确认。
5. 调用 `cutroom_get_agent_context` 获取锁定文案、素材摘要、Schema 和 `inputDigest`。
6. 当前客户端模型生成结构化规划。
7. 调用 `cutroom_submit_planning_artifact`；Gateway 校验后保存不可变版本。
8. 只从动作白名单启动后续作业。
9. 到画面锁定、包装确认和完整审片时停止，提示用户进入剪辑工作台 UI。
10. 查询作业与结果链接；中断后依靠 SQLite 和持久队列恢复。

客户端关闭或切换不会把未完成任务改成 `standalone`，也不会自动触发剪辑工作台自身文本模型。

## 9. 安全与权限边界

- 三端都不能读取或修改模型凭据。
- 三端都不能执行任意命令、任意 SQL、任意文件读写或素材删除。
- 三端都不能批准文案锁定、画面锁定、包装确认或完整审片。
- TTS、Seedance 等付费动作继续要求显式费用确认；查询、重连和恢复不得自动触发费用。
- 原始素材保持只读；结果路径继续经过项目根和任务归属校验。
- MCP 输出继续过滤 API Key、Token、Cookie、密码、项目绝对根路径和无界日志。
- 客户端身份只用于审计和兼容诊断，不增加额外业务权限。
- 安装脚本不读取或输出其他 MCP 的敏感字段。

## 10. 错误与恢复

- 客户端配置无效：返回可执行的配置错误，不修改原文件。
- MCP 进程退出：任务和规划产物保留；重新连接后继续查询。
- 输入指纹变化：拒绝旧规划产物并要求重新读取上下文。
- 重复提交：相同任务、输入摘要和内容摘要保持幂等。
- 门禁未完成：返回 `awaiting_human_confirmation`，不创建越级作业。
- 未确认费用：返回 `PAID_CONFIRMATION_REQUIRED`，不创建付费作业。
- 豆包工作 HTTP 连接失败：保留真实错误证据，核对前台服务、端口、URL 和 Header，不降级为无鉴权或非回环监听。

## 11. 安装、卸载与发布包

计划新增或调整：

- `scripts/cutroom_mcp.py`：客户端身份参数。
- `cutroom_mcp/server.py`：固定身份注入。
- `scripts/install-workbuddy-mcp.ps1`。
- `scripts/uninstall-workbuddy-mcp.ps1`。
- 豆包工作连接器配置生成脚本、回环 HTTP 前台启动脚本与操作文档。
- README、同事操作手册和发布清单。

普通剪辑工作台安装仍不自动注册任何 Agent 客户端。三个客户端的启用与卸载相互独立；卸载某一客户端配置不影响其他客户端和剪辑工作台独立模式。

## 12. 测试与验收

### 12.1 自动化测试

必须覆盖：

- 三个允许的客户端身份和未知身份拒绝。
- `source_agent` 不再出现在公开工具 Schema 中。
- 三种启动身份提交产物后数据库来源准确。
- 旧 Codex 无参数启动保持兼容。
- WorkBuddy 安装、重复安装、Dry Run、卸载和无效 JSON 拒绝。
- WorkBuddy 现有服务和未知字段保持不变，敏感值不出现在输出。
- 豆包工作配置只包含回环 URL 和随机 Bearer Header，服务端身份固定为 `doubao_work`。
- 四道人工作业门禁、付费确认、路径限制和凭据过滤回归。
- 发布包包含新增脚本与文档，不包含本机配置、数据库、日志或凭据。
- 回环 HTTP：身份、真实工具发现、无令牌拒绝、错误令牌拒绝、端口冲突和停止恢复测试。

### 12.2 真实客户端验收

WorkBuddy：

1. 保留现有 `zhaoshang_wechat_bridge`，发现 `cutroom` 及 12 个工具。
2. 系统状态返回 `clientId = workbuddy`。
3. 使用隔离任务完成一次上下文读取和规划提交，不调用剪辑工作台文本模型。
4. 在人工门禁处正确停止；未确认费用时不创建付费作业。
5. 卸载后仅 `cutroom` 配置消失，其他服务和剪辑工作台数据不变。

豆包工作：

1. 通过公开自定义连接器入口完成连接，不修改内部数据库。
2. 发现 12 个工具，系统状态返回 `clientId = doubao_work`。
3. 使用隔离任务完成一次上下文读取和规划提交，不调用剪辑工作台文本模型。
4. 在人工门禁处正确停止；未确认费用时不创建付费作业。
5. 客户端重启后可以重新读取同一任务和 Artifact。

Codex 回归：

1. 仍发现 12 个工具并返回 `clientId = codex`。
2. 既有 Agent 模式、独立模式、安装/卸载和真实 stdio 探测继续通过。

真实验收不触发 TTS、Seedance 或其他付费生产调用。

## 13. 完成定义

只有同时满足以下条件才可宣布完成：

- WorkBuddy 与豆包工作各自真实发现并调用剪辑工作台 MCP。
- 两端当前模型都能完成一次无付费规划链路，剪辑工作台不调用自身文本模型。
- 三端产物来源身份准确且不能由模型伪造。
- WorkBuddy 既有 MCP 配置未被覆盖；豆包工作未使用内部数据库或 CDP 逆向写入。
- 四道人工作业门禁、费用确认、凭据隔离和独立模式无回归。
- 安装、卸载、重启恢复、自动化测试和真实客户端验收均有证据文档。

仅生成配置、看到连接器名称、MCP 能握手或单元测试通过，都不等于完成。
