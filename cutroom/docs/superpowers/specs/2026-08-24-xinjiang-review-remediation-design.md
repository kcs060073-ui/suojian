# 新疆数字人首轮审片返工设计

**日期：** 2026-08-24  
**状态：** Peter 已于 2026-08-24 书面确认
**范围：** 修复字幕同步、接通字幕模板、生成本轮新 B-roll，并产出新的数字人审片候选；不批准审片、不生成发布包。

## 1. 背景与已确认问题

首轮候选 `out/p2-xinjiang.mp4` 的音视频流均从 `0.000s` 开始，视频时长 `69.880s`、音频时长 `69.866s`，因此用户观察到的“不协同”不是播放器或电脑性能造成的 A/V 封装偏移。

问题来自字幕生成：

- 累计模式在句末追加 `0.8s` 停留，并通过相邻不重叠规则把下一句推迟；“同样的逻辑”在声音约 `2.48s` 开始，但字幕到 `3.22s` 才切换。
- 用户文案含 `40%`、`99%`、`100G`，Whisper 词序列使用“百分之四十”“一百计”等口语/误识别形式。现实现按全文字符比例映射，局部差异会扩散为后续漂移。
- packaging plan 选择了 `sub-heavy-orange`，但 assemble 直接烧录既有 `xinjiang.ass`；实际仍是硬编码的 93/100 号思源粗宋样式，包装预设没有参与 ASS 生成。
- 三条实际 B-roll 都是 2026-06-17 的非 `seed_` 旧库素材。本轮没有调用 Seedance 新生成。

首轮候选保持未审批、未发布，作为可追溯的失败样片保留，不覆盖、不删除。

## 2. 目标与非目标

### 目标

1. 字幕由真实 Whisper 词级时间戳驱动，不再因句末停留或全文比例映射产生累积漂移。
2. 简体/繁体差异先统一到简体；数字口语差异由单调序列对齐局部吸收，对齐失败时阻断而不是生成“看似成功”的 ASS。
3. assemble 使用 packaging plan 已确认的字幕 preset 生成 ASS；实际输出样式和 snapshot 中记录的 preset 必须一致。
4. 默认样式改为现代无衬线：阿里妈妈数黑、白字、橘黄关键词、轻描边、最多两行、短句替换，不使用累积堆叠。
5. 只生成三条新的 9:16、1080p、5 秒 Seedance B-roll，每条最多创建一个生成任务；失败时停止，不回退到旧库素材。
6. 新候选写到 `out/p2-xinjiang-v2.mp4`，使用独立验收数据库，自动 QA 通过后停在 `awaiting_human_review`。

### 非目标

- 不更换 FFmpeg/libass 渲染引擎，不引入 Remotion。
- 不在本轮实现全量 PyonFX 动画、逐字弹跳或卡拉 OK 特效。
- 不覆盖首轮候选、旧 B-roll、原 `p1-verify.sqlite` 或首轮验收数据库。
- 不代 Peter 写入 `review approve`，不生成发布包。

## 3. 方案比较

### 方案 A：TypeScript 对齐模块 + Aegisub 模板 + libass（采用）

- 把时间对齐从 `make-ass.ts` 拆到纯函数模块。
- 固定依赖 `opencc-js@1.4.1`，把 Whisper 繁体转为简体。
- 使用 Needleman–Wunsch/编辑距离式单调对齐，把用户文案字符映射到 Whisper 字符时间；数字口语差异只影响局部。
- Aegisub 作为人工设计和预览模板的工具，运行时继续由 TypeScript 读取 `subtitle-presets.json` 并生成 ASS，FFmpeg/libass 烧录。

优点：保留现有主流水线、改动边界最小、容易写确定性测试。缺点：需要维护一个小型对齐算法。

### 方案 B：Python + pysubs2 重写 ASS 生成

优点：字幕对象和样式 API 成熟。缺点：重写现有 TypeScript CLI，增加跨语言切换和 Python 依赖面，本轮风险较大。

### 方案 C：PyonFX 透明字幕视频层

优点：动画表现力强。缺点：渲染链和资源成本明显增加，复杂效果会掩盖同步问题，不适合作为本轮修复入口。

## 4. 字幕同步设计

新增 `src/subtitle-alignment.ts`，暴露：

```ts
export interface TimedWord { w: string; start: number; end: number }
export interface AlignedChar { scriptIndex: number; start: number; end: number; confidence: number }

export function alignScriptToWords(
  scriptText: string,
  words: TimedWord[],
  speed: number,
  leadSeconds: number,
): { chars: AlignedChar[]; coverage: number }
```

算法步骤：

