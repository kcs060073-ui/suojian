# 溯剪 / SuoJian

一个本地“参考成片 → 相似剪映草稿”Demo。拖入成片后先输出可读的 `edit_blueprint.json` 与多轨工作台；用户输入新脚本并明确确认后，再输出独立的 `draft_blueprint.json` 和基础可编辑剪映草稿。

## 快速启动

前置条件：Python 3.11+、FFmpeg（`ffprobe` 可在命令行使用）。

推荐先运行自动安装器。它会创建隔离的 `.venv`，逐项检测并只安装缺失依赖；FFmpeg 缺失时会尝试通过 `winget` 安装。默认安装 Whisper 运行库，但模型权重只在首次启用字幕识别时下载：

```powershell
cd suojian
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

演示机暂不需要 Whisper 时可加 `-SkipWhisper`。环境能力可随时自检：

```powershell
.\.venv\Scripts\python.exe .\scripts\selfcheck.py
```

手动安装方式：

```powershell
cd suojian
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python web.py
```

浏览器打开 `http://127.0.0.1:8788`，拖入 MP4 并开始解析。

在这个“本地工作台”中完成脚本预览并点击“生成剪映草稿”后，草稿会直接写入本机剪映的草稿列表。双击打开的 `preview.html` 是离线展示页：可看分析结果，但无法写入剪映，页面会明确提示而不再下载 JSON 伪装为草稿。

也可用命令行：

```powershell
.\.venv\Scripts\python run.py .\sample\demo.mp4 --output .\results\demo
```

需要生成可单独试听的分轨时：

```powershell
.\.venv\Scripts\python -m pip install "audio-separator[cpu]"
.\.venv\Scripts\python run.py .\sample\demo.mp4 --output .\results\demo --separate-audio
```

首次分轨会下载所选模型。可通过 `SUOJIAN_SEPARATOR_MODEL` 更换模型；默认使用 `UVR-MDX-NET-Inst_HQ_3.onnx`。

每次命令行分析也会生成零依赖的 `preview.html`，双击即可查看，因此即使 Web UI 的 Flask 依赖尚未安装，也能完成演示。

## 当前能识别什么

- FFprobe 视频元信息：时长、分辨率、fps、音视频编码。
- PySceneDetect 内容切点、镜头起止与时长；每个边界在 v0.1 默认标为 `hard_cut`。
- OpenCV 稀疏光流的**粗粒度** `static`、`pan_left/right`、`zoom_in/out` 运动判断。
- 若安装可选 `librosa`：基础 BPM 与 beat timestamps，以及切点的 `beat_sync`。
- 基于音频瞬态的音效候选：转场冲击、提示/点击、其他瞬态，并显示时间、切点距离和置信度。
- 可选人声/背景音乐分轨：使用 `audio-separator` 调用 Demucs/MDX 模型，工作台可从当前时间单独试听各轨；`other` 仅标作“其他/音效候选”。
- 中文语音识别默认使用 FunASR SenseVoice + VAD，可配置专有名词热词；Whisper 可手动切换，语音识别稿独立保存在 `speech_transcript[]`。
- 独立画面文字识别：优先 RapidOCR，缺少时回退本机 Tesseract；`onscreen_text[]` 保存画面里识别到的文字，`subtitles[]` 仅采用较像字幕的下半屏、重复出现文本（找不到时才回退语音稿）。语音与画面文字冲突单独记录为 `subtitle_conflicts[]`。
- 音频结构 Blueprint：输出语音可用性、语速/音色画像、BPM/音乐风格检索词，以及带时间和置信度的音效事件；下游负责匹配并重建素材，不追求还原原始音效文件。
- 基于抽帧和高饱和轮廓的贴纸候选：显示时间、归一化位置、尺寸和置信度。
- 结构化 `edit_blueprint.json`：`shots`、`timing`、`transition_out`、`motion`、`beat_sync`、`confidence` 等字段。
- 浏览器本地预览：视频、镜头时间线、镜头清单与 JSON 下载。
- 配音 TTS 候选：估算音高、文案语速，显示最接近的剪映音色类别、备选音色、倍速与置信度。
- 字幕模板标签：位置、字号、字体候选、描边/投影样式、花字候选会写入 Blueprint，并显示在字幕时间线上。
- 封面选帧：从原视频截取候选帧，分别说明推荐时间点和理由；封面标题单独给出。
- 简化版剪映多轨时间线：视频、字幕、运动、转场、节拍、音效、贴纸。
- 时间线元素悬停详情卡、固定属性检查器，以及点击元素后的视频定位联动。
- `styles` 与 `edit_slots`：字幕只引用 `style_id`，新脚本通过语义块与 Edit Slot 匹配。
- 新脚本向导：智能适配 / 严格复刻、原字幕 VS 新字幕、字数/朗读时长/Fit Score 警告。
- 播放区保留参考视频本身的字幕，不额外叠加演示字幕；生成方案在下方以可核对的字幕时间线展示。
- 字幕和画面文字识别结果是候选稿，不承诺零错字；工作台会把语音识别、画面 OCR 和冲突分开，避免把招牌/标题误当配音或字幕。

