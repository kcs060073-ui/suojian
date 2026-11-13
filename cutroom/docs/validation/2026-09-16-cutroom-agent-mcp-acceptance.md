# 剪辑工作台 Agent MCP 验收报告

验收日期：2026-09-17

对应设计：`docs/superpowers/specs/2026-09-16-cutroom-agent-mcp-design.md`

验收分支：`codex/cutroom-agent-mcp-impl`

版本策略：本次未切正式 Release，产品版本保持 `0.9.0-beta.4`。

## 结论

验收通过。

剪辑工作台现在具备可选的本地 stdio MCP 接口。Agent 模式下，Codex 当前会话模型负责文案分析、重点词和分镜规划，剪辑工作台不需要为这些文本规划步骤配置大模型 API Key；剪辑工作台继续负责素材主账本、任务状态、四个人工门禁、持久队列、TTS、Seedance、FFmpeg 渲染、质检和发布包。独立模式、现有模型配置和既有任务执行入口保持不变。

本次真实验收没有调用 TTS、Seedance 或其他付费生产服务。

## 验收环境

- Windows，本地 Codex Desktop 配置目录：`%USERPROFILE%\.codex`
- 剪辑工作台工作台地址：`http://127.0.0.1:8765/`
- MCP 传输：标准输入输出（stdio）
- 数据库迁移版本：31
- MCP 注册状态：`cutroom` 已启用
- MCP 入口：项目虚拟环境 Python + `scripts/cutroom_mcp.py`

注册脚本会优先使用 `CODEX_HOME`，未设置时使用 `%USERPROFILE%\.codex\config.toml`；写入采用原子替换，并保留其他 MCP 服务配置。Dry Run 只显示剪辑工作台自身配置，不回显无关配置或其中的凭据。

## 工具发现与真实连接

通过真实 stdio 客户端启动 MCP 进程并调用系统状态，返回：

- `service = cutroom`
- `database = available`
- `schemaVersion = 31`
- 路线：`digital_human`、`ad_mix`

发现的 12 个工具如下：

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

真实连接阶段发现并修复了一个 SQLite 线程归属问题：MCP SDK 会把同步工具放到工作线程执行，而剪辑工作台的 SQLite 连接属于服务主线程。所有 MCP 工具改为异步入口后在服务事件循环线程执行，并增加真实数据库回归测试，避免 stdio 连接成功但工具调用返回 `INTERNAL_ERROR`。

## Agent 模式链路

使用隔离临时数据库和本地替身素材完成数字人链路：

- 创建 Agent 模式任务成功。
- `cutroom_get_agent_context` 返回锁定文案、受任务素材范围约束的摘要、规划 Schema 和稳定 `inputDigest`。
- 上下文不包含 API Key、Token、Cookie、密码、凭据或项目绝对根路径。
- Codex 产物提交后生成不可变 `agent_artifacts` 记录，并完成数字人分镜归一化。
- 相同输入与相同内容重复提交保持幂等：第一次 `created = true`，第二次 `created = false`，Artifact ID 不变。
- 任务推进到 `rough_cut`，没有执行 `plan-brief.ts`、`plan-ad.ts` 或文本模型 Provider；受控命令记录为空。
- 返回任务页：`http://127.0.0.1:8765/#/tasks/accept-agent-digital-20260917`。

这证明文本规划由 Agent 当前模型完成，剪辑工作台不会静默调用自身文案/分镜模型，也不会在 Agent 失败时自动回退到独立模式文本模型。

## 人工门禁

四道人工作业门禁保持原定义，MCP 不提供批准工具：

1. 文案锁定。
2. 画面锁定。
3. 包装方案确认。
4. 完整审片。

验收任务在提交规划后仅保留已由 UI 完成的文案锁定，`picture_lock`、`packaging_confirmed` 和 `human_reviewed` 均未被 MCP 自动置为通过；系统质检 `qa_passed` 也未被伪造。

## 外部付费服务门禁

广告 Agent 任务提交规划产物后，下一步为 `start_recommend_storyboard_for_tts`。未传入明确费用确认时调用付费动作，稳定返回 `PAID_CONFIRMATION_REQUIRED`，且作业列表为空。

因此：

- TTS、Seedance 等生产服务继续使用剪辑工作台各自的配置和凭据。
- Agent 规划不需要这些凭据，也无法通过 MCP 读取或写入凭据。
- 未经明确费用确认，不创建付费作业。

## 中断、重启和关闭 MCP

- 关闭数据库连接并重新打开后，任务仍可读取，Artifact ID 保持一致，证明任务和 Agent 产物不依赖 MCP 进程内存。
- 实际运行卸载脚本后，`codex mcp get cutroom` 返回未注册；此时独立模式的分镜推荐测试和独立任务保护测试仍通过。
- 随后重新运行安装脚本，`codex mcp get cutroom` 显示 `enabled: true`。
- 卸载只移除 Codex 配置中的 `cutroom` MCP 项，不删除剪辑工作台数据、模型配置、凭据或任务。

## 独立产品兼容

- 默认 `orchestration_mode` 为 `standalone`，旧任务迁移后保持独立模式。
- 独立任务可以继续通过原工作台、业务 API、CLI 和现有模型配置执行。
- Agent 上下文或 Agent 产物提交到独立任务时返回 `AGENT_MODE_REQUIRED`，不会隐式改变任务模式。
- 正常安装器不会自动安装 MCP 可选依赖，也不会自动注册 MCP；MCP 仍是明确启用、可单独关闭的附加能力。

## 发布包验证

发布清单已加入：

- `requirements-mcp.txt`
- `cutroom_mcp/`
- `scripts/install-codex-mcp.ps1`
- `scripts/uninstall-codex-mcp.ps1`
- `scripts/cutroom_mcp.py`

ZIP 构建测试确认 MCP 运行时和可选依赖文件实际进入发布包，同时继续排除 `.env`、数据库、运行时数据、签名密钥和本地私有素材。

## 自动化回归

最终完整回归结果：

- Python：705/705 通过，耗时 684.990 秒。
- TypeScript：73/73 通过，耗时 39.756 秒。
- 工作台 Web/UI：58/58 通过，耗时 80.990 秒。
- MCP、安装脚本、发布包、Agent Context、Agent Gateway 集中回归：35/35 通过。
- 真实 stdio MCP 验收探测：通过。

TypeScript 渲染测试显式把本机 FFmpeg 目录加入 `PATH`；这是测试运行环境要求，不是产品代码回退或绕过。

## 验收边界

- 本次使用隔离任务、临时数据库和受控替身验证 Agent 规划、持久化、门禁、重连和费用保护。
- 未生成真实付费配音或 AI 视频，因此没有验证供应商余额、限流或真实生成质量。
- 未切 Release、未创建远端 PR、未合并回主分支；这些属于代码集成步骤，不影响本地功能验收结论。