1. 去除不发音标点与空白，但保留原文索引。
2. 用 `opencc-js` 将 Whisper 字符转换为简体；英文字母统一大写，全角数字转半角。
3. 为 Whisper 每个字符按词的起止时间均分，保留原速时间。
4. 对“脚本文字序列”和“Whisper 字符序列”做单调全局对齐：相同字符成本 0，替换成本 1，插入/删除成本 1；禁止倒序映射。
5. 匹配字符直接使用对应 Whisper 时间；未匹配的短区间在左右已匹配锚点间插值，不能跨越下一锚点。
6. 转成最终成片时间：`sourceTime / speed - leadSeconds`。
7. `coverage < 0.80`、任何 cue 无时间锚点、时间倒退或 cue 超出目标成片时长时，ASS 生成失败关闭并输出稳定错误码。

生成规则：

- 默认 `replace`，禁止对下一句施加句末 dwell。
- 每个 cue 起点来自首个对齐字符，终点来自最后一个对齐字符；只允许最多 `0.12s` 的视觉尾垫，不得推动下一 cue。
- 相邻 cue 有重叠时取中点切分，不允许简单把后一 cue 推到前一 cue 结束。
- 回归锚点：“同样的逻辑”在 1.25 倍速、提前 0.08 秒时应于 `2.40s ± 0.12s` 出现。

## 5. 字幕样式与包装绑定

新增 preset `sub-modern-clean-v1`：

- 字体：`5 Alimama ShuHeiTi 阿里妈妈数黑.otf`
- 普通字号：72；金句字号：82
- 普通文字：白色；关键词与数字：`#FD9437`
- 黑色描边 4px、阴影 1px，不使用大面积纯色背板
- 底部安全区：`MarginV=430`
- 每行最多 12 个全角字符，最多两行
- 普通 cue 使用 `\fad(60,80)`；不做逐字弹跳

`make-ass.ts` 必须接受 `--preset sub-modern-clean-v1`，并从 manifest 读取字体、字号、位置和颜色，不再用硬编码样式覆盖 preset。编排器在渲染前根据 confirmed packaging plan 生成任务专属 ASS 到 `runtime/jobs/<task>/subtitles.<digest>.ass`；该 ASS 的文件签名、preset id 和 alignment coverage 进入 dependency snapshot。调用方传入与 packaging plan 不一致的 ASS 时失败关闭。

## 6. 新 B-roll 设计

为 v2 固定生成三条新素材，使用现有 `generate-broll.ts` 和已声明的 Seedance 配置：

1. 新疆戈壁风电场：大型白色风机、金色晨光、缓慢航拍横移、无文字/Logo/人物特写。
2. 现代数据中心机房：蓝色服务器指示灯、冷通道、镜头缓慢推进、无文字/Logo。
3. 新疆算力园区外景：现代园区建筑与能源设施、远景、纪录片风格、无招牌文字。

运行约束：

- 9:16、1080p、5 秒、无水印。
- 每条最多提交一次 Seedance 任务，不自动付费重试。
- 文件名包含 v2 run id，不覆盖旧 `seed_*.mp4`；保存模型、任务 ID、prompt、生成时间、SHA-256。
- 生成后只登记到独立 v2 验收数据库，场景卡改绑新 asset_id，再重新 picture lock 和确认 packaging plan。
- 任一新素材失败、缺文件、探针失败或未登记时，渲染阻断；禁止静默回退旧素材。

## 7. 数据流与产物

```text
script.txt + words.json
  → 简繁/数字归一化 + 单调词级对齐
  → confirmed subtitle preset
  → runtime/jobs/p2-xinjiang-v2/subtitles.<digest>.ass

3 个新生成 B-roll
  → v2 SQLite asset_id / scene_cards / picture lock
  → render-mix.plan.json
  → out/p2-xinjiang-v2.mp4
  → automatic QA
  → awaiting_human_review
```

v2 数据库通过 SQLite backup API 从首轮验收库建立；首轮 snapshot、QA 和候选文件继续保留。

## 8. 验收标准

### 自动测试

- 简繁体文本可对齐。
- `40% ↔ 百分之四十`、`99% ↔ 百分之九十九` 不造成后续 cue 漂移。
- 句末视觉尾垫不推动下一句。
- 低覆盖率、倒序、空词时间轴失败关闭。
- confirmed preset 与实际 ASS 样式、字体依赖及 snapshot 一致。
- 新 B-roll 不能复用首轮三个 SHA-256，不能覆盖旧文件。

### 真实候选

- H.264/AAC、1080×1920、24–60fps、音视频同起点。
- QA blocking count 为 0。
- 抽查首段、三处 B-roll 进入/退出和尾段，字幕锚点无明显漂移。
- 数据库状态返回 `awaiting_human_review`；`review_approvals=[]`、`publish_records=[]`。
- Peter 完整观看并明确批准前，任务不得进入发布包阶段。

## 9. 安全与回滚

- 所有开发继续位于 `codex/p1-p2-recovery` 隔离分支。
- 所有代码改动先写失败测试，确认 RED 后再实现 GREEN。
- Seedance 生成是唯一付费外部动作，严格限制 3 次创建请求；不自动重试。
- v2 使用新 DB、新任务 ID、新输出路径；删除任何旧产物都不属于本设计。
