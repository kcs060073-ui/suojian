# 剪辑工作台自启与普通同事真实任务验收实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 验证独立目录的 Windows 登录自启链路，并把普通同事首条真实数字人任务整理成可执行、可签收的验收流程。

**Architecture:** 复用现有 `CutroomLocal` 计划任务与 `workbench/start-local.ps1`，不新增第二套守护程序。自启结果写入一次性验证报告；人工试运行采用独立验收单，明确自动证据和人工判断边界。

**Tech Stack:** Windows Task Scheduler、PowerShell、Python HTTP/SQLite 工作台、Markdown、Git。

**Spec:** `docs/user-guide/cutroom-user-guide.md`

## Global Constraints

- 不执行真实 Windows 注销；以计划任务停止、服务停止、计划任务启动复现登录触发的同一 Action。
- 不修改生产 SQLite 内容，不读取或输出任何密钥。
- 普通同事验收必须由真实同事本人操作，不得由维护人员代填“通过”。
- 首次验收只跑一条真实数字人混剪；项目宣传片仍不开放。

---

### Task 1: 登录自启链路冒烟

**Files:**
- Create: `docs/validation/2026-09-16-cutroom-autostart-smoke.md`

- [ ] 记录计划任务 Action 和工作目录。
- [ ] 停止计划任务和受控服务进程，再启动 `CutroomLocal`。
- [ ] 验证任务状态、8765 进程、HTTP、安全边界和 SQLite 完整性。
- [ ] 写入验证报告并注明未实际注销 Windows。

### Task 2: 普通同事首条真实任务验收单

**Files:**
- Create: `docs/validation/cutroom-colleague-first-real-task-acceptance.md`
- Modify: `README.md`
- Modify: `docs/user-guide/cutroom-user-guide.md`

- [ ] 定义普通同事与维护人员角色边界。
- [ ] 覆盖安装启动、素材登记、任务创建、四个人工门禁、渲染、QA、完整审片和七文件发布包。
- [ ] 记录人工时间、总时间、返工点、阻断与恢复方式。
- [ ] 从 README 和同事手册建立入口。

### Task 3: 回归、提交与 GitHub 同步

**Files:**
- Test: `tests/test_release_packaging.py`

- [ ] 新增文档契约测试。
- [ ] 运行定向测试观察 RED。
- [ ] 完成文档后观察 GREEN，并运行发布专项、Web UI 和类型检查。
- [ ] 运行差异与敏感信息检查，提交并推送 GitHub。