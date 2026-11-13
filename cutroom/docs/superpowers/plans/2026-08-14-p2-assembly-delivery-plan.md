# P2 剪辑组装与交付 · 实施计划

> 日期：2026-08-14
> 父流程：视频生产总工作流（P0 数据底座 / P1 预览选材 已完成真实验收）
> 目标：两类真实任务（项目 + 数字人）**从素材 ID 生成可发布成片**，四道人工门禁全程可追溯，BGM 不压人声。
> 风格：每条先说"为了提升什么体验"，再说技术动作，避免堆术语。

## 0. 当前已具备（不重复造）

| 已有能力 | 位置 | P2 怎么用 |
| --- | --- | --- |
| 素材底座（扫描/探针/库/任务/选材/预览/音频粗转写/场景片段/隐私标记） | `media_catalog/` 包 | 场景卡片、QA、发布包都建在它之上 |
| 数字人混剪统一渲染（倍速+粗宋字幕+画中画+全屏B-roll+标题卡+水印+BGM） | `scripts/render-mix.ts` | P2 项目/数字人成片的主渲染引擎，补 audio_policy + ducking |
| 信息流广告渲染 | `scripts/render-ad.ts` | 广告线沿用，P2 只补声音策略一致性 |
| 词级时间戳 + 文案校正 | `scripts/transcribe.py` + `make-ass.ts` | 累积字幕、声音对齐的时间基准 |
| 包装资产库（水印/标题卡/BGM 登记） | `media_catalog/packaging.py` | 升级为"包装方案 packaging_plan"的数据源 |

## 1. 大白话：P2 给用户带来什么

| 模块 | 用户视角价值 | 技术动作 |
| --- | --- | --- |
| 场景卡片 + 画面锁定 | 把"哪段用哪段素材、怎么切"从聊天记录变成可追查的卡片；锁定后系统知道改文案会重跑哪些环节 | 新 `media_catalog/scene_cards.py` + DB 表 + CLI |
| 累积字幕 | 字幕按口播一句句叠加，最后整句呈现，手机看更顺 | `make-ass.ts` 加 `--mode cumulative` |
| 原声策略 + BGM 压低 | BGM 不再盖过人声，手机外放也听得清 | `render-mix.ts`/`render-ad.ts` 支持 keep/mute/duck/clean/replace + sidechain/loudnorm 压低 |
| AI 音色库 + 包装方案 | 新人 clone 就知道有啥音色/BGM/贴纸，试听后一键套用，不用猜内部 ID | 资产 manifest（voice/bgm/sfx/字幕模板/文字卡/贴纸/花字）+ CLI `assets catalog/recommend` |
| 自动质检 | 出片前自动扫一遍，先把能算的毛病挑出来，人只盯"感觉" | 新 `media_catalog/qa.py`（ffprobe 黑帧/冻结/静音/削波/字幕越界/重复镜头/依赖不一致） |
| 发布准备与合规 | 成片→发布包一步到位（封面+标题+简介+标签+敏感检查），三平台共用，不自动上传 | 新 `media_catalog/publish.py` |
| 渲染快照 + 局部重跑 | 改一个字不必全片重渲，省时间 | 每版本存 文案/分镜/素材ID/指纹/时间码/字幕/声音/包装；只重跑受影响环节 |
| 端到端编排 | 从素材 ID 直接出一版能发的片子，四门禁全程可追 | `media_catalog` CLI `assemble <task>` 串起上面所有环节 → 产出 2 条真实样片（项目+数字人）= P2 验收门禁 |

## 2. 模块切分与顺序（建议）

### 模块 1 · 场景卡片 + 画面锁定（scene_cards）— 地基
- 新增 `media_catalog/scene_cards.py` + 迁移：`scene_cards`（task_id, idx, narrative_text, narrative_start, narrative_end, asset_id, segment_id, mix_form[数字人全屏/B-roll全屏/画中画/信息卡], enter_tc, exit_tc, audio_policy_draft, source_type[用户指定/系统推荐/长期库/缺口替代], system_suggestion, final_decision, confirm_status）、`picture_lock`（task_id, version, locked_at, changed_since_lock）。
- CLI：`scene-card add/list/set-mix/set-audio/confirm`、`picture-lock set`。
- 验收：建 1 条数字人任务的场景卡片池 → `confirm` → `picture-lock set` 落锁。

### 模块 2 · 累积字幕模式（make-ass cumulative）
- `make-ass.ts` 加 `--mode cumulative|replace`（默认 replace 兼容现状）。
- cumulative：在语义句内划分短语，生成时间连续的多个 ASS Dialogue，每段含此前已出现的短语；固定锚点不跳动；不切断数字/关键词。
- 验收：用 xinjiang 文案产出 cumulative ASS，手机尺寸审看不越界、不腰斩。

### 模块 3 · 原声策略 + BGM 压低（audio policy + ducking）
- `render-mix.ts` 读 scene_cards 的 `audio_policy`：keep（原声）/mute（静音）/duck（环境声压到主声下）/clean（基础降噪+限幅）/replace（主旁白/BGM 替）。
- BGM 在主声区间用 `sidechaincompress` 或 `volume` 动态压低，无人声段恢复；经验起点 BGM≈-19dB，正式阈值等样片校准（底层存响度数据）。
- 验收：数字人样片 BGM 段人声 RMS > BGM RMS（ffprobe/volumedetect 验证"不压人声"）。