### 提升中文识别准确度（可选）

FunASR 与 RapidOCR 是可选组件，不会被基础安装器静默拉取大型模型。当前溯剪环境已安装 FunASR 并下载 SenseVoice 中文识别模型及 FSMN-VAD 权重。模型缓存在项目内的 `model_cache/`，不会写入用户全局缓存目录；首次下载完成后，重启或再次启动工作台会直接使用本地权重。

```powershell
.\.venv\Scripts\python.exe -m pip install "funasr>=1.4.9" "rapidocr>=3.0" "onnxruntime>=1.18"
```

工作台现在默认使用已缓存的 FunASR SenseVoice 中文模型；也可设置 `SUOJIAN_ASR_BACKEND=whisper` 切换 Whisper，或设置 `auto` 先试 FunASR、失败后回退 Whisper。专有名词可设置 `SUOJIAN_ASR_HOTWORDS`，多个词以空格分隔，例如园区名、品牌名、地名。可用 `SUOJIAN_FUNASR_MODEL` 更换 FunASR 模型，或用 `SUOJIAN_WHISPER_MODEL` 选择 Whisper 模型尺寸。更换模型后首次运行仍需联网下载对应权重。

## 两种 Blueprint

- `edit_blueprint.json`：只保存参考视频分析结果、样式、镜头和 Edit Slots。
- `draft_blueprint.json`：只保存用户的新作品，包括脚本模式、语义块、Slot 匹配、字幕内容与审批状态。

二者不会混写。分析 Blueprint 当前为 `0.9`（语音稿、画面文字和字幕候选分字段保存），生成版本为 `draft-0.1`。

## Cutroom 执行后端

溯剪将三类数据严格分开：`edit_blueprint.json` 是参考视频分析结果；`draft_blueprint.json` 是用户可修改的新视频方案；`cutroom_binding.json` 记录已创建的 Cutroom 任务。新包的 Agent MCP 已在本机探测通过。工作台可明确确认后创建 Cutroom 广告任务、查看任务状态，并在 Cutroom 完成人工确认和可编辑交付包后生成剪映草稿；不会自动跳过 Cutroom 的文案、画面、包装和审片门禁，也不会自动触发收费动作。

Cutroom MCP 是任务级接口，不是独立的 TTS／字幕／音效接口。其可编辑交付包会尽可能导入为剪映的**一个视频片段**及独立交付的音频、字幕轨；Cutroom 镜头切点、贴纸和花字目前不会还原成剪映原生可编辑对象。没有独立交付的效果仍属于烘焙画面，不能声称在剪映中逐项可编辑。旧“只生成参考素材基础草稿”按钮不调用 Cutroom。尚未拿到真实 Cutroom 发布包进行剪映实机验收，当前已用小型模拟交付包验证转换器。

## 草稿审批闸门

分析阶段只生成工作台和 Blueprint，不会自动写入剪映。用户明确许可后，草稿命令还必须显式携带 `--approved-by-user`；缺少该参数会立即退出：

```powershell
.\.venv\Scripts\python generate_jianying.py <blueprint.json> <video.mp4> <draft-root> --approved-by-user
```

使用已经确认的新脚本草稿：

```powershell
.\.venv\Scripts\python generate_jianying.py <edit_blueprint.json> <video.mp4> <draft-root> --draft-blueprint <draft_blueprint.json> --approved-by-user
```

状态流为 `analysis → preview → approved → generation → completed`。如果当前剪映版本或 `pyJianYingDraft` 不兼容，工作台会保留真实的 `draft_blueprint.json` 并标记 `generation_failed`，不会伪装成功。

