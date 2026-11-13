# 剪辑工作台 WorkBuddy 与豆包工作 MCP 验收记录

日期：2026-09-18  
分支：`codex/workbuddy-doubao-mcp`  
客户端：WorkBuddy 5.5.6；豆包工作 2.30.1

## 结论

**实现与协议门禁通过；客户端公开界面最后一次启用操作待人工完成。**

- Codex、WorkBuddy 共用本地 stdio；豆包工作使用只监听 `127.0.0.1:8766` 的 Streamable HTTP。
- 三端共用同一套 12 个剪辑工作台工具与 Agent Gateway。
- `source_agent` 已从公开工具 Schema 移除，由服务器按 `codex`、`workbuddy`、`doubao_work` 固定注入。
- 豆包 HTTP 要求随机 Bearer Token；无令牌和错误令牌均返回 401。
- 四道人工作业门禁与付费确认没有放宽；本次验收未调用 TTS、Seedance 或其他付费生产服务。

桌面自动化能够读取两个客户端的公开连接器入口，但当前 Chromium WebView 不返回可点击几何信息，Windows Graphics Capture 也返回“不支持此接口”。因此没有盲点、没有修改内部数据库，也没有伪造客户端内工具调用通过。人工只需完成本文末尾的公开 UI 填写。

## WorkBuddy

实际配置：`%USERPROFILE%\.workbuddy\mcp.json`

- 写入前备份：`mcp.json.20260918-101926.bak`。
- 原有 `zhaoshang_wechat_bridge` 保留。
- 只新增 `mcpServers.cutroom`，类型为 `stdio`，客户端身份为 `workbuddy`。
- WorkBuddy 在配置写入后重新启动，版本核验为 5.5.6。
- 独立真实 stdio 探针发现 12 个工具并返回 `clientId = workbuddy`。

待人工：在 WorkBuddy 的“专家·技能·连接器”中刷新连接器发现，然后执行一次只读 `cutroom_get_system_status`。

## 豆包工作

公开资料与当前界面证明自定义连接器使用 HTTP URL 和自定义 Headers，而不是本地 stdio 命令。实施结果：

- URL：`http://127.0.0.1:8766/mcp`
- 身份：`doubao_work`
- 启动脚本：`scripts\start-doubao-work-mcp.ps1`
- 配置输出：`scripts\show-doubao-work-mcp-config.ps1`
- Token 文件：`runtime\mcp\doubao-work.token`，不进入 Git、发布包、日志或共享记忆。
- 正确令牌真实发现 12 个工具并返回 `clientId = doubao_work`。
- 无令牌、错误令牌均返回 HTTP 401。
- 端口 8766 与剪辑工作台工作台 8765 分离。

待人工：在“插件 · 技能 · 伙伴 → 新建自定义连接器”中填写配置输出，并执行一次只读 `cutroom_get_system_status`。

## 自动化证据

- MCP / Gateway / 发布专项：50 项通过。
- Python 全量：721 项通过，0 失败。
- TypeScript：73 项通过，0 失败。
- Web/UI：58 项通过，0 失败。
- 三个 stdio 身份真实探针均返回 12 个工具与准确身份。
- 豆包 Streamable HTTP 真实探针返回 `{"clientId":"doubao_work","toolCount":12,"ok":true}`。
- WorkBuddy 安装、重复安装、无效 JSON、Dry Run、卸载与无变更回滚均有测试覆盖。
- 发布包包含启动、探针、安装与文档脚本，不包含客户端配置、Token、数据库或日志。

## 人工公开界面收尾

1. 从正式剪辑工作台根目录运行 `scripts\start-doubao-work-mcp.ps1` 并保持窗口开启。
2. 另开窗口运行 `scripts\show-doubao-work-mcp-config.ps1`，仅把输出填入豆包工作的公开自定义连接器表单。
3. WorkBuddy 与豆包工作各调用一次 `cutroom_get_system_status`，分别确认 `clientId = workbuddy` 和 `clientId = doubao_work`。
4. 不创建付费作业；若要继续真实视频任务，仍在剪辑工作台页面完成人工门禁。
