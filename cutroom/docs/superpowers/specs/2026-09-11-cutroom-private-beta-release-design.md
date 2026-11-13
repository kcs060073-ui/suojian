# 剪辑工作台私有仓库与 Beta Release 设计

状态：已批准实施  
日期：2026-09-11  
目标版本：`v0.9.0-beta.1`

## 1. 目标与边界

本轮把已经在 Peter 本机跑通的剪辑工作台固化为可追溯的私有源码仓库，并交付一个供内部同事试用的 Windows Beta 分发包。源码、测试和内部开发文档只进入 GitHub 私有仓库；本轮不创建公开下载仓库，不对外公开产品。

以下内容不得进入 Git 或 Release：API Key、Windows 凭据、`.env`、运行数据库、缓存、任务产物、客户素材、数字人源视频、配音成品、最终视频、个人本地素材和调试日志。

## 2. 仓库与版本结构

- 私有源码仓库：`qnwhageqlavs-jpg/cutroom-private`。
- 默认分支：`main`。
- 首个分发标签：`v0.9.0-beta.1`，GitHub Release 标记为 prerelease。
- 当前 `codex/colleague-workbench-phase1` 的受控改动在完整回归和敏感信息扫描通过后并入 `main`。
- 暂不创建 `cutroom-downloads`。只有普通同事试用通过且 Peter 决定允许公众下载时，才建立仅含说明和 Release 附件的公开下载仓库。

## 3. Beta 分发包

首版采用“Windows 在线安装型便携包”，而不是 MSI/EXE 安装器。Release 附件包含：

- `剪辑工作台-v0.9.0-beta.1-windows-x64.zip`：应用代码、Web UI、核心清单、授权素材、安装与启动入口。
- `剪辑工作台-v0.9.0-beta.1-SHA256SUMS.txt`：分发包哈希。
- Release notes：能力范围、安装步骤、已知边界和回退方式。

用户解压后双击 `安装CUTROOM.bat`。安装器在应用目录内创建 Python 虚拟环境，安装锁定的 Python/Node 依赖，检查 FFmpeg/ffprobe，并生成可双击启动入口。缺少 Python 3.11、Node.js LTS、FFmpeg 或 winget 时，安装器给出中文可执行提示并停止，不静默失败。Beta 安装允许联网，不承诺离线安装。

## 4. 可重复构建

发布包由仓库内脚本从明确允许清单生成，不从工作区直接全量压缩。构建器必须：

1. 只复制运行所需目录和用户文档。
2. 硬拒绝 `.env`、`runtime/`、`out/`、`assets-local/`、`.git/`、`.worktrees/`、缓存、日志和媒体任务输入。
3. 生成包内 `release-manifest.json`，记录版本、Git 提交、文件路径、大小和 SHA-256。
4. 生成 ZIP 与外部 SHA-256 文件。
5. 在临时目录解压后检查启动入口、核心素材和清单完整性。

## 5. 授权处理

- Release 必须包含 `THIRD_PARTY_NOTICES.md` 和素材包现有许可证副本。
- Kenney CC0、Smiley Sans/OFL、思源字体/OFL 的来源和许可证保留。
- Scott Buckley 的 5 首 CC BY 4.0 BGM 只用于内部 Beta，继续标记为“发布时必须署名”；Release notes 明示该义务。
- 公开下载版出现前，需二选一：补齐自动署名流程，或从公开包移除这 5 首 BGM。

## 6. GitHub 发布流程

1. 运行完整 Python、TypeScript、Web UI 回归以及清单校验。
2. 扫描待提交文件与 Git 历史，确认不含凭据和运行数据。
3. 将功能代码、素材清单、用户手册、发布脚本和测试提交到私有仓库。
4. 构建并在隔离临时目录验证 ZIP。
5. 将当前分支快进或合并到 `main`，推送私有仓库。
6. 创建 `v0.9.0-beta.1` tag 和 prerelease，上传 ZIP 与 SHA-256 文件。
7. 通过 GitHub API 回读仓库可见性、tag、Release 状态、附件大小和摘要。

## 7. 完成标准

- GitHub 仓库存在且为 private。
- `main`、`v0.9.0-beta.1` 和 prerelease 均指向同一已验证提交。
- Release 附件可以由有仓库读取权限的账号下载。
- 完整测试、敏感信息扫描、清单校验、分发包结构检查和隔离解压烟测全部通过。
- 用户手册包含安装、启动、升级、卸载/回退、模型配置和素材授权说明。
- 明确记录：本机隔离烟测不等于普通同事电脑验收；完成一次同事电脑实测后才允许改发 `v1.0.0`。