音色结果是当前成片与剪映音色类别的近似匹配，不是对剪映内部 voice ID 的读取；剪映升级后应在当前音色库试听首选和备选。

## 有意不做的事

- 不声称能辨识所有剪映转场、滤镜、特效或“电影感”。`transition_out` 目前仅把检测到的切点标为 hard cut。
- 运动识别为全局光流启发式；人物移动、手持抖动、画面内物体运动会降低准确性。
- 音效只能从混音音轨中定位疑似瞬态并粗分类，不能恢复剪映音效素材名。
- 通用源分离模型不能稳定输出“纯音效”；Demucs 的 `other` 包含其他乐器与残留声音，因此工作台只将其称为“音效候选”。
- 音频模块的职责是“听懂结构”，不是母带级分离：人声服务 ASR/TTS，音乐服务节奏/风格，音效服务事件检测与重建。
- 贴纸检测是保守的视觉候选扫描；画面原生图形、字幕花字和贴纸可能互相混淆，必须人工复核。
- Whisper 包可由自动安装器准备，但模型权重不会预先下载；只有以 `--subtitles` 启用时才需要模型。
- 剪映 Builder 在分轨可用时生成静音视频主轨、人声轨、背景音乐轨和可编辑字幕轨；没有分轨时回退为原片声音轨。不声称精确复原字体资产 ID、花字 ID、复杂逐字动画、高级变速或素材替换。

## 分层架构

```text
analyzer/   ffprobe · PySceneDetect · OpenCV · librosa · Whisper
fusion/     把镜头、节拍、运动合并成剪辑动作
blueprint/  稳定的 edit_blueprint.json 中间格式
script_parser/  规则式语义拆分 · 角色分类 · Slot Matcher
jianying/   DraftBlueprint → Timeline / Subtitle / Style Builder
ui/         Flask API · 多轨工作台 · Inspector · 草稿向导 · Overlay
```

## 参考项目如何被借鉴

- **PySceneDetect**：采用其 ContentDetector，而不是维护切点算法。
- **TransNetV2 / deep-video-watcher**：后续可作为更强的镜头检测与流式任务层；当前保留替换口。
- **ai-video-agent**：借鉴“分析结果→结构化中间产物→下游动作”的分层思路。
- **pyJianYingDraft**：仅放在 `jianying` 适配层；不把其或任何第三方项目整仓 fork 进来。

## 接入「源流」

源流只需把下载或选取好的本地视频路径交给 `suo_jian.pipeline.analyze(video_path, output_dir)`。下游读取 `edit_blueprint.json` 后，可将 `shots` 中的节奏、运动和字幕时间戳映射回源素材的剪映时间线。推荐先以 `schema_version` 做契约，后续即使替换 TransNetV2 / Whisper 也不破坏调用方。
## Cutroom Agent MCP 接入（本机）

设置 `SUOJIAN_CUTROOM_ROOT` 为 Cutroom 安装目录；未设置时默认检查
`C:\Users\Tanikawa\CUTROOM`。溯剪的 `/api/cutroom/capabilities` 会通过
Cutroom 自己的 Python 环境读取 MCP 服务状态；`/api/cutroom/preflight/<job>`
只做广告任务创建前检查，需要已确认的 DraftBlueprint 和 Cutroom 的
`primary_library_id`。`/api/cutroom/create/<job>` 需要用户明确确认才创建任务；
`/api/cutroom/task/<job>` 读取状态；`/api/cutroom/import/<job>` 只在已有
Cutroom 可编辑发布包且用户确认预览后写入剪映草稿。这些接口不直接启动收费模型。

新包的 MCP 是任务级接口（12 个工具），不是单独的 TTS／字幕／音效接口。
本机已登记嘉兴园区素材库 `suojian-jiaxing-park`（123 个视频），默认用于
新 Cutroom 广告任务；可通过 `SUOJIAN_CUTROOM_LIBRARY_ID` 更换。正式执行
仍需在 Cutroom 完成脚本锁定、素材同步、费用许可和各阶段人工确认。
Cutroom 目前不通过 MCP 输出剪映原生草稿；草稿转换由溯剪完成。
不要把 API 密钥或 MCP 访问令牌写入 DraftBlueprint 或网页前端。
