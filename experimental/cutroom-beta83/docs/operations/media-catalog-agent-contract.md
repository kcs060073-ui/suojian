# 素材目录 Agent 操作契约

适用入口：Codex、WorkBuddy。

## 路由

所有视频任务先读取[视频生产总工作流](../superpowers/specs/2026-08-12-video-production-workflow-design.md)。只有当任务涉及本机或移动硬盘素材登记、盘点、搜索、重连或恢复时，再读取[渐进式素材资产层](../superpowers/specs/2026-08-13-large-media-progressive-asset-workflow-design.md)和本契约；不要为 P0 加载其他六个子工作流。

## 正常 Agent 控制入口

日常由 Codex 控制剪辑工作台时优先使用 `cutroom` MCP：查询任务、读取受限规划上下文、提交规划产物、启动允许的后台动作和读取结果都通过 MCP 完成。CLI 只保留给开发、诊断、批处理和 MCP 不可用时的维护操作，不作为普通同事入口。

- 独立模式：剪辑工作台自己调用已配置的文案/分镜模型。
- Agent 模式：当前 Codex 模型完成文案与分镜；剪辑工作台不调用文本模型。
- 两种模式：豆包 TTS、Seedance 等生产服务仍使用剪辑工作台各自凭据。
- 四个人工门禁继续由用户在剪辑工作台页面确认，MCP 不提供门禁批准工具。

任务地址使用 `http://127.0.0.1:8765/#/tasks/<task-id>`，且要求现有工作台会话已经建立；不得记录或传播带启动密钥的地址。关闭 MCP 时只移除 Codex 中的剪辑工作台服务项，现有剪辑工作台和任务继续可用。

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

## 补拍缺口建议（开发候选）

复用统一库及任务授权范围，不另建素材目录逻辑。Agent 可经 MCP `cutroom_get_task` 取得 `storyboardDigest`，再调用 `cutroom_build_reshoot_list(task_id, expected_revision)`；读取用 `cutroom_get_reshoot_list`，导出用 `cutroom_export_reshoot_list(task_id, report_id, expected_revision, format)`。格式为 text/json；这三个工具不包含确认门禁或外部请求。

CLI 等价入口：`reshoot build --task <ID> --revision <摘要> --project-root <实际项目> --json`、`reshoot list --task <ID> --json`、`reshoot export --task <ID> --report <报告ID> --revision <摘要> --format text --output <新文件>`。仍通过前述同一数据库与脚本入口调用；输出文件存在时拒绝覆盖。

只把本地有效观察当相应证据，候选可用不表示已选或语义准确率通过。离线/未分析先恢复与分析，不能要求用户补拍；同路径文件替换会失效旧观察。旧精确锚点缺失只使用唯一当前文案段的实测范围并披露，不能猜秒数。修改或补材后重检同一需求，旧报告不可冒充当前版本；保持既有镜头与业务确认。


## 节奏建议与只读修改草稿（开发候选）

沿用任务 `storyboardDigest` 作为 `expected_revision`；不新增素材目录或代理专用修改逻辑。MCP 为 `cutroom_build_rhythm_advice(task_id, expected_revision)`、`cutroom_get_rhythm_advice(task_id, report_id=None)`、`cutroom_preview_rhythm_edit(task_id, report_id, suggestion_id, expected_revision)`、`cutroom_locate_rhythm_suggestion(task_id, report_id, suggestion_id, expected_revision)`。

CLI 通过同一数据库：`rhythm build --task <ID> --revision <摘要>`、`rhythm list --task <ID>`；`rhythm preview` 与 `rhythm locate` 另带 `--report <报告ID> --suggestion <建议ID> --revision <摘要>`。HTTP 为 GET/POST `/api/tasks/<ID>/rhythm-advice`，预览/定位分别 POST 同路径 `/preview`、`/locate`；写请求均校验当前版本。

报告来自本地结构线索，不包含完整 AI 节奏结论。镜头、选定文件完整内容、绑定口播、包装或观察依据改变后旧操作拒绝；旧报告保留，重检生成/复用对应版本。跨任务报告拒绝读取/操作；素材不可用或明确重复约束不能用节奏建议绕过。草稿只调用既有手工编辑验证与影响计算，不应用、不启动模型、渲染或批准。
