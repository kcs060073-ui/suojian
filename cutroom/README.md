# 视频自动化剪辑 · 数字人混剪 + 信息流广告

从「口播 MP4 + 文案」自动产出带样式字幕、B-roll、标题卡的 **9:16 / 16:9 短视频**。两条主流水线，一条龙脚本，目标日产 10 条、多账号铺量。

普通同事请先阅读：[剪辑工作台同事操作手册](./docs/user-guide/cutroom-user-guide.md)。工作台右上角也可直接打开网页版“使用手册”；旧的[视频生产目标流程手册](./docs/user-guide/video-production-user-guide.md)仅保留设计追溯。

首次真实试运行请使用：[普通同事首条真实任务验收单](./docs/validation/cutroom-colleague-first-real-task-acceptance.md)。验收必须由普通同事本人操作，自动测试不能代替人工签收。

技术栈：**FFmpeg 原生渲染 + faster-whisper 语音识别 + ts-node 脚本 + 大模型（火山方舟）规划/生成**。

## Windows Beta 安装

剪辑工作台 v0.9.0-beta.8 采用在线安装 ZIP，仅面向获得私有 GitHub 仓库访问权限的内部同事：

当前开发目录包含尚未打包发布的 QA 修正：可将锁定镜头中的团队自摄素材按文件指纹登记并在后续任务复用；静态照片不再误报为画面卡顿；非阻断警告不再暗中挡住审片；信息流成片音频输出固定为 48 kHz。已发布的 beta.8 ZIP 不包含这些后续改动。

