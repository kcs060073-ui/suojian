# 素材目录 Agent 操作契约

适用入口：Codex、WorkBuddy。

## 路由

所有视频任务先读取[视频生产总工作流](../superpowers/specs/2026-08-12-video-production-workflow-design.md)。只有当任务涉及本机或移动硬盘素材登记、盘点、搜索、重连或恢复时，再读取[渐进式素材资产层](../superpowers/specs/2026-08-13-large-media-progressive-asset-workflow-design.md)和本契约；不要为 P0 加载其他六个子工作流。

## 正常 Agent 控制入口

日常由 Codex 控制 CUTROOM 时优先使用 `cutroom` MCP：查询任务、读取受限规划上下文、提交规划产物、启动允许的后台动作和读取结果都通过 MCP 完成。CLI 只保留给开发、诊断、批处理和 MCP 不可用时的维护操作，不作为普通同事入口。

- 独立模式：CUTROOM 自己调用已配置的文案/分镜模型。
- Agent 模式：当前 Codex 模型完成文案与分镜；CUTROOM 不调用文本模型。
- 两种模式：豆包 TTS、Seedance 等生产服务仍使用 CUTROOM 各自凭据。
- 四个人工门禁继续由用户在 CUTROOM 页面确认，MCP 不提供门禁批准工具。

任务地址使用 `http://127.0.0.1:8765/#/tasks/<task-id>`，且要求现有工作台会话已经建立；不得记录或传播带启动密钥的地址。关闭 MCP 时只移除 Codex 中的 cutroom 服务项，现有 CUTROOM 和任务继续可用。

## CLI 诊断与批处理动作

1. `doctor --json`：开始前检查。
2. `library add` 或 `library mount`：登记或重连。
3. `scan --catalog-only --json`：L1 快速盘点。
4. `probe --json`：L2 技术探测。
5. `status --json`、`search --json`：轻量查询，可在重任务运行时执行。
6. `backup --json`、`restore --json`：备份或恢复主账本。

诊断入口：

```powershell
& 'E:\我的AI项目\视频自动化剪辑\.venv\Scripts\python.exe' scripts/media_catalog.py <参数>
```

数据库未由任务另行指定时使用 `runtime/media-catalog/catalog.sqlite`。Agent 负责填写命令参数，不要求普通同事复制或编辑命令。

本机硬盘和移动硬盘统一按 `local` 类型登记；移动硬盘盘符变化时使用 `library mount` 更新位置，不重新建立素材身份。底层 `nas` 类型只为向后兼容保留，当前任务不主动使用。

## 用户回复

始终使用四段式回复：

```text
当前结果：完成了什么
处理进度：完成数 / 总数
需要处理：阻塞、警告或“无”
下一步：用户需要做什么，或 Agent 将继续什么
```

不要要求用户输入命令、SQLite路径、素材ID或ffprobe路径。原片只读；P0不删除、不改名、不复制、不转码。

## 错误处理

- `retryable=true`：说明当前结果仍保留，并按 `next_action` 等待条件恢复。
- `LIBRARY_OFFLINE`：不把素材标记为删除；等待本机硬盘或移动硬盘可访问。
- `HEAVY_JOB_BUSY`：不启动第二个重任务；可以继续状态查询和搜索。
- `TOOL_NOT_FOUND`：L1 结果保留，修复 ffprobe 后只继续 L2。
- `DATABASE_ERROR`：停止写入，检查完整性或从已验证备份恢复。
- `INTERRUPTED`：只继续未完成项，不从零扫描。

默认只读取 JSON 结果、退出码和有界错误摘要，不把完整日志或原始视频送入模型上下文。

## 阶段边界

P0 只负责 L0 登记、L1 快速盘点和 L2 技术探测。缩略图、接触表、代理、VAD、粗转写、场景识别、候选确认、分镜、包装、质检和发布继续服从主工作流 P1—P3 与七个子工作流的分工。
