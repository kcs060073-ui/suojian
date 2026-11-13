# P2 数字人路线 · v2 样片验收报告（新疆算力口播）

- 状态：**已验收（数字人路线闭环）**
- 日期：2026-09-04
- 关联设计：[v2 返工修复方案](../superpowers/plans/2026-08-24-xinjiang-review-remediation-plan.md) / [v2 设计](../superpowers/specs/2026-08-24-xinjiang-review-remediation-design.md)
- 验收库：`runtime/media-catalog/p2-v2-acceptance.sqlite`（28 表、241 资产，由 p1-verify 在线备份复制）
- 任务：`p2-xinjiang-v2`（数字人混剪）
- 成片：`out/p2-xinjiang-v2.mp4`，发布包：`deliverables/publish/p2-xinjiang-v2/`

## 背景：为什么返工

v1 样片被 Peter 判返工，三个问题：

1. **字幕同步漂移**：Whisper 整句时间戳误差导致字幕晚约 0.7s；
2. **数字误识别**：Whisper 将 40%/99% 误识别并造成累积漂移；
3. **包装预设未生效**：烧的是旧硬编码字幕样式，而非选定的现代包装预设。

v2 修复策略：单调词级对齐引擎 + `sub-modern-clean-v1`（阿里妈妈数黑）字幕样式 + 新 B-roll。

## 执行结果

### 门禁链（四道人工门禁全部落库）

| 门禁 | 状态 | 凭证 |
|---|---|---|
| 文案锁定（brief） | ✅ 已落 | tasks → brief_locked |
| 画面锁定（picture-lock） | ✅ 已落 | picture-lock-v1 |
| 包装确认（packaging） | ✅ 已落 | 方案A `pp-a71896dee5c7`（subtitle=sub-modern-clean-v1 + bgm + 品牌框 + 音效 + 配音） |
| 人工完整审片（reviewed） | ✅ 已落 | `approval-cb2b2ace2743406d`，Peter 完整观看通过 |

### 渲染与质检

- 渲染耗时：283.6s（`assemble --render`，ffmpeg 烧字幕 + B-roll 全屏 + 画中画 + 包装）
- 成片规格：**1080×1920 · 25fps · 69.88s · 40,149,116B**（sha256 `56900817…`）
- 自动 QA：`qa-142edf9685b9`，**19 项全部通过、0 阻断**（含黑场/冻结/静音 0s/削波 -0.1dB/画幅 9:16/编码 h264）
- 字幕对齐覆盖率：0.87（单调词级对齐引擎）

### 字幕核验（v2 修复是否生效）

逐项核验（抽帧 8s/20s/35s + OCR + ASS 源码比对）：

- [x] **同步**：字幕与口播对齐，无累积漂移；
- [x] **数字准确**："40%以上""21个项目""5300P""4000P"均与用户文案原文一致，非 Whisper 误识别；
- [x] **样式生效**：`sub-modern-clean-v1` 真实烧录——阿里妈妈数黑 72 号正文 / 82 号金句，白色正文 + 关键词橘黄 `#FD9437`，短句拆行、金句整屏；
- [x] **标签泄漏修复**：首版渲染发现 `\fad(60,80)` 因缺花括号被 libass 渲染成可见文本，已修复 `src/subtitle-ass.ts`（`\fad(...)` → `{\fad(...)}`，git `08389a5`）并重渲染确认消失。

### 发布包（合规 cleared）

`deliverables/publish/p2-xinjiang-v2/`（`pub-56a865ff3354`，平台=抖音，无敏感词命中）：

| 文件 | 说明 |
|---|---|
| `video-final.mp4` | 发布成片（42MB，与 QA 快照同一字节） |
| `cover-01/02/03.jpg` | 3 张平台封面 |
| `publish-copy.md` | 发布标题/标签/简介 |
| `qa-report.md` | 自动质检报告（19 项） |
| `publish-check.json` | 合规检查（status=cleared） |

## 结论与限制

**数字人混剪路线已端到端闭环并验收**：素材登记 → 场景卡片 → 画面锁 → 包装 → 自动质检 → 人工审片 → 合规发布，全程门禁可追溯，QA 0 阻断。

遗留与限制（不影响本样片验收）：

- **B-roll 未按 v2 设计重新生成**：Seedance 付费生成被豁免，三处 B-roll 复用现有已登记素材（新疆风力发电_绿电 / 昌吉新疆信息产业园 / 沙特石油_新疆数据中心）。后续若需全新 B-roll 可走 `npm run gen-broll` 补齐后重跑 compile。
- **项目样片路线仍未验收**：P2 验收要求"项目 + 数字人各 1 条"，项目样片仍在 backlog。
- 本验收基于 `p2-v2-acceptance.sqlite`（验收专用副本），不污染主库 `catalog.sqlite`。