1. 从 [剪辑工作台 v0.9.0-beta.8 下载页](https://github.com/qnwhageqlavs-jpg/cutroom/releases/tag/v0.9.0-beta.8) 下载 `剪辑工作台-v0.9.0-beta.8-windows-x64.zip`，并完整解压到自己可写的短路径，例如 `D:\CUTROOM-v0.9.0-beta.8`；不要直接在压缩包内运行，也不要放进层级很深的目录。
2. 首次双击 `安装CUTROOM.bat`。安装器会检查并安装 Python 3.11、Node.js LTS、FFmpeg，再建立项目专用环境；首次安装文件较多，看到“安装完成”前不要关闭窗口。
3. 安装完成后双击 `启动CUTROOM.bat`，浏览器打开 <http://127.0.0.1:8765/>。
4. 在剪辑工作台“设置 → AI 服务连接”中配置供应商与模型；豆包 API Key用于合成、试听和连接检测，AK/SK用于自动刷新音色目录，AppID + Access Token是旧版兼容项，可以留空。保存并检测会生成极短的测试语音。每台电脑需由火山账号/IAM 管理员录入一次；不要修改 `.env`，密钥由 Windows 凭据管理器保存。
5. 如不想把个人包装素材放在安装目录，打开“设置 → 存储与缓存”选择自己的本地文件夹。系统会自动建立八类子目录；切换路径不会搬迁或删除旧文件。

Beta 安装需要 Windows 64 位、联网和可用的 Windows 程序包管理器 `winget`。普通同事电脑的独立安装验收仍是发布后的现场门禁，不把本机验证等同于全环境兼容。

> 渲染引擎曾用 Remotion，因本机 360 内核驱动 `sysdiag.sys` 拦截文件 I/O 导致 ffprobe 读取失败（moov atom not found），改用 FFmpeg 直接读源文件绕过。**勿退回 Remotion，除非禁用该驱动。**

---

## 能做什么

- **数字人混剪**：1.25 倍速 + 思源粗宋橘黄字幕 + 重点色 + 金句整屏 + 开场标题卡 + 全屏 B-roll + 画中画 + 水印 + BGM，**一次 filter_complex 烧成片**，B-roll 时间自动对齐口播。
- **信息流广告**：文案 → AI 分镜 → TTS 配音 → 逐句配片 → xfade 转场拼接出片。
- **B-roll 自动来源**：Seedance 文生视频（贴合度最高，推荐）/ Pexels 库存检索 / 本地素材库。

---

## P0 素材目录当前状态

已用 10.324GB、23,086 个真实媒体文件完成 L1/L2、中途终止续跑、离线重连和备份恢复技术验证；WorkBuddy 端到端实操亦已通过。正式使用范围为本机硬盘和移动硬盘；NAS 仅保留底层兼容，不属于发布门禁。P0 仍待普通同事电脑、实际移动硬盘和真实长路径/权限环境的现场复核，验证记录见 [P0 素材目录验收报告](./docs/validation/p0-media-catalog-validation.md)。

## P1 预览与选材（素材数据底座的上层）

在 P0 素材目录（SQLite 主账本）之上，提供「预览 + 人工选材」能力：自动生成缩略图 / 拼 contact sheet / 代理视频 / 音频预览，跑 VAD 粗转写与响度异常，把长视频切成带原片时间码的候选镜头，标出疑似隐私点（仅标记不打码），并维护任务目录（素材检查表）与团队包装清单。所有改动都是**只读原片 + 写数据库**，不碰成片渲染。

P1 核心软件能力与本机技术验证已完成，但不是“14 个模块全部端到端通过”：模块 14 样片、普通同事使用和真实操作时长尚未完成。普通同事不接触命令、SQLite、素材 ID 或 ffprobe 路径，也不得把本节命令当成已发布独立工具的操作说明。

完整验收见 [P1 预览与选材验收报告](./docs/validation/p1-media-catalog-acceptance.md)。常用命令（先 `npm run` 见 `package.json`，或直接 `python scripts/media_catalog.py --db <库> <子命令>`）：

```powershell
# 全库默认生成 3 类：thumbnail / contact_sheet / audio_preview（不自动生成 proxy）
python scripts/media_catalog.py --db <库> preview --library <库名> --workers 4

# proxy 按需生成：可指定单素材，或对全库显式传 --kinds proxy
python scripts/media_catalog.py --db <库> preview --asset <素材ID> --kinds proxy
python scripts/media_catalog.py --db <库> preview --library <库名> --kinds proxy --workers 4

# 音频分析：VAD 人声区间 + 粗转写 + 响度异常 + 联系方式扫描
python scripts/media_catalog.py --db <库> analyze-audio --library <库名> --model small

# 场景切分：长视频切成带原片时间码的候选镜头（无转场则兜底整片）
python scripts/media_catalog.py --db <库> segments --library <库名> --workers 4

# 人工选材：给素材打 candidate / rejected / confirmed
python scripts/media_catalog.py --db <库> select --asset <资产ID> --decision candidate --by <你>

# 任务目录（素材检查表）+ 团队包装清单
python scripts/media_catalog.py --db <库> tasks create --id t1 --title "某选题" --type zhaoshang
python scripts/media_catalog.py --db <库> packaging register --asset <资产ID> --kind watermark --name "水印"

# 隐私标记：仅扫描标记，等你在选材环节拍板遮不遮
python scripts/media_catalog.py --db <库> privacy scan --library <库名>
python scripts/media_catalog.py --db <库> privacy list --unconfirmed

# 缓存安全清理（先 dry-run 看将释放多少，再真删）
python scripts/media_catalog.py --db <库> cache status
python scripts/media_catalog.py --db <库> cache clean --dry-run
```

> 隐私红线：自动标出手机号 / 微信 / 邮箱（来自音频转写）与可选的人脸 / 二维码，**绝不自动打码**，统一在候选确认环节由人决定是否遮。屏幕、工牌暂不做自动识别。

## P2 剪辑组装与交付（门禁链）

在 P1 选材之上，把「场景卡片 → 画面锁定 → 包装方案 → 自动质检 → 人工完整审片 → 发布合规」串成可追溯的门禁链。四道人工门禁：文案锁定 → 画面顺序锁定(picture-lock) → 包装确认(packaging_plan) → 完整观看(reviewed)。验收要求：项目 + 数字人各 1 条真实端到端样片、BGM 不压人声、门禁全程可追溯、qa 0 阻断。

当前状态：**数字人路线已端到端验收**——v2 样片（新疆算力口播）已通过四道人工门禁（文案/画面锁/包装/人工完整审片）、自动 QA 0 阻断，并生成合规发布包（抖音，`deliverables/publish/p2-xinjiang-v2/`）。v1 的三个返工问题（字幕漂移、数字误识别、样式未生效）已在 v2 修复并核验。完整结论见 [P2 数字人 v2 样片验收报告](./docs/validation/2026-08-24-xinjiang-v2-acceptance.md)。**项目样片路线仍未验收**，P2 的"项目 + 数字人各 1 条"验收要求尚差项目一条。

常用命令（库默认 `runtime/media-catalog/catalog.sqlite`）：

```bash
# 场景卡片 + 画面锁定门禁
python scripts/media_catalog.py --db <库> scene-card add --task t1 --idx 1 --asset <资产ID> --mix broll全屏 --audio mute
python scripts/media_catalog.py --db <库> picture-lock set --task t1

# 包装资产库 + 方案推荐（manifest 驱动，推 2–3 套）
python scripts/media_catalog.py --db <库> assets catalog          # 离线 HTML 试听/预览
python scripts/media_catalog.py --db <库> assets validate         # 校验所有 manifest
python scripts/media_catalog.py --db <库> assets recommend --task t1 --route 数字人混剪
python scripts/media_catalog.py --db <库> assets confirm --plan <plan_id>   # 人工确认门禁

# 自动质检（黑场/冻结/静音/削波 + 工程一致性）
python scripts/media_catalog.py --db <库> qa run --task t1 --video out/<成片>.mp4
python scripts/media_catalog.py --db <库> qa reports --task t1

# 数字人真实编排第一段：素材 ID → render-mix → 依赖快照 → QA → 等待人工审片
python scripts/media_catalog.py --db <库> assemble --task t1 --render --script public/scripts/t1.script.txt --words public/scripts/t1.words.json --semantics public/scripts/t1.semantics.json --output out/t1.mp4

# 从第一段 JSON 结果读取 qa_snapshot_id 与 report_id；完整观看同一成片后绑定审批
python scripts/media_catalog.py --db <库> review approve --task t1 --snapshot <qa_snapshot_id> --qa <report_id> --by Peter --notes "完整观看通过"

# 仅匹配上述受信候选的审批才能生成发布包
python scripts/media_catalog.py --db <库> assemble --task t1 --render --script public/scripts/t1.script.txt --words public/scripts/t1.words.json --semantics public/scripts/t1.semantics.json --output out/t1.mp4 --platform 视频号 --title "..." --desc "..." --tags "招商"

# 旧 --video 只导入外部已有视频做 QA，不代表素材 ID 到成片的端到端通过
python scripts/media_catalog.py --db <库> assemble --task t1 --video out/<外部已有成片>.mp4
```

`snapshot` 仅提供 `list`，发布记录仅提供 `publish list`；渲染快照与发布包都只能由 `assemble` 可信闭环生成，避免手工伪造候选或绕过审片。

已确认包装方案含字幕 preset 时，`--script`、`--words` 与 `--semantics` 必须成套提供；semantics JSON 只能包含字符串数组 `keywords` 与 `punchLines`。assemble 会把三份来源共同签名并按已确认 preset 生成不可变任务 ASS；如改用 `--ass`，必须同时存在同名 `.metadata.json` sidecar，且 preset、三份来源签名和 ASS 哈希均匹配。

> 资产清单在 `assets/manifests/*.json`：BGM 3 首 + 火山 TTS 音色（env 引用，不落明文密钥）+ **字幕模板 6 套 + 花字 6 套 + 音效 8 个（ffmpeg 合成，CC0 级）+ 贴纸 12 个（PIL 透明 PNG）** 均已填真实数据；`assets recommend` 自动纳入方案（B/C 方案含贴纸+花字，全方案含 sfx）。

## 剪辑工作台同事视频工作台

面向普通同事的正式入口已经从旧“一键出片页”升级为本地工作台：任务看板/列表、数字人创建向导、主素材与通用补充目录、十二步主链、四个人工门禁、镜头级调整、本片包装组合、后台恢复、受控成片预览和七文件发布包均读取同一 SQLite 账本。

固定入口：**http://127.0.0.1:8765/**。首次安装登录自启、页面打不开时的恢复方法和完整同事操作流程见 [剪辑工作台使用说明](./web-ui/README.md)。

AI 服务收敛为“文案策划、B-roll、配音”三类：文案策划统一复用到文案分析、分镜与包装推荐，本地素材匹配无需模型。豆包配音会使用 AK/SK 读取官方音色目录，设置页选择常用音色，信息流广告再单独选择“本条视频音色”；普通同事不填写模型 ID 或音色 ID。连接检测、24 小时有效期和任务快照共同阻止静默回退；任务创建后冻结精确声音，后来刷新目录或修改常用项不会影响旧任务。密钥只在当前 Windows 用户的凭据管理器中，GitHub、ZIP 和 SQLite 不会把密钥带到同事电脑。具体配置见 [多模型与供应商配置](./docs/operations/model-profiles-new-api.md)。

剪辑工作台当前开放“数字人混剪”和“信息流广告混剪”，项目宣传片仍显示为后续开放。数字人路线复用 `faster-whisper → AI 分镜 → 本地素材匹配 → render-mix`；广告路线使用 `锁定广告文案 → AI 逐句分镜 → TTS → 已绑定素材库逐句配片 → render-ad`。两条路线共用素材同步、人工画面锁、包装确认、QA、完整审片和发布包门禁；广告每句没有真实素材时禁止锁画面，付费视频生成仍不自动调用。

### 2026-09-22 双路线设计收口

数字人混剪和信息流广告混剪的本轮**软件实现与自动化验证**已按统一设计收口：任务搜索与完整预检、任务范围素材索引、AI 推荐拒绝/纠正/重荐、后台安全暂停与 ETA、字幕/响度/素材指纹/来源/授权 QA、候选或外部或待补封面、发布标签，以及标准/可编辑交付均接入同一十二步工作流。两条路线共用一份发布级 QA 完整性契约，发布包只能绑定当前、QA 通过且已完整审片的候选；可编辑组成项缺失时记录 `not_available`，不生成伪文件。

本轮没有完成真人可用性测试、普通同事现场验收、真实现场素材验收，也没有开放项目宣传路线；因此不能据此宣称整个剪辑工作台已按最初设计全部验收。自动化范围、命令与剩余边界见[双路线设计收口验证报告](./docs/validation/2026-09-22-two-route-design-closure.md)。

### Agent 模式（Codex / WorkBuddy / 豆包工作，可选 MCP）

- 独立模式：剪辑工作台自己调用已配置的文案/分镜模型。
- Agent 模式：当前客户端模型完成文案与分镜；剪辑工作台不调用文本模型。
- 两种模式：豆包 TTS、Seedance 等生产服务仍使用剪辑工作台各自凭据。
- 四个人工门禁仍只能在剪辑工作台页面确认，Agent 不能代替人工放行。

Codex、WorkBuddy 和豆包工作共用同一个 MCP Server 和同一组 12 个工具。Codex、WorkBuddy 使用本地 `stdio`；豆包工作的公开自定义连接器只接受 HTTP，因此使用只监听 `127.0.0.1` 的 Streamable HTTP。来源身份分别由 `--client codex`、`--client workbuddy`、`--client doubao_work` 固定注入。普通安装不会自动注册任何客户端。完整安装、卸载和验收步骤见 [Agent MCP 客户端操作手册](./docs/operations/cutroom-agent-mcp-clients.md)。

WorkBuddy 写入配置后还需在连接器管理页信任并启用 `cutroom`，再重启客户端或新建会话；豆包工作必须先启动本机 HTTP 服务，再显示并填写连接字段。`cutroom_get_system_status` 中 `mcpToolCount = 12` 表示完整 MCP 工具面，`allowedTaskActionCount = 4` 只表示一个动作工具允许启动的四种任务动作。

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-codex-mcp.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-workbuddy-mcp.ps1 -DryRun
# 豆包窗口 1：启动后保持开启
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-doubao-work-mcp.ps1
# 豆包窗口 2：读取要填入连接器的字段
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\show-doubao-work-mcp-config.ps1
```

在已打开并登录的工作台会话中，Agent 返回的任务地址形如 `http://127.0.0.1:8765/#/tasks/<task-id>`；不要把任何启动密钥拼进地址。关闭某个客户端的 MCP 不会删除剪辑工作台和任务。WorkBuddy 安装器只修改 `mcpServers.cutroom`；豆包工作只通过公开的自定义连接器入口配置，不修改内部数据库。

## P3 智能深化（规则版本化 / 重复检测 / 语义匹配 / 脚本适配器）

在 P2 门禁链之上叠加「可从真实反馈持续改进」的智能层：规则可版本化（新任务默认最新、旧任务锁定创建时版本）、镜头级近重复检测、脚本↔候选镜头匹配、无文案时的脚本草稿适配器。全部纯 Python（PIL+numpy+SQLite），不依赖 ffmpeg / 外部模型，可独立验证。

P3 四个模块已完整实现并通过验证：\	ests/test_p3_{rules,dedup,match,script_adapter}.py\ 共 30 个测试全绿（全量 235 测试 OK）；CLI 实机验证 M1 rules（v1→v2 递增、旧任务锁 v1、新任务默认 v2）、M2 dedup scan（5 视频 60 帧 4 组近重复落库）、M3 match shots（脚本↔候选镜头排序合理）、M4 script draft→confirm（草稿生效）全部通过；主库迁移已补到 010（task_gate_events + review_approvals）。真实运行环境为 untime/media-catalog/p3-verification.sqlite\（复制自验收库，避免污染 P2 数据）。

```bash
# 规则版本化：新任务默认最新规则，旧任务锁定创建时版本（qa 自动读取锁定版本阈值）
python scripts/media_catalog.py --db <库> rules list                       # 列出全部版本
python scripts/media_catalog.py --db <库> rules current                    # 当前最新版本号
python scripts/media_catalog.py --db <库> rules show --version 2           # 查看某版本 payload
python scripts/media_catalog.py --db <库> rules bump --notes "加严黑场" --patch '{"qa":{"black":{"block_above":0.5}}}'

# 镜头级近重复检测（感知哈希，纯 Python；抽帧依赖 ffmpeg）
python scripts/media_catalog.py --db <库> dedup scan --videos v1.mp4,v2.mp4 --task t1 --scope "t1 候选镜头"
python scripts/media_catalog.py --db <库> dedup list --task t1

# 脚本 ↔ 候选镜头语义匹配（词法/二元组 + IDF 打分，阈值/权重可迭代）
python scripts/media_catalog.py --db <库> match shots --task t1 --script "项目 厂房 招商 政策"
python scripts/media_catalog.py --db <库> match list --script "项目 厂房" --items "a.mp4,b.mp4"

# 脚本写作适配器（无文案入口草稿 + 人工确认回退门禁）
python scripts/media_catalog.py --db <库> script draft --task t1 --route 项目宣传
python scripts/media_catalog.py --db <库> script confirm --draft <draft_id>
```

> 设计要点：规则版本化保证「新任务吃最新阈值、历史任务不随规则改动返工」；`qa run` 透传任务锁定的规则版本阈值。重复检测抽帧需 ffmpeg（环境二进制待恢复），哈希/分组算法纯 Python 已验证。语义匹配当前为词法/二元组+IDF，真·embedding 列为待升级点。脚本适配器保持人工回退：草稿 `confirm` 才生效，可随时 `reject` 改手写。

## 安装

```powershell
# 1. 依赖（跑前先清空死代理：$env:HTTP_PROXY=""; $env:HTTPS_PROXY=""）
npm install

# 2. 语音识别环境（faster-whisper，装在项目 .venv，无需 torch）
python -m venv .venv
.venv\Scripts\pip install faster-whisper

# 3. FFmpeg / ffprobe 需在 PATH（探时长 + 渲染）

# 4. 配置 key
copy .env.example .env   # 然后编辑 .env 填入火山方舟 / Pexels key
```

字体（思源粗宋等）放 `assets/fonts/`，已 gitignore，需单独同步。

---

## 配置 key（.env）

复制 `.env.example` 为 `.env` 填入。关键变量：

| 变量 | 用途 | 备注 |
|------|------|------|
| `ARK_API_KEY` / `ARK_MODEL` / `ARK_BASE_URL` | plan-brief 文案规划 | coding plan 用 `deepseek-v4-pro` + `/api/coding/v3` |
| `SEEDANCE_API_KEY` / `SEEDANCE_MODEL` | 文生视频 B-roll | 同一把 Ark key；走标准 `/api/v3`，需开通 Seedance |
| `PEXELS_API_KEY` | 库存检索 B-roll（可选） | pexels.com/api 免费 |

---

## 流水线 A：数字人混剪（主线，全自动）

```
口播 MP4 + 文案
  ① transcribe.py    faster-whisper → <name>.srt + <name>.words.json（词级时间戳）
  ② plan-brief       文案 → <name>.brief.json（重点词/金句/标题/B-roll提示词）+ plan.json   ← AI
  ③ compile-plan     brief + 词级时间戳 + 本地库 → <name>.plan.json（锚点定位+素材匹配）
  ④ gen-broll        plan 待补的 B-roll → Seedance 文生视频 → 本地库（或 fetch-broll 走 Pexels）
     ↳ 重跑 compile-plan，B-roll 并入 plan
  ⑤ make-ass         文案 + 词级时间戳 + brief → <name>.ass（带样式字幕）
  ⑥ render-mix       倍速 + 字幕 + B-roll + 画中画 + 标题卡 + 水印 + BGM 一次烧成 → out/<name>.mp4
```

```powershell
.venv\Scripts\python scripts/transcribe.py public/digital_human/<name>.mp4 small
# 文案存到 public/scripts/<name>.script.txt
npm run plan        <name>            # ② 出 brief（招商默认；财税加参数 caishui）
npm run compile     <name>            # ③ 定位+匹配，缺素材列入 pending
npm run gen-broll   <name>            # ④ Seedance 生成待补 B-roll（或 npm run fetch-broll <name>）
npm run compile     <name>            #    重跑，B-roll 并入
npx ts-node scripts/make-ass.ts <name> 1.25    # ⑤
npm run mix         <name>            # ⑥ 出片
```

> **双业务线**：`npm run plan <name> [speed] [profile]`，profile 取 `zhaoshang`（默认·招商）或 `caishui`（财税）。详见 CLAUDE.md。

### 受控 Seedance 批次（付费生成）

`npm run gen-broll <name>` 保留为旧任务兼容入口。需要可审计的付费生成时，使用显式模式，并在真实调用前人工确认三条请求：

旧入口会在提交前检查整个批次的文件名与查询冲突；匹配且通过媒体检查的缓存直接复用。提交意图、供应商任务 ID 和结果保存在 `public/library/seedance-jobs.json`。存在未完成或结果不明的旧任务时会停止，维护人员需先核对供应商任务和本地结果；不要直接删除任务记录再次付费。任一条失败时命令返回非零状态。

```powershell
npx ts-node scripts/generate-broll.ts --requests <三条请求.json> --run-id <唯一批次ID> --output-dir <专用输出目录> --manifest <provenance.json>
```

显式模式只接受 3 条 key 唯一的请求；每条只创建一次任务，强制要求 `9:16 / 1080p / 5s / watermark false`，输出名为 `seed_<run-id>_<01..03>_<slug>.mp4`，并写入带 SHA-256、模型、任务 ID 的 schema v1 provenance。首个任务创建前会独占预留 manifest；每成功一条即原子写入该条完整 provenance，因此后续失败仍可审计已付费成功项。它不会更新 `public/library/manifest.json`，失败即停止，不能用旧素材替代。

### 流水线 A2：项目样片（真实项目素材，复用 render-mix 链路）

项目 route 无独立渲染适配，复用已验证的 render-mix 链路：横版项目空镜 center-crop 成 9:16 竖屏素体并 mux TTS 配音（render-mix 强制素体 9:16），B-roll 全屏覆盖由 scene_cards 按旁白时间驱动。

`
项目空镜(横版) + 文案
  ① tts             adplan 逐句合成配音 → voice.mp3 + adtiming.json（句级精确时长）
  ② ffmpeg          横版素材 center-crop 1080x1920 + mux 配音 → 竖屏素体 public/digital_human/<name>-v.mp4
  ③ make-ass        script.txt + words.json(用 adtiming 构造逐字时间) + semantics.json → ass（coverage=1.0）
  ④ media_catalog   建任务 → 挂素材(digital_human+ broll) → scene_cards(时间=adtiming/1.25) → picture_lock → packaging → assemble --render → QA → review approve → 发布包
`

要点：Whisper 对 TTS 中文识别错字多（对齐覆盖率 <0.8），改用 dtiming 句级时间 + 文案原文字均分构造 words.json（make-ass coverage=1.0）。项目样片实拍：park-zhouwangmiao（周王庙真实空镜 11 段 + 11 句招商文案），发布包 deliverables/publish/park-zhouwangmiao/。

---
---

## 流水线 B：信息流广告

```
广告文案
  ① plan-ad      文案 → <name>.adplan.json（逐句分镜：text/query/prompt/type）   ← AI
  ② tts          adplan 逐句合成配音 → <name>.voice.mp3 + <name>.adtiming.json（句级时长）
  ③ compile-ad   adplan + adtiming + 本地库 → <name>.adrender.json（逐句配片，缺的列 pending）
     ↳ gen-broll / fetch-broll 补素材后重跑 compile-ad
  ④ render-ad    逐段 scale/裁切/循环铺满 + xfade 转场 + 烧字幕 + 配音(+BGM) → out/<name>.mp4
```

```powershell
npm run plan-ad    <name>
npm run tts        <name>
npm run compile-ad <name>
npm run gen-broll  <name>      # 补待补素材后重跑 compile-ad
npm run render-ad  <name> [bgm路径]
```

> render-ad 已做健壮性兜底：**短素材自动循环铺满**（不冻结）、**图片 `-loop 1` 无限帧**、**缺素材用品牌色占位屏不阻断整片**。

以上兜底适用于旧命令行计划。剪辑工作台工作台生成的广告计划使用人工锁定的素材起止位置和已确认字幕预设；源窗无效会停止渲染，转场补帧只延长选段最后一帧，不取选段以外内容。工作台仍要求每句有真实素材才能锁定画面。

---

## 流水线 C：产品宣传片（截图/录屏类）

```
旁白 SRT + PC 截图 + APP 竖屏录屏
  ① promo-tts   读 SRT 逐句配音，每句贴合到字幕时间窗（短补静音/长轻微加速）→ <name>.voice.mp3   ← AI 配音
  ② 编 storyboard.json  主画面段（PC 截图，Ken Burns 推拉）+ PiP 段（APP 小窗，右侧）+ 高亮卖点
  ③ render-promo  主画面 zoompan + xfade 转场 + 右侧 APP 画中画 + 烧 SRT 字幕(蓝色高亮) + 配音 + BGM → out/<name>.mp4
```

```powershell
npm run promo-tts  <name> <srt路径>          # ① 锁定 SRT 时间轴配音（正好卡 60s）
# 编 public/scripts/<name>.storyboard.json   # ② 见下方契约，按 SRT 切点排主画面/画中画
npm run promo      <name> [bgm路径]          # ③ 出片
```

**与另外两条线的区别**：A 数字人是「整条口播素体倍速混剪」，B 信息流是「文案→AI 配片+TTS 驱动时长」；**C 是「已有 SRT/素材，锁定时间轴拼装」**——PC 静态截图做 Ken Burns、APP 竖屏做右侧画中画，配音用 AI 但**贴合 SRT 不夺主导**。脚本 `promo-tts.ts` / `render-promo.ts` 完全独立，不碰 mix/ad。

**storyboard.json 契约**（放 `public/scripts/<name>.storyboard.json`，src 支持绝对路径）：

| 字段 | 说明 |
|------|------|
| `ratio` | `16:9`（横版 1920×1080）/ `9:16`（竖版） |
| `main[]` | 主画面段 `{start,end,src,motion}`，motion=`zoom-in`/`zoom-out`/`none`；图走 Ken Burns，视频走铺满裁切 |
| `pip[]` | 画中画段 `{start,end,src}`，竖屏 APP/电话录屏；短素材自动循环铺满 |
| `pip_box` | `{w_pct,x_pct,y_pct}` 小窗宽与左上角（画布占比），默认右侧 0.18/0.79/0.30 |
| `subtitle` | SRT 路径，烧白色粗体字幕 |
| `highlights[]` | 需蓝色高亮的卖点短语（如「科学选址」「实名认证」） |
| `voice` / `bgm` | 配音/BGM 路径（默认 `ad_audio/<name>.voice.mp3`；bgm 命令行参数优先） |

> 时间轴锁定：promo-tts 把每句配音贴合到 SRT 时间窗、拼成与 SRT 等长的整轨，故 storyboard 的所有 start/end 直接照 SRT 排，配音/字幕/分镜天然对齐。
> 首个实例：`选哪儿60s`（招商平台 60s 横版，PC 主 + APP 画中画 + 电话脱敏），分镜见 `public/scripts/选哪儿60s.storyboard.json`。

---

## B-roll 素材源（混合）

| 源 | 脚本 | 特点 |
|----|------|------|
| **Seedance 文生视频** | `gen-broll` | 按 prompt 生成定制竖屏空镜，**贴合度最高**，中文专有题材首选；按次计费、每条 1-3min |
| **Pexels 检索** | `fetch-broll` | 搜现成库存，免费；中文专有词命中弱 |
| **本地素材库** | 直接放 `public/library/` | 文件名或 `manifest.json`（`{"query":"文件名"}`）当标签，compile 按 token 重合度匹配 |

- B-roll 质量主要由 brief 的 **`prompt`**（喂 Seedance 的电影级画面描述）决定，写法规则在 `plan-brief.ts` 的 SYS 提示词。
- **品牌全局配置** `public/brand.json`（`{watermark, bgm}`）：compile 自动注入每条片的水印/BGM。

---

## 剪辑规范（已落实）

| 规范 | 实现位置 |
|------|----------|
| 拆短句·单行不换行·绝不切数字/关键词 | make-ass.ts |
| 思源粗宋 93号 / 金句 100号 | make-ass.ts `FONT_SIZE`/`PUNCH_SIZE` |
| 普通白·重点橘黄 #FD9437（数字+量词/地点/项目名） | make-ass.ts 数字正则 + brief 关键词 |
| 金句整屏 / 标题卡 / 1.25 倍速 / 字幕距底 560px / 提前 0.08s | make-ass.ts + render-mix.ts |
| 水印 / BGM / 全屏 B-roll / 画中画 | render-mix.ts（plan.json 驱动） |

样式参数集中在 `scripts/make-ass.ts` 顶部，随时调。

---

## 命令速查

| 命令 | 作用 |
|------|------|
| `python scripts/transcribe.py <视频> [模型]` | 识别字幕 + 词级时间戳 |
| `npm run plan <name> [speed] [profile]` | 文案 → brief（AI） |
| `npm run compile <name>` | brief → plan.json（定位+匹配素材） |
| `npm run gen-broll <name>` | Seedance 生成待补 B-roll |
| `npm run fetch-broll <name>` | Pexels 检索待补 B-roll |
| `npx ts-node scripts/make-ass.ts <name> [speed]` | 文案 → 带样式 ASS |
| `npm run mix <name> [speed]` | 一次烧成片（数字人混剪主出口） |
| `npm run plan-ad / tts / compile-ad / render-ad <name>` | 信息流广告四步 |
| `npm run promo-tts <name> <srt路径>` | 产品宣传·锁定 SRT 时间轴配音 |
| `npm run promo <name> [bgm路径]` | 产品宣传·主画面+画中画+字幕一次出片 |
| `npm run build-storyboard` / `npm run batch` | （旧·插入素材支线，已被 mix 取代） |

---

## 目录结构

```
视频自动化剪辑/
├── scripts/
│   ├── transcribe.py        ① Whisper 识别（词级时间戳）
│   ├── plan-brief.ts        ② 文案 → brief（火山方舟 LLM，profile 切招商/财税）
│   ├── compile-plan.ts      ③ brief → plan.json（src/timeline.ts 定位 + 本地库匹配）
│   ├── generate-broll.ts    ④ Seedance 文生视频补 B-roll
│   ├── fetch-broll.ts       ④ Pexels 检索补 B-roll（备选）
│   ├── make-ass.ts          ⑤ 文案 → 带样式 ASS（参数在顶部）
│   ├── render-mix.ts        ⑥ 统一引擎：倍速+字幕+B-roll+画中画+标题卡+水印+BGM
│   ├── plan-ad/tts/compile-ad/render-ad.ts   信息流广告四步
│   ├── ad-style.ts          广告字幕样式
│   ├── load-env.ts          零依赖 .env 加载器
│   └── build-storyboard/ffmpeg-render/render-styled.ts   旧管线（已被 mix 取代）
├── src/{types.ts, timeline.ts}    数据契约 + 文案→时间轴映射
├── public/{digital_human,scripts,inserts,library,ad_audio}/   素材（gitignore）
├── assets/fonts/            字体（gitignore，需单独同步）
├── out/                     成片（gitignore）
├── .env.example             复制为 .env 填 key
├── CLAUDE.md                项目手册（Claude Code）
└── AGENTS.md                项目手册（通用代理）
```

---

## 坑 / 环境备注

- 系统 `HTTP_PROXY/HTTPS_PROXY` 指向死代理 → **npm/pip 安装、调 API 前先清空**（脚本内已自动清；渲染走 FFmpeg 不联网）。
- 360 `sysdiag.sys` 内核驱动拦截未签名进程临时文件 I/O → FFmpeg/Node 直接读源文件不受影响；**勿退回 Remotion**。
- 字幕路径转义脆：渲染脚本已把 ass/字体拷到系统临时 ASCII 目录、cwd 设此、相对名引用，勿改这套规避逻辑。
- 数字人底层须是 **9:16 竖屏**（render-mix 会拦横屏源，避免中心裁切切脸）。
- 数字人素材是「已合成背景 MP4」，不抠像；换绿幕需在底层加 `colorkey`。
- Seedance/真实视频 6+ 条一次合成较重（渲染数分钟），资源争用偶发 `exit 255`（非文件损坏，重跑即成），渲染建议走后台。
