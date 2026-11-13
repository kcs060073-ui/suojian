# AGENTS.md · 数字人混剪自动化（Codex / 通用代理）

> 本文件给 OpenAI Codex 等编程代理读。内容与 `CLAUDE.md` 等价，照此操作即可跑通同一套流程。
> 前提：本机有真实 shell + 已装 **FFmpeg / Node / Python(.venv 内 faster-whisper)**，并能访问项目文件与素材。

## 任务

把「数字人口播 MP4 + 文案」自动加工成**带样式字幕的 9:16 竖屏短视频**。技术栈：**FFmpeg 渲染 + faster-whisper + ts-node 脚本**。

涉及本机或移动硬盘素材登记、盘点、搜索、重连或恢复时，Codex 与 WorkBuddy 必须共同遵守 [`docs/operations/media-catalog-agent-contract.md`](docs/operations/media-catalog-agent-contract.md)，不得各自维护一套素材目录逻辑。移动硬盘按 `local` 类型登记；NAS 仅保留底层兼容，不属于当前验收门禁。

> ⚠️ 不要改用 Remotion：本机 360 内核驱动 `sysdiag.sys` 拦截文件 I/O，会让 Remotion 下载临时文件后立刻 ffprobe 读取失败（moov atom not found）。FFmpeg 直接读源文件，不受影响。

## 主流水线（数字人混剪 · 全自动）

主出口是**统一引擎 `render-mix.ts`**：倍速 + 粗宋橘黄字幕 + B-roll + 画中画 + 标题卡 + 水印 + BGM 一次烧成。

```
口播 MP4(9:16) + 文案
  ① transcribe.py   faster-whisper → <name>.srt + <name>.words.json（词级时间戳）
  ② plan-brief      文案 → <name>.brief.json（重点词/金句/标题/B-roll提示词）+ plan.json  ← 火山方舟 LLM
  ③ compile-plan    brief + 词级时间戳 + 本地库 → <name>.plan.json（锚点定位 + 素材匹配）
  ④ gen-broll       待补 B-roll → Seedance 文生视频 → 本地库（或 fetch-broll 走 Pexels）→ 重跑 compile
  ⑤ make-ass        文案 + 词级时间戳 + brief → <name>.ass
  ⑥ render-mix      → out/<name>.mp4
```

命令（PowerShell；跑前清空死代理 `$env:HTTP_PROXY=""; $env:HTTPS_PROXY=""`；key 放 `.env`，脚本 import "./load-env" 自动读）：
```
.venv\Scripts\python scripts/transcribe.py public/digital_human/<name>.mp4 small
#   文案写入 public/scripts/<name>.script.txt
npm run plan <name>          # ② AI 出 brief（profile 默认招商；财税加参数 caishui）
npm run compile <name>       # ③
npm run gen-broll <name>     # ④（或 npm run fetch-broll <name>）→ 然后 npm run compile <name>
npx ts-node scripts/make-ass.ts <name> 1.25   # ⑤
npm run mix <name>           # ⑥ 出片
```

**关键**：Whisper 出繁体+错字 → 保留**时间戳**，文本换成**用户文案原文**（make-ass 已用 script.txt 实现，无需手改）。

## 信息流广告管线

```
npm run plan-ad <name>      # 文案 → adplan.json 分镜（AI）
npm run tts <name>          # 配音 + adtiming.json 句级时长
npm run compile-ad <name>   # 逐句配片 → adrender.json（缺的列 pending，补素材后重跑）
npm run render-ad <name> [bgm]   # xfade 转场 + 字幕 + 配音 → out/（短素材自动循环、缺素材占位兜底）
```

> 旧的 `build-storyboard + batch`（`ffmpeg-render.ts`）是早期关键词叠加支线，**已被 render-mix 取代**，新片走 mix。

## 剪辑规范（每条片子执行）

字幕：**思源粗宋**，普通 93号·金句 100号；普通**白**、重点**橘黄 #FD9437**（数据/地点/项目名上色）；**单行不换行**、拆短句、金句整屏；距底 560px；整体提前 0.08s；整体 **1.25 倍速**。
参数集中在 `scripts/make-ass.ts` 顶部（`FONT_SIZE/PUNCH_SIZE/MAX_LEN/MARGIN_V/LEAD_SEC/KEYWORDS/PUNCH_LINES`）。
**拆句铁律**：按标点打包短句、超长才硬拆、**绝不切在数字/关键词中间**（否则出现 "0多亿""里了，" 这类腰斩碎片）。
B-roll / 贴纸 / 音效 / BGM(-19) / 转场：**依赖用户素材**，无素材时跳过并说明。

## 素材摆放（同名匹配）

| 目录 | 放什么 |
|------|--------|
| `public/digital_human/<name>.mp4` | 口播 MP4（已合成背景，ASCII 名） |
| `public/scripts/<name>.script.txt` | 文案；同名生成 srt/ass/words.json |
| `public/inserts/` | 插入图片/小视频（对应 keywords.json） |
| `assets/fonts/SourceHanSerifSC-Heavy.otf` | 思源粗宋 |
| `out/<name>.mp4` | 成片 |

## 环境备注

- 系统 `HTTP_PROXY/HTTPS_PROXY` 指向死代理 → npm/pip 安装前清空（渲染走 FFmpeg 不联网）
- 字幕路径转义脆：render-styled.ts 已把 ass 写到系统临时 ASCII 目录、cwd 设此、用相对名 + fontsdir，勿改这套规避逻辑
- 数字人素材是「已合成背景 MP4」，不抠像；换绿幕需在底层加 colorkey

## 日常 SOP

用户给 **口播 MP4 + 文案** → 跑 ①②③ 出片，`out/` 取片。有 BGM/贴纸素材则一并加上。

> 数字人路线 v2 样片已端到端验收（四道门禁 + QA 0 阻断 + 合规发布包），结论见 [P2 数字人 v2 样片验收报告](./docs/validation/2026-08-24-xinjiang-v2-acceptance.md)；项目样片路线仍未验收。

## 素材管理与预览选材（P1）

素材底座是 `media_catalog` 包（SQLite 主账本），P1 在其上提供预览缓存、音频分析、场景切分、人工选材、任务目录、包装清单、隐私标记、缓存清理。命令入口 `python scripts/media_catalog.py --db <库> <子命令>`，常用子命令与验收结论见 [P1 预览与选材验收报告](./docs/validation/p1-media-catalog-acceptance.md) 与 README 的「P1 预览与选材」小节。渲染主流程（render-mix 等）不改这部分数据。

<!-- DEVELOPMENT-PROGRESS-REPORTING:START -->
## 开发进度主动汇报
本项目沿用Codex全局AGENTS的自动汇报规则及[统一约定](E:/AI-memory-Hub/wiki/20_SOP/产品开发/开发进度主动汇报约定.md)。此处只维护以下项目事实入口，通用规则统一维护。
<!-- DEVELOPMENT-PROGRESS-REPORTING:END -->
### 本项目进度事实入口

- 使用当前工作树的 `docs/implementation/README.md` 定位唯一验收台账和当前计划；活动台账为 `docs/validation/requirements-99-progress.json`，执行说明为 `docs/validation/product-acceptance/execution-status.md`。文件缺失时查对应Session中的实际开发工作树，不拿主目录旧版替代开发进度。
- 汇报前核对实际Git差异和适用验收证据；不得从旧报告照抄99项状态。数字人、横竖广告、剪映交付、同事使用分别验收。
