# P1 预览与选材验收报告

日期：2026-08-14

结论：P1「预览缓存 + 人工选材」的核心软件能力已完成，并在真实 241 资产库完成本机技术验证。修复了 3 类真实 bug（跨线程 SQLite、安全删除守卫阻断清理、PNG 探针字段崩溃），新增 `analysis_runs` 阶段标记一致性。代码可由 Codex / WorkBuddy 调用；但不得据此宣称“14 个模块全部端到端通过”：模块 14 样片、普通同事使用和真实操作时长尚未完成。

## 1. 验证环境

| 项目 | 值 |
| --- | --- |
| 验证数据库 | `runtime/media-catalog/p1-verify.sqlite` |
| 素材库 | `p1pub` → `public/`（241 资产：138 mp4 + 100 mp3 + 3 png） |
| 前置 | 全量 scan + probe 241/241（P0 底座） |
| 模型 | faster-whisper `small`，CPU int8，离线缓存 |
| FFmpeg | 8.1.1 full_build（WinGet 安装路径） |

## 2. 模块验收明细

| 模块 | 验证方式 | 结果 |
| --- | --- | --- |
| P1-A 预览缓存 | `preview --library p1pub --workers 4` | 241/241，failed=0；thumbnail/contact_sheet/proxy/audio_preview 共 613 行 |
| P1-B 音频分析 | `analyze-audio --asset` ×2（xinjiang.mp4 / ad1.voice.mp3） | zh 转写、VAD 人声区间、响度异常、联系方式扫描入库成功 |
| P1-C 场景切分 | `segments --library p1pub --workers 4` | 138 视频→152 片段，0 失败；场景切分+兜底（静态口播整片 1 段） |
| P1-C 候选决策 | `select --asset … --decision candidate` | candidate/rejected/confirmed 入库 OK |
| P1-C 任务目录 | `tasks create/list/add-asset/status` | draft→selection_locked 状态机 + 素材挂载 OK |
| P1-D 包装清单 | `packaging register/list` | 水印/标题卡/BGM 等登记 OK |
| P1-E 时间码/调色 | `verify_timecode_consistency` + `set/get_color_params` | 纯 DB，非破坏调色参数读写 OK |
| P1-F 缓存清理 | `cache status` + `cache clean --dry-run` | 613 条 / 150MB / 0 缺失；dry-run 正确报告可释放量且不删 |
| 隐私标记 | `privacy scan/confirm/list` | 读转写→联系方式标记（needs_human_confirm=1）→人工确认置 confirmed=1；无联系方式样本 0 误报 |

## 3. 修复的真实 bug

### 3.1 跨线程 SQLite 错误（关键）
`generate_previews` / `analyze_audio_pending` / `segments_pending` 原在 worker 线程内既读又写 `db`，触发 `SQLite objects created in a thread can only be used in that same thread`，导致批量任务 100% 失败。改为：**主线程读素材信息 + worker 线程只做纯文件 I/O / 模型推理 + 主线程回写**（对齐 P0 `probe.py` 已验证模式）。`privacy.scan_privacy` 为主线程顺序执行，本无此问题。

### 3.2 安全删除守卫阻断临时文件清理，掩盖真实结果
本机回收站不可用 → 沙箱 `[safe-delete]` 守卫 FAIL_CLOSED，对 `unlink` 抛自定义 `FILESYSTEM_ERROR`。`audio_analysis` 在 `finally` 里清理临时 `.transcribe.wav`，异常冒泡导致 `_persist` 永不执行、整条命令返回 `{"ok":false}`。同因还影响 `cache.clean_cache`（`except OSError` 抓不到自定义异常）与 `backup.restore` 的 `temporary.unlink()`。三处均改为 best-effort `try/except Exception: pass`（临时清理绝不掩盖结果；缓存清理在本机可能留孤儿文件但 DB 行删除、dry-run 正常）。

### 3.3 PNG 探针 `format` 字段崩溃
`_duration_of` 假设 `probe.format` 是字典，但 3 个 PNG 资产存的是字符串 `'PNG'`，`fmt.get("duration")` 抛 `AttributeError` 中断整批。加 `isinstance(fmt, dict)` 防御。

### 3.4 阶段标记一致性
`segments._persist` 原只写 `segments` 表，漏写 `analysis_runs` stage='segments' 完成标记（其它阶段均写），导致 `_status` 指标与统一阶段追踪缺失。已补 `ON CONFLICT(asset_id, stage)` upsert，与 preview/audio/probe 一致。

### 3.5 CLI `--json` 子命令不可达
`tasks` / `packaging` 的 `--json` 错挂在父解析器，子命令后追加报 `unrecognized arguments`。已给各子命令补 `--json`（保留父级，两种顺序兼容）；`select` 原本即在子命令上定义故一直可用。

## 4. 关键验证证据（抽样）

- **音频转写**：ad1.voice.mp3 正确捕获口语数字"租金四毛 / 1000平米 / 4.9米-10.5米 / 10吨航车"，证明 Whisper 转写链路可用（与混剪线 make-ass 的数字正则无关，属独立音频分析阶段）。
- **隐私标记**：注入含手机号"13800138000"的转写 → 生成 1 个 contact 标记 → `confirm_flag` 置 `confirmed=1`；干净样本生成 0 标记（无误报）。
- **缓存清理**：`cache clean --dry-run` 正确列举全部 613 条 / 150MB 待释放项，未实际删除（dry-run 安全）。

## 5. 尚未完成的正式验收

- 模块 14 样片尚未制作，不能用本机技术验证替代成片效果验收。
- 普通同事使用和真实操作时长尚未记录，不能把目标手册当成已发布的独立工具。
- 人脸 / 二维码检测为可选项，依赖 OpenCV / pyzbar；本机未装则自动跳过（设计内）。