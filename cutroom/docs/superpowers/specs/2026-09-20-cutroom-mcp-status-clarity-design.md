# 剪辑工作台 MCP 状态口径与操作手册优化设计

日期：2026-09-20

## 问题

Codex、WorkBuddy 和豆包工作共用 12 个剪辑工作台 MCP 工具，但 `cutroom_get_system_status` 当前只返回四个 `agentActions`。客户端模型在回答“工具数量”时会把这四个任务动作误报成 MCP 工具总数。

同时，普通同事手册没有完整说明 WorkBuddy 的“信任”步骤、豆包工作的 HTTP 启动与请求头配置，也没有解释“12 个 MCP 工具”和“4 个任务动作”的区别。

## 设计

1. `cutroom_get_system_status` 保留现有 `agentActions`，避免破坏已有调用。
2. MCP 层新增：
   - `mcpToolCount`：当前 MCP 服务注册的工具数量；
   - `mcpTools`：完整工具名列表；
   - `allowedTaskActionCount`：允许交给 `cutroom_start_task_action` 的动作数量；
   - `allowedTaskActions`：允许动作列表。
3. 工具清单由 MCP 服务端单一常量维护，状态返回和工具发现测试必须一致，防止以后新增工具时数量漂移。
4. 更新普通同事手册、内置帮助、维护手册和 README：
   - WorkBuddy 使用 `stdio`，安装后需在连接器管理中信任并重启或新建会话；
   - 豆包工作使用本机 HTTP，必须先启动服务，再显示并填写 URL 与 Authorization；
   - Authorization 不得截图、分享或写入文档；
   - 12 个 MCP 工具与 4 个允许动作是两个不同口径。

## 验收

- 三种客户端身份调用状态工具时均返回正确 `clientId`。
- 状态返回 `mcpToolCount = 12`、`allowedTaskActionCount = 4`，工具列表与真实 MCP 发现结果完全一致。
- 旧字段 `agentActions` 继续存在。
- MCP 相关测试、项目 Python 全量测试和文档构建/前端测试通过。
