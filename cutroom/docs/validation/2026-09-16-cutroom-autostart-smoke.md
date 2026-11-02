# CUTROOM 新目录登录自启链路冒烟

- 日期：2026-09-16
- 代码版本：`v0.9.0-beta.3` / `01dfc110e0b764cbfe2871af430687d051ae08c3`
- 项目目录：`E:\我的AI项目\CUTROOM`
- 结论：**通过（计划任务 Action 等价冒烟）**

## 验证范围

本次不执行真实 Windows 注销，避免中断当前工作会话。验证使用登录触发器会执行的同一个 `CutroomLocal` Action，按“停止计划任务 → 停止已核验的 CUTROOM 服务进程 → 启动计划任务 → 等待 HTTP 就绪”复现冷启动链路。

## 当前配置

- 任务名：`CutroomLocal`
- 任务状态：`Running`
- Action：PowerShell 调用 `E:\我的AI项目\CUTROOM\workbench\start-local.ps1`
- WorkingDirectory：`E:\我的AI项目\CUTROOM`
- 服务模块：`python -m workbench.server`
- 数据库：`E:\我的AI项目\CUTROOM\runtime\workbench\catalog.sqlite`
- 地址：`http://127.0.0.1:8765/`

## 冒烟结果

| 检查项 | 结果 | 判定 |
| --- | --- | --- |
| 计划任务重新启动 | 状态 `Running` | 通过 |
| 8765 监听进程 | 命令行指向新目录数据库 | 通过 |
| 首页 | HTTP 200 | 通过 |
| 未授权 Bootstrap API | HTTP 403 | 通过，授权门禁生效 |
| SQLite 完整性 | `integrity_check=ok` | 通过 |
| 源码工作目录 | 新目录 `E:\我的AI项目\CUTROOM` | 通过 |

## 启动时序说明

端口开始监听后，第一次 HTTP 请求曾在 10 秒内超时；随后按应用就绪条件再次检查，首页立即返回 HTTP 200。以后自启验收必须以“HTTP 200 + 新目录进程 + 数据库完整”为完成条件，不能只凭端口监听判定就绪。

## 证据边界

- 已验证计划任务使用的实际 Action，可以停止旧进程并由任务重新拉起新目录服务。
- 未执行真实注销/重新登录，因此没有把本次结论扩大为完整 Windows 会话重建验收。
- 普通同事电脑的公司网络、安全策略、移动硬盘和真实素材仍需按独立验收单执行。