### 模块 4 · AI 音色库 + 包装方案（voice_library + packaging_plan）
- 资产 manifest：`assets/manifests/{voice-profiles,bgm,sfx,subtitle-presets,visual-cards,stickers,fancy-text-presets}.json`（仓库核心资产）；`assets-local/` 本地扩充（Git 忽略）。
- CLI `assets catalog`（离线 HTML 目录，试听/预览/筛选）、`assets recommend <task>`（按路线推 2–3 套方案）、`assets validate`。
- `packaging_plan` 落库：每方案含 asset_id/版本/备用项；无论命中默认模板都必须人工确认。
- 验收：数字人任务推 2–3 套方案 → 试听 → 确认 → 输出 packaging_plan（含 BGM/字幕模板/文字卡/贴纸/花字 ID）。

### 模块 5 · 自动质检（qa）
- 新增 `media_catalog/qa.py`：ffprobe 检查 播放/编码/比例/分辨率/帧率；黑帧/冻结帧（黑场占比）、异常静音、削波（峰值>0dB）、音量突跳；字幕越界/断句（读 ASS）；BGM 人声优先级（读 audio_policy+响度）；重复镜头/长镜头（读 segments+scene_cards）；素材缺失/指纹变化/渲染依赖不一致；成片新出现人脸/车牌/二维码/联系方式风险（读 privacy 标记增量）。
- 输出 `qa_report`（阻断项/警告/人工决定），不替代完整人工观看。
- 验收：对一条已知有黑帧/静音的测试片能标出，对正常片 0 阻断。

### 模块 6 · 发布准备与合规（publish）
- 新增 `media_catalog/publish.py`：从 final MP4 抽 2–3 封面候选（清晰/主体明确/无字幕残影）；基于已确认文案生成 主标题+2–3 备选/简介/标签（不编造政策/金额）；只查新增封面与发布文字的 敏感表达/事实/隐私（读 qa_report 不重复扫）；输出发布包 `deliverables/publish/<task>/`。
- 不自动登录/上传；用户有自己的封面生成器则只导出关键帧+品牌信息，发布包标记"待补封面"。
- 验收：数字人样片产出发布包（video-final + cover + publish-copy.md + publish-check.json + qa-report.md）。

### 模块 7 · 渲染快照 + 局部重跑（render snapshot / rerun）
- 每候选/正式渲染存依赖快照（文案/分镜/素材ID/指纹/时间码/字幕计划/声音策略/包装配置）到 `render_snapshots` 表，不覆盖历史版本（rough-v1/candidate-v1/final-v1…）。
- 文案改只重跑受影响环节：错字→字幕；单句改→该句配音+字幕+镜头+后续对齐；增删段落→重排后续时间线；最终 MP4 可能全片重渲但不重扫素材库。
- 验收：改 1 个字 → 只重渲字幕层，不重跑 probe/音频分析。

### 模块 8 · 端到端编排（assemble）— P2 验收门禁
- 新增 `media_catalog/assemble.py` + CLI `assemble <task>`：读 scene_cards + packaging_plan → 驱动 render-mix（数字人）/ 项目渲染 → 产出 candidate → qa →（人工审片）→ publish。
- **验收门禁**：项目 + 数字人各 1 条真实端到端样片；四道人工门禁（文案锁定→画面顺序锁定→包装确认→完整观看）全程可追溯；BGM 不压人声。

## 3. 四道人工门禁（全程可追溯，P2 硬要求）

| 门禁 | 触发点 | 落锁位置 |
| --- | --- | --- |
| 文案锁定 | 最终文案/配音确认 | `tasks.status` |
| 画面与顺序锁定 | picture-lock | `picture_lock` 表 |
| 包装确认 | packaging_plan 确认 | `packaging_plan.confirmed` |
| 完整观看 | 候选成片人工确认 | `qa_report` final 标记 |

## 4. 当前不实施（边界，同总控第 13 节）
- 不自动登录/上传任何平台。
- 不做平台派生版本（三平台共用 9:16）。
- 不做剪映原生多轨工程（只导出可编辑素材包）。
- 不建复杂版权审批流/综合推荐分数（只给理由+可用状态+警告）。
- 样片（验证用）即真实任务产出，不单独造"演示片"。

## 5. 验收总门禁（决定 P2 是否算完）
1. 项目 + 数字人各完成 1 条真实端到端样片（从素材 ID → 发布包）。
2. BGM 不压人声（手机外放/普通耳机验证）。
3. 四道人工门禁在 DB/文件中全程可追溯，未确认不能进下一环节。
4. 自动质检 0 阻断项 + 人工完整观看确认。
5. 改 1 字只重跑受影响环节（不全会重渲/重扫）。

## 6. 待 Peter 确认
- 是否按全计划 8 模块执行（不砍范围）？
- 模块 8 两条真实样片：用现有 `public/` 里的 xinjiang（数字人）+ 一条项目素材，还是你指定具体任务？
- 包装资产 manifest：先用仓库现有 BGM/标题卡/字幕能力建最小 manifest，还是等扩展包？
