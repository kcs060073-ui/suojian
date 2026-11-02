# 同事视频工作台 Phase 1 实施计划

> **For Codex（2026-09-07 执行方式更新）：** 后续实现、复核和修正均由当前主代理直接完成，不再派发子代理。此前已产生的提交保留，但继续纳入主代理回归验证；任务范围、TDD、V4 一致性、真实出片门槛以及 Checkpoint A/B 均不改变。若实现移动到另一会话，仍按 `superpowers:executing-plans` 续接并停在两个检查点。

**目标：** 不建设“剪映替代品”，把现有素材目录、数字人混剪、四道门禁、自动 QA 和发布包做成普通同事无需命令行即可使用的本地工作台；第一阶段用 `p2-xinjiang-v2` 完成真实纵切。

**架构：** 浏览器只负责交互；Python 本地服务负责 API、SQLite 状态机、原生文件/目录选择和单重任务队列；现有 Python、TypeScript、Whisper、FFmpeg 通过领域适配层调用。前端不能执行渲染命令，也不能把内存状态当任务真相。

**技术栈：** Python 3、`unittest`、`http.server`、SQLite、原生 HTML/CSS/ES Modules、`node:test`，以及项目现有 ts-node/faster-whisper/FFmpeg/media_catalog。

**设计依据：** `docs/superpowers/specs/2026-09-05-colleague-video-workbench-design.md`

---


<!-- PROTOTYPE-FIDELITY:START -->
## 原型实现合同（2026-09-06 补充，适用于全部 Task）

统一规范：[原型到开发验收规范](E:/AI-memory-Hub/wiki/20_SOP/产品开发/原型到开发验收规范.md)。本节补齐已确认 V4 的承接要求，不重做产品设计。

- 原型基准：CUTROOM V4 恢复版；Peter 已在原任务确认 V4 主路径和产品边界，正式规格包含其粗剪操作。
- 只读源：`E:\我的AI项目\视频自动化剪辑\.superpowers\brainstorm\100-1788573373\content\cutroom-behavior-validation-v4-restored.html`。
- SHA256：`6F7A7977C7696DECF57F190FA7AF5DD40E3674FA4987A5BEB3486AAD4884721A`。开发树不含原型时读取此绝对路径，不退回旧出片网页自行设计。
- 对照条件：1536×864、浏览器缩放 100%、同主题/同展开状态；按 V4 原有颜色、字体层级、1480px 最大外壳及 82px/242px/剩余空间三栏起步。窄屏按实际可用性适配并验证无溢出。
- 原型控制外观、文字层级、操作位置与既定交互；SQLite/API 控制真实状态、数据和安全。示例素材、假数字、模拟提示改为真实反馈；阶段范围仍只真实开放数字人，其他路线按原计划诚实禁用。
- 差异矩阵：`docs/validation/2026-09-06-cutroom-v4-fidelity-matrix.md`。截图存于 `runtime/workbench/acceptance/fidelity/`，逐行填写原型与产品路径、页面状态、实现版本、观察、允许差异和结论；初始均为未验证。
- 每个 UI 任务的执行者必须同时读取本节、相应矩阵行及原型；不能只按文字功能清单独立重画。

| 原型页面/关键操作 | 实现文件 | 承接任务与验收 |
| --- | --- | --- |
| 外壳、任务看板/列表、主题、折叠 | index.html、styles.css、app.mjs、state.mjs | Task 11；先完成代表页面门禁 |
| 新建/预检、主/通用目录选择 | task-create.mjs | Task 4/12；同路线与缺失输入状态对照 |
| 文案锁定、十二步与分镜推荐 | workflow-view.mjs、task-workspace.mjs | Task 2/13；检查当前阶段、下一动作与锁定 |
| 粗剪/右侧对象面板、拆分/撤销/重做 | scene-editor.mjs、media_catalog/rough_cut.py | Task 5/13；真实拆分持久化与版本影响 |
| 镜头素材库与包装素材库 | libraries-view.mjs | Task 3/12；分类、空态、离线与更新 |
| 本片包装组合、预览与确认 | packaging-editor.mjs | Task 6/13；按钮与确认独立 |
| AI 服务设置、配置/连接反馈 | settings-view.mjs | Task 12；配置/未配置/失败状态 |
| 队列、异常、恢复、锁定后修改弹窗 | task-workspace.mjs、app.mjs | Task 7/13；状态与影响说明 |
| 完整审片、发布包 | review-view.mjs | Task 8/13/15；观看状态与发布门禁 |

### 开发顺序：以 V4 源代码迁移展示层

Task 11–13 必须从上述 V4 HTML 的对应 section 和 CSS 抽取正式界面；先保持外观的迁移，再逐项接入真实数据/操作，不把原型重新翻译成另一套 UI。为每页在已有矩阵记录原型 data-view/CSS 选择器到正式模块的来源对应。原型文件仍只读，正式代码放入本工作树。

- Task 11：抽取 #cutroom-prototype 下外壳、导航、任务中心及相关样式，保留结构/尺寸/颜色/文案；拆出 styles.css 和 ES Modules 只改变组织方式。
- Task 12：从 data-view=create/preflight/library/packlib/settings 迁移页面，再绑定原生选择器、预检、素材目录和设置 API。
- Task 13：从 rough/recovery/packaging/review/publish 等页面迁移时间线、右侧对象面板、弹窗和按钮，再接拆分、撤销/重做、包装、恢复、审片及发布包。
- 先在明确的开发/测试预览中用可比数据检查迁移未改变外观；随后把演示数组/内存状态替换为 API/SQLite 投影，把模拟 toast 换成真实结果。正式入口不展示伪成功，不把原型阶段跳转当门禁通过。
- 移除反馈投票、原型服务器包装及无用模拟监听器，核验图标/字体等依赖；保留视觉效果，真实用户文本安全渲染。现有数据库、队列、门禁和渲染能力继续复用。

执行顺序：
1. Task 5 前补齐“在播放头拆分”的真实领域动作。先读取场景卡/旁白绑定规则，拆分仅限用于分别替换或调整 B-roll 的当前画面段，声音主轴不可切断或重排；验证画面段被拆成连续两个片段、时长与原有旁白绑定保持、不改原素材；口播主轨不能删除。若确有约束阻断，记录具体冲突交 Peter 决定，不能静默删去已确认功能。
2. Task 11 完成外壳＋任务中心后，由开发者在真实浏览器完成原型/产品同视口截图对照，修掉未批准差异并记录结果，才能展开 Task 12–13 的页面制作；独立后端任务可继续。
3. 每个页面完成即更新矩阵。Checkpoint A 保留 Peter 原有交互复核，但其前提是开发者已完成全部适用页面的原型对照。
4. Task 15 验收报告分别列“功能与数据”和“原型一致性”，引用矩阵及成对截图；不能以自动测试通过代替视觉通过。
<!-- PROTOTYPE-FIDELITY:END -->

## 按序开发与原型后台契约（2026-09-06 成本评估后确认）

Peter 已确认继续 Task 4–10 → Task 11–13，不提前插入静态前端迁移。Task 11 仍直接迁移 V4 展示层再接真实数据/操作；本节让后台在开发时就支持该原型，不改变现有任务编号与依赖顺序。

### 原型操作与后台支持核对

| 原型展示/操作 | 承接任务 | 后台必须提供的事实或动作 | 实施时必须验证 |
| --- | --- | --- | --- |
| 创建/预检 | 4/7/10 | checks、blocking、warnings、空间、机器/人工耗时估算、API 费用范围、队列观测、下一步和四次确认说明 | 真实依据、未知不填零、无付费副作用；Task 7 完成后实际接入队列观测 |
| 看板/列表/十二步 | 2/7/10 | task_id、current_stage、run_status、next_action、gates、stages，任务标题/路线/更新时间 | 两视图同源；阶段与运行状态分离；API 不让前端自行推断业务门禁 |
| 主/通用素材与更新 | 3/4/10 | 绑定目录、来源优先级、在线/变化、同步影响及确认 | 原型来源标识有真实出处，不能跨未绑定目录选材 |
| 画面段拆分、替换、排序、撤销/重做 | 5/10 | 当前场景 ID、来源时间范围、画面段、原旁白绑定、影响预览、真实编辑命令及历史 | 口播声音主轴连续，编辑持久化，旧锁定内容改变后重新确认 |
| 包装组合/预览/确认 | 6/10 | 方案及槽位、可选资产、预览结果/状态、草稿/确认版本、缺失警告 | 预览不等于确认；单槽位变更保留历史，不用前端假标记通过 |
| 排队/取消/恢复 | 7/10 | 持久任务状态、队列顺序、实际进度、阶段标签、错误与下一动作 | 仅服务端决定可取消/恢复；关闭浏览器不丢任务；等待时间有依据或未知 |
| 完整审片/发布 | 8/10 | 当前候选与 QA 快照身份、真实观看进度、可审批结论、驳回原因和发布结果 | 新候选使旧观看/审批失效；禁止模拟解锁发布 |

表中是实现要求，不表示对应功能已经通过。各 Task 的领域验证完成后再进入后续，不需要先做页面来证明存在接口。

### Task 4 预检响应补充

保留现有 checks/blocking/warnings/estimated_space_bytes/next_action，增加：
- `estimates.machine_seconds`：机器执行时长，单位秒。
- `estimates.human_seconds`：人工操作时长，单位秒，与机器时间分开。
- `estimates.api_cost`：本次已选步骤的 API 费用，unit 为配置中明确的币种；附 `excluded_costs` 说明未包含项目。
- `queue`：当前重任务槽位、正在运行任务、等待数量、观察时间及预计等待。
- `confirmations`：文案、画面、包装、完整审片四次人工确认的说明，仅供预检展示，不是审批记录。

三个估算项和预计等待均使用同一响应形状：
`{status: "known"|"estimated"|"unavailable", min: number|null, max: number|null, unit: string|null, basis: string|null, reason: string|null}`。
known/estimated 必须有真实依据、非负上下界且 min<=max；unavailable 的数值为 null，reason 非空。费用币种未知时 unit 为 null，不能暗示人民币。UI 将 unavailable 显示为“暂无法估算”并说明原因。

队列结构：
`{heavy_slot: "available"|"busy"|"unknown", running_job_id: string|null, waiting_count: number|null, observed_at: string, estimated_wait_seconds: Estimate}`。
observed_at 是真实观测时间，槽位状态只是此刻快照，不承诺未来仍空闲；入队时由 Task 7 再做实际互斥校验。无法读取队列时明确 unknown/null，不能默认“无人排队”。

示例：没有历史测速、人工操作基线、价格依据及队列来源时，必须如实返回：
```json
{
  "estimates": {
    "machine_seconds": {"status":"unavailable","min":null,"max":null,"unit":"seconds","basis":null,"reason":"尚无适用的本机处理耗时记录"},
    "human_seconds": {"status":"unavailable","min":null,"max":null,"unit":"seconds","basis":null,"reason":"尚无适用的人工操作基线"},
    "api_cost": {"status":"unavailable","min":null,"max":null,"unit":null,"basis":null,"reason":"所选远程步骤的用量或价格依据不完整","excluded_costs":["另行确认的缺失画面生成"]}
  },
  "queue": {
    "heavy_slot":"unknown","running_job_id":null,"waiting_count":null,
    "observed_at":"2026-09-06T05:24:06Z",
    "estimated_wait_seconds":{"status":"unavailable","min":null,"max":null,"unit":"seconds","basis":null,"reason":"队列来源暂不可用"}
  }
}
```
示例时间仅用于契约测试，运行时生成真实时间。未知是数据不足时的降级，不是跳过接入的默认实现。

### 数据来源与实现边界

- 机器耗时优先复用相同设备/步骤的真实历史测量或明确已有的测算基线，说明适用输入；人工时间只使用独立人工基线，不能从机器时间或“半小时出片”目标反推。已有依据则给区间，没有则未知。
- API 费用只依据已选步骤、可确定用量及已配置/记录的价格计算；缺用量、价格、币种或包含范围时未知，不能取原型示例数。只有确认本次所选步骤全部本地且不调用计费服务时才返回 known/0，并写明依据。未启用的视频生成不混入本次总价，也不表示其他远程步骤免费。
- 不为预检发起真实转写、渲染、计费模型调用或修改业务状态，不建设新的预测平台。估算值不是付款批准，与 job_payloads.cost_status / provider_request_id 的执行及计费记录分开。
- Task 4 用可注入的只读估算/队列来源编写测试。Task 7 提供真实持久队列观测并接回预检；Task 10 接入真实服务，保留 unknown 降级，不能用测试替身作为生产实现。
- 最小反例：依据齐全返回估算范围；缺测速/缺价格返回 null 而非 0；本地零新增 API 费有明确依据；队列忙/空/读取失败；预检后槽位变化仍由入队重新校验；费用范围及未包含项目不被 UI 丢失；无网络/付费/状态写入副作用。


<!-- HUMAN-AI-HANDOFF:START -->
## 人机协同交接（2026-09-07）

执行统一规范 1.2“人和 AI 的交接与反馈闭环”。Peter 在聊天中反馈，开发者负责记录和落实。保留 Task 4–10 → 11–13 → Checkpoint A → 14–15 → Checkpoint B；不增加逐页用户审批。产品内文案/画面/包装/审片四门禁与开发验收 A/B 是不同层次，均按原有要求执行。

### Checkpoint A 的具体交付

- 前提：Task 11 外壳＋任务中心已通过开发者代表页对照，Task 12–13 及全部适用 V4 页面/状态已自查；粗剪按钮接真实编辑动作，不能用模拟成功代替。
- 交付：已验证可打开的隔离开发版入口、提交/工作区版本、数据环境与测试素材说明、本次可用范围、未接入或不可用项。执行者届时填实际入口，不将计划端口当已可运行入口。
- Peter 试用动作：①切换看板/列表并进入同一任务，辨认阶段与下一步；②创建任务并体验素材目录选择、预检缺项和费用/未知状态提示；③在粗剪拆分画面、替换一段、撤销/重做；④区分包装素材库与本片方案，预览并确认方案。
- 开发者提前验证：两种视图同源、保存/重开恢复、声音主轴连续、门禁不可越过、原素材未改、视觉与原型一致。Peter 重点判断入口是否易找、提示是否理解、操作是否符合普通同事习惯。
- 范围：本节点不宣称 Task 14 真实出片链路完成。按原有 A 门禁取得明确结果后进入依赖工作；实现偏差先修，涉及设计取舍只提出具体问题。

### Checkpoint B 的具体交付

- 前提：Task 14–15 完成适用自动化与真实浏览器验证，并备好验收副本、可播放成片及发布包；开发者先跑完整流程。
- 交付：核验过的产品入口与版本、实际验收数据范围、成片及发布包可访问位置、功能/原型双重结果、A 节点反馈关闭情况和剩余限制。
- Peter 试用动作：①以准备好的数字人任务确认输入与文案；②调整画面并确认包装；③体验渲染排队及关闭浏览器后返回；④完整观看成片并完成原有审片/发布动作，判断字幕、画面和声音效果是否满足业务使用。
- Peter 的产品体验认可不代替普通同事电脑或团队发布的原有现场验收。没有取得本节点结果时分别报告“开发验收结果”和“用户试用待确认”。

### 反馈与证据落点

统一记入 docs/validation/2026-09-06-cutroom-v4-fidelity-matrix.md 的“用户试用与反馈记录”，反馈 ID 使用 V4-F 序号并关联本计划 Task。Task 15 最终报告引用同一记录，不复制另一份状态表。开发者负责分类、具体变更确认依据、修复版本和复验；不得预填 Peter 已认可。
<!-- HUMAN-AI-HANDOFF:END -->

## 1. 范围与固定合同

连续交付两个里程碑：

1. **Checkpoint A — 工作台骨架：** 真实 SQLite 任务中心、看板/列表、十二步主链、素材库、设置、主题、折叠导航和安全启动。
2. **Checkpoint B — 数字人真实纵切：** 新建数字人任务，指定禅镜 MP4、最终口播文案、主素材目录和通用素材目录，完成素材同步、场景调整、画面锁、包装确认、渲染、QA、完整观看和发布包；让 `p2-xinjiang-v2` 从验收库副本真实进入工作台。

本阶段不做园区和信息流真实出片、多轨编辑器、自动发布、云协作、剪映工程文件。园区与信息流只显示开放顺序，不能进入假流程。付费视频生成只显示能力位置、费用说明和“尚未启用”，不发起真实调用。

十二步稳定键：

```python
WORKFLOW_STAGES = (
    ("create_preflight", "创建与预检"),
    ("brief_lock", "文案锁定"),
    ("material_sync", "素材增量同步"),
    ("ai_storyboard", "AI分镜推荐"),
    ("rough_cut", "粗剪"),
    ("picture_order", "调整画面顺序"),
    ("picture_lock", "画面锁定"),
    ("packaging_confirm", "包装方案确认"),
    ("fine_render", "精剪成片"),
    ("auto_qa", "自动质检"),
    ("full_review", "完整审片"),
    ("publish_package", "发布包"),
)
```

阶段状态固定为 `not_started/ready/in_progress/completed/invalidated/blocked`；运行状态固定为 `draft/awaiting_user/queued/running/paused/blocked/failed/interrupted/completed/cancelled`。两者必须分开。

四道门禁复用可信事实：`task_gate_events.brief`、`picture_lock`、`packaging_plan.confirm_plan()`、绑定 QA 快照的 `review_approvals`。`tasks.status` 只作兼容，不能单独证明可发布。

允许的粗剪动作只有替换当前场景、在播放头拆分可编辑片段、排序、调整入/出点、删除 B-roll/信息卡、恢复、撤销、重做。拆分仅作用于当前画面段，用于分别替换或调整 B-roll；数字人口播的声音主轴必须保持连续，不得被切断或重排。 拆分保持总时长、来源时间范围和旁白绑定；拆分及其撤销/重做必须持久化并遵守锁定后影响确认。数字人口播主轨不可删除；修改口播必须回到文案阶段并重新导入禅镜最终 MP4。

---

## 2. 实施任务

### Task 1：工作台数据库迁移

**Files:**
- Create: `media_catalog/migrations/011_workbench.sql`
- Create: `media_catalog/migrations/012_rough_cut_edits.sql`
- Create: `media_catalog/migrations/013_job_payloads.sql`
- Create: `media_catalog/migrations/014_watch_ranges.sql`
- Modify: `tests/test_database.py`
- Create: `tests/test_workbench_schema.py`

**TDD steps:**
1. 先写测试，断言新增表 `task_source_scopes/task_source_snapshots/task_stage_states/review_watch_progress/review_watch_ranges/model_provider_settings/rough_cut_edits/job_payloads`；`jobs` 新增 `task_id/progress_current/progress_total/phase_label/cancel_requested/cancel_reason/started_at/finished_at`。
2. 验证单任务最多一个 `primary`、观看秒数非负、阶段键/状态受枚举约束、`provider_request_id` 唯一。
3. 运行 `.venv\Scripts\python -m unittest tests.test_workbench_schema -v`，确认先失败。
4. 实现只增不改的 011–014 迁移：011 保存任务素材范围、素材摘要、十二阶段状态、观看汇总、模型能力设置和 jobs 扩展；012 保存粗剪 `before_json/after_json/impact_digest/status`；013 保存 job payload、费用状态、唯一供应商请求 ID；014 保存服务端合并的观看区间。不得修改 001–010。
5. 运行 `.venv\Scripts\python -m unittest tests.test_database tests.test_workbench_schema -v` 和 Python 全量。
6. Commit: `git commit -m "feat: add durable workbench state schema"`。

### Task 2：单一工作流投影

**Files:**
- Create: `media_catalog/workflow.py`
- Create: `tests/test_workflow.py`
- Modify: `media_catalog/assemble.py`
- Modify: `tests/test_assemble.py`

**TDD steps:**
1. 测试空任务、锁文案、锁画面、包装确认、渲染/QA、完整观看/审批、发布包七种状态；稳定返回 `task_id/current_stage/run_status/next_action/gates/stages`。
2. 回归断言只把 `tasks.status='reviewed'` 不能通过审片或发布。
3. 实现 `project_workflow()`、`seed_stage_states()`、`complete_stage()`、`invalidate_from()`；优先读取门禁事件、选择摘要、确认包装、受信 render/QA 快照、观看区间、绑定审批和发布记录。
4. 固定失效链：文案变化从 brief 后失效；素材范围/摘要从 sync 后失效；镜头/顺序/入出点从 rough cut 后失效；包装从 packaging 后失效；产物/依赖从 QA 后失效。保留历史快照。
5. 让 `assemble.gate_status()` 调用同一门禁投影并增加 `brief_locked`。
6. Run: `.venv\Scripts\python -m unittest tests.test_workflow tests.test_assemble tests.test_task5_provenance -v`。
7. Commit: `feat: project the trusted twelve-stage workflow`。

### Task 3：主素材与通用素材范围

**Files:**
- Create: `media_catalog/task_sources.py`
- Create: `tests/test_task_sources.py`
- Modify: `media_catalog/tasks.py`
- Modify: `tests/test_libraries.py`

**TDD steps:**
1. 测试恰好一个主目录、多个指定通用目录、自动默认通用目录、主目录离线阻断、通用目录离线提示、改名保留 asset ID、锁定后变化只失效不替换、禁止跨未绑定园区目录。
2. 实现：
```python
def set_task_sources(db, task_id, *, primary_library_id, selected_generic_ids, include_default_generic): ...
def get_task_sources(db, task_id): ...
def sync_task_sources(db, task_id, *, workers=4): ...
def source_health(db, task_id): ...
```
3. 返回 `primary/selected_generic/default_generic`，每个目录含 ID、名称、挂载、在线、素材数、最后同步。优先级为主目录 > 本次指定通用 > 默认通用。
4. Run: `.venv\Scripts\python -m unittest tests.test_task_sources tests.test_libraries tests.test_scanner -v`。
5. Commit: `feat: bind primary and shared material folders to tasks`。

### Task 4：数字人任务创建与预检

**Files:**
- Create: `media_catalog/digital_workflow.py`
- Create: `tests/test_digital_workflow.py`
- Modify: `media_catalog/errors.py`

**TDD steps:**
1. 在任何写入前测试路线/子类型、禅镜最终 MP4、非空最终文案、主目录在线、FFmpeg/ffprobe/Node/ts-node/Whisper 和磁盘空间。失败不留半任务、不覆盖原片、不产生费用。
2. 实现 `preflight_digital_task(...)` 保留 `checks/blocking/warnings/estimated_space_bytes/next_action`，并按“原型后台契约”返回 `estimates/queue/confirmations`。实现 `create_digital_task(db, request, project_root)`；先用可注入只读来源验证估算上下界与依据、未知 null、本地零新增 API 费、队列忙/空/不可读、无网络计费或业务写入副作用。不能把估算成功当作付费批准。
3. 创建时生成不可猜测 task ID；在 `runtime/workbench/tasks/<id>/inputs/` 保存 UTF-8 文案副本和只读来源记录，不复制/改写原 MP4；登记/复用 digital_human asset；绑定素材范围；初始化十二阶段。
4. Run: `.venv\Scripts\python -m unittest tests.test_digital_workflow tests.test_tools tests.test_errors -v`。
5. Commit: `feat: create preflighted digital-human tasks`。

### Task 5：受控粗剪、拆分与撤销

**Files:**
- Modify: `media_catalog/scene_cards.py`
- Create: `media_catalog/rough_cut.py`
- Create: `tests/test_rough_cut.py`
- Modify: `tests/test_task5_provenance.py`

**TDD steps:**
1. 测试替换只影响当前场景且素材属于允许范围；排序恰好含现有卡片一次；裁剪不越界；只能删除 B-roll/信息卡；undo/redo 不碰原素材；锁定后的编辑先预览影响，匹配 `impact_token` 才执行。
2. 实现 `preview_edit_impact()`、`apply_edit()`、`undo_last_edit()`、`redo_last_edit()`；修改、日志和失效同事务。在现有编辑命令中加入 split 动作，先补失败测试：片段内部有效播放头拆为连续两段，总时长和原有来源/旁白绑定不变；端点或越界拆分拒绝；新片段 ID 唯一；锁定后先预览影响；撤销/重做及刷新/重启恢复；原素材不变。核对 scene_cards 与实际旁白绑定后实现，不能只显示模拟提示。
3. 修复通过拆分 `card_id` 推断 task ID 的脆弱做法，改为查库。
4. Run: `.venv\Scripts\python -m unittest tests.test_rough_cut tests.test_task5_provenance tests.test_render_mix_adapter -v`。
5. Commit: `feat: add bounded rough-cut edits with undo`。

### Task 6：包装推荐后自由组合

**Files:**
- Modify: `media_catalog/packaging_plan.py`
- Create: `tests/test_packaging_composition.py`
- Modify: `tests/test_render_mix_adapter.py`

**TDD steps:**
1. 测试应用推荐整套、替换单槽位、关闭可选槽位、空白方案、个人模板；确认方案不可原地覆盖；画面锁前不能确认；必需槽位/授权继续阻断；预览不等于确认。
2. 实现 `clone_plan()`、`create_blank_plan()`、`replace_item()`、`disable_optional_slot()`、`save_personal_template()`。已确认方案的修改必须克隆为 draft 并失效后续阶段；确认只绑定当前完整槽位摘要。
3. Run: `.venv\Scripts\python -m unittest tests.test_packaging_composition tests.test_render_mix_adapter tests.test_subtitle_binding -v`。
4. Commit: `feat: compose packaging plans safely`。

### Task 7：持久单重任务队列

**Files:**
- Modify: `media_catalog/jobs.py`
- Create: `media_catalog/workbench_worker.py`
- Create: `tests/test_workbench_worker.py`
- Modify: `tests/test_jobs.py`

**TDD steps:**
1. 测试所有 `HEAVY_TYPES` 都参与互斥查询，修复当前 SQL 只检查四类的缺口；第二个重任务 pending；浏览器断开不影响 worker；重启将陈旧 running 标 interrupted 并保留 cursor；取消在安全点生效；生成式步骤不自动重试；相同 provider request ID 不重复创建。
2. 实现 `enqueue_job()`、`request_cancel()`、`resume_job()`、`list_jobs()` 和 `WorkbenchWorker.run_next/run_forever`。
3. 检查点固定为 `prepare/transcribe/sync/recommend/render/qa/await_review/publish`；错误只保存有界摘要和下一步，日志不写密钥。提供真实持久队列的只读观测并接回 Task 4 预检：当前重任务/等待数量/观测时间/有依据的等待范围；无依据明确未知。验证队列观测与实际入队之间变化时互斥仍正确，不能将预检空闲视作槽位预留。对外返回实际进度、阶段和可执行下一动作。
4. Run: `.venv\Scripts\python -m unittest tests.test_jobs tests.test_workbench_worker -v`。
5. Commit: `feat: add durable single-heavy-job queue`。

### Task 8：完整观看门禁

**Files:**
- Create: `media_catalog/full_review.py`
- Create: `tests/test_full_review.py`
- Modify: `media_catalog/reviews.py`
- Modify: `media_catalog/assemble.py`
- Modify: `tests/test_publish_package.py`

**TDD steps:**
1. 测试观看绑定 task + QA snapshot；服务端合并连续 tick；直接报 100% 或跳播无效；覆盖率 98% 且到片尾、输出签名一致才可审批；新渲染后旧进度/审批失效；驳回必填备注。
2. 实现 `start_watch()`、`record_watch_tick()`、`complete_watch()`、`approve_after_full_watch()`；只相信服务端区间，不相信前端百分比。
3. Run: `.venv\Scripts\python -m unittest tests.test_full_review tests.test_review_decisions tests.test_publish_package tests.test_assemble -v`。
4. Commit: `feat: require full-video watch before release`。

### Task 9：安全本地服务与原生选择器

**Files:**
- Create: `workbench/__init__.py`
- Create: `workbench/server.py`
- Create: `workbench/routes.py`
- Create: `workbench/security.py`
- Create: `workbench/native_picker.py`
- Create: `tests/test_workbench_server.py`
- Modify: `启动出片工具.bat`
- Modify: `web-ui/server.js`

**TDD steps:**
1. 测试只监听 `127.0.0.1`；启动 key 换取 `HttpOnly; SameSite=Strict` cookie；写请求校验 cookie/Origin/JSON/体积；路径逃逸、任意下载和链接逃逸被拒；错误结构稳定；重启从 SQLite 恢复；选择器可注入 fake。
2. 实现 `python -m workbench.server --db <path> --port 8765 --no-open`：迁移 DB、恢复 job、启动一个 worker、生成随机 key、监听本机并打开浏览器。
3. `native_picker.py` 用隐藏 tkinter 提供 `pick_video/pick_directory`，返回路径仍由服务端复验。
4. `web-ui/server.js` 仅兼容转交，不再持有 Map；修复 BAT 当前错误的 `%~dp0..`，改从项目根启动 Python 服务。
5. Run: `.venv\Scripts\python -m unittest tests.test_workbench_server -v`。
6. Commit: `feat: serve the persistent local workbench`。

### Task 10：显式且门禁安全的 API

**Files:**
- Create: `workbench/api.py`
- Create: `tests/test_workbench_api.py`
- Modify: `workbench/routes.py`

**TDD steps:**
1. 覆盖任务 bootstrap/list/create/detail，brief lock、source sync、storyboard recommend、rough-cut impact/apply/undo/redo、picture lock、packaging propose/compose/confirm、render、watch start/tick、review approve/reject、publish package、job cancel/resume、原生选择器和 diagnostics。
2. 约定非法 400、不存在 404、门禁冲突 409、排队 202、同步成功 200；不得提供直接设阶段完成、直接设 reviewed 或绕过审批的通用接口。
3. `workbench/api.py` 只调用领域函数，不写裸 SQL、不执行 shell 字符串；业务响应不显示底层技术名，诊断只返回 diagnostic ID。按“原型后台契约”逐项核对任务中心、素材、编辑、包装、队列、审片的字段与动作。新增 API 契约测试：预检 estimates/queue/confirmations 原样保留真实语义；未知不转 0，估算不转审批；价格范围和未包含项目保留；队列观测来自 Task 7，不能在生产回退假空队列；服务端门禁结论不得由前端覆盖。
4. Run: `.venv\Scripts\python -m unittest tests.test_workbench_api tests.test_workbench_server -v`。
5. Commit: `feat: expose gate-safe workbench APIs`。

### Task 11：迁移 V4 外壳与任务中心，再接真实任务

**Files:**
- Modify: `web-ui/public/index.html`
- Create: `web-ui/public/styles.css`
- Create: `web-ui/public/app.mjs`
- Create: `web-ui/public/state.mjs`
- Create: `web-ui/public/workflow-view.mjs`
- Create: `tests/workbench-ui.test.mjs`
- Modify: `package.json`

**TDD steps:**
1. 用 `node:test` 验证看板/列表同 task ID、状态、下一动作；看板列为等待我处理/自动处理中/排队/阻塞或失败/最近完成；两种呈现打开同一任务；十二步完整；阶段与运行状态分离；主题和折叠偏好持久化。增加 `npm run test:web`。
2. 从冻结 V4 的 #cutroom-prototype 外壳和 data-view=tasks 抽取 HTML/CSS，迁入 index.html/styles.css；保留节点层级、类名对应、布局尺寸、颜色、文字和点击位置。提取 UI-only 事件到 app.mjs/state.mjs，移除反馈投票和模拟业务成功。先在测试预览检查迁移外观，再把任务数组替换成真实 API 投影；左栏为任务/镜头素材/包装素材/设置并可折叠；中栏在任务页显示任务快速列表和看板/列表切换，在其他页显示目录/分类；右侧为主工作区。顶栏显示面包屑、后台、重任务和主题。
3. 看板不可拖过门禁。任务卡显示标题、路线、阶段、运行状态、下一动作、更新时间和阻塞原因。继承原型已有 CSS 令牌和选择器关系，补清晰焦点、44px 点击区、reduced-motion 时保留已确认布局；业务界面不显示 SQLite/FFmpeg/Whisper/模型 ID。
4. Run: `npm run test:web` 和 `npm run test:ts`。随后执行原型实现合同中的代表页面门禁，实际查看 V4 与外壳/任务中心同视口截图，记录矩阵首行。该门禁未通过前不铺开 Task 12–13 页面。
5. Commit: `feat: build the workbench shell and task center`。

### Task 12：迁移 V4 创建/素材/设置页并接真实能力

先从 V4 的 create/preflight/library/packlib/settings section 迁移既定页面结构、样式及按钮位置到下列模块，按原型选择器在矩阵记录来源；先核对外观，再执行下列测试与 API 接入，不能重新设计一套表单和素材页。

**Files:**
- Create: `web-ui/public/task-create.mjs`
- Create: `web-ui/public/libraries-view.mjs`
- Create: `web-ui/public/settings-view.mjs`
- Create: `media_catalog/credentials.py`
- Create: `tests/test_credentials.py`
- Modify: `workbench/api.py`
- Modify: `tests/workbench-ui.test.mjs`

**TDD steps:**
1. 测试创建顺序：路线 → 数字人子类型 → 禅镜 MP4 → 最终文案 → 主素材目录 → 可选通用目录 → 用途/时长 → 预检。园区/信息流不能进入未实现流程。
2. 镜头素材库显示目录、在线、素材数、最后同步、用途和“检查更新”；可新增主/通用目录。包装素材库按字幕、音色、BGM、文字卡、贴纸、花字、Logo/水印分类与授权，不混入本片方案。
3. 设置接口只返回能力、供应商、模型、是否配置和检测时间。实现 `save_secret/read_secret/delete_secret/configured_capabilities`；Windows 用 ctypes 调 Credential Manager，测试注入内存 backend；SQLite 只存 credential ref；`.env` 只兼容读取；日志/错误不得含密钥。
4. Run: `.venv\Scripts\python -m unittest tests.test_credentials tests.test_workbench_api -v` 与 `npm run test:web`。
5. Commit: `feat: add guided creation libraries and secure settings`。

### Task 13：迁移 V4 阶段工作区并替换模拟操作

先从 V4 的 rough/recovery/packaging/review/publish section 及十二步条迁移页面、时间线、对象面板和弹窗。保留外观与操作位置，再用真实 API/状态替换 data-edit 等模拟处理器；拆分按钮必须真正影响画面段，不能保留只显示提示的演示实现。

**Files:**
- Create: `web-ui/public/task-workspace.mjs`
- Create: `web-ui/public/scene-editor.mjs`
- Create: `web-ui/public/packaging-editor.mjs`
- Create: `web-ui/public/review-view.mjs`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/workbench-ui.test.mjs`

**TDD steps:**
1. 测试页面始终回答“现在做什么/系统已完成什么/下一步”；选镜头后右侧显示画面、旁白、时间、来源、真实性和仅替换当前镜头；编辑先显示影响；包装支持整套推荐与自行组合，预览/确认分开。
2. 每个 AI 按钮明确是文案模型、镜头匹配、语音识别、TTS 或视频生成；改写/生成/付费动作显示输入、影响和费用，先生成候选不覆盖。视频生成显示“尚未启用付费生成”。
3. 按 API current_stage 渲染；阶段条只可查看，不可强行执行后续；编辑旧阶段必须走影响预览；刷新后从 API 恢复。
4. Run: `npm run test:web`。
5. Commit: `feat: add guided digital-human workspaces`。

### Checkpoint A：交互复核

开发者先完成 V4 矩阵全部适用页面/状态的同视口对照，实际查看成对截图并关闭未批准差异。然后请 Peter 在真实浏览器检查三栏、看板/列表、十二步和运行状态、主/通用/包装素材与本片方案的区分、AI 按钮用途/费用，以及含拆分/撤销/重做的粗剪。这里只确认交互与原型一致性，不宣称真实出片完成。

### Task 14：真实数字人链路接入队列

**Files:**
- Create: `media_catalog/workbench_actions.py`
- Create: `tests/test_workbench_actions.py`
- Modify: `media_catalog/workbench_worker.py`
- Modify: `workbench/api.py`
- Modify: `scripts/transcribe.py`

**TDD steps:**
1. fake runner 验证：锁文案 → words/srt → 同步素材 → 按最终文案和词级时间推荐候选 → 人工调整/确认 → 画面锁 → 包装确认 → assemble render → QA → 完整观看 → 审批 → 发布包。
2. 任一步失败停在一致检查点；重进不重复转写/渲染或覆盖；输出为 `out/<task_id>/<version>/video-final.mp4`；只从绑定目录取材；Whisper 只供时间戳，文本始终使用锁定原文；未确认场景不能锁画面。
3. 实现 handlers：`transcribe/sync_sources/recommend_storyboard/render_and_qa/build_publish_package`。每个接受 `(db, job, project_root, runner)` 并返回稳定 cursor/progress/result；外部进程用现有受控 runner/ATR 参数数组，禁止 shell=True。transcribe 只增加显式输出参数并保持旧命令兼容。
4. Run: `.venv\Scripts\python -m unittest tests.test_workbench_actions tests.test_workbench_worker tests.test_render_mix_adapter tests.test_assemble -v` 和 `npm run test:ts`。
5. Commit: `feat: run the trusted digital-human pipeline from the workbench`。

### Task 15：`p2-xinjiang-v2` 真实纵切验收

**Files:**
- Create: `scripts/prepare_workbench_acceptance.py`
- Create: `scripts/smoke_workbench.py`
- Create: `tests/test_prepare_workbench_acceptance.py`
- Create: `docs/validation/2026-09-05-workbench-phase1-acceptance.md`
- Modify: `web-ui/README.md`
- Modify: `README.md`

**TDD and validation:**
1. 用 SQLite backup API 把 `runtime/media-catalog/p2-v2-acceptance.sqlite` 复制到 `runtime/workbench/acceptance/p2-xinjiang-v2.sqlite`。目标存在默认拒绝；`--replace` 也先备份。测试源库哈希/内容不变。
2. Run: `.venv\Scripts\python scripts/prepare_workbench_acceptance.py`。
3. Run: `.venv\Scripts\python -m workbench.server --db runtime/workbench/acceptance/p2-xinjiang-v2.sqlite --port 8765 --no-open`。Expected: bootstrap 返回真实新疆任务、四门禁、QA、审批和发布包。
4. Run: `.venv\Scripts\python scripts/smoke_workbench.py --base-url http://127.0.0.1:8765 --task p2-xinjiang-v2`。验证页面、看板/列表同源、十二步、受控预览、七文件发布包、门禁不可绕过、路径逃逸拒绝、响应无密钥。
5. 在副本中新建数字人任务：选择禅镜 MP4 和素材范围；锁文案；同步/推荐；拒绝和替换一个镜头；调整顺序并 undo/redo；锁画面；复制包装并换槽位后确认；渲染时关闭浏览器再打开；未完整观看审批必须失败；完整播放后审批并生成发布包；修改锁定输入后确认后续失效、旧版本仍在。
6. 全量回归：
```powershell
.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"
npm run test:ts
npm run test:web
```
7. 验收报告记录测试数量、耗时、产物路径和门禁证据；分别列功能/数据结果与原型一致性结果，引用 V4 矩阵及每页成对截图、原型哈希和实际观察。两类验收均通过且无未解决差异后，才声明本批次完成。
8. Commit: `test: accept the colleague workbench digital-human slice`。

### Checkpoint B：通过条件

真实 SQLite 非假数据；新任务由 UI 完成四门禁、真实渲染、QA 和发布包；看板/列表同源；十二步准确；浏览器关闭后台继续；服务重启可恢复；未完整观看不能审批；不跨目录取材；原素材/旧快照/验收源库未覆盖；付费视频生成未启用且无重复扣费路径。

---

## 3. 红队回归与硬指标

演练：画面锁＋包装确认后修改文案；主素材移动盘离线；存在已记录 provider request ID 但结果未下载的模拟付费任务；关闭浏览器并重启服务。系统必须保留旧版本、明确失效门禁、不跨园区替换、不重复付费，从一致 cursor 恢复，发布包只绑定重新确认后的版本。

硬指标：数据丢失 0、重复付费 0、跨园区替换 0、门禁绕过 0、原素材修改 0。

## 4. 计划自检

- 覆盖三栏、看板/列表、十二步、四门禁、路线选择、主/通用素材、镜头/包装素材库、包装组合、AI 分工、API Key 设置、受控粗剪、队列恢复、QA/完整观看和发布包。
- 只真正开放数字人；园区和信息流不并行开发；不建设多轨编辑器。
- SQLite 是任务、门禁、队列、快照和观看状态唯一真相；浏览器不能决定发布。
- 复用现有 media_catalog、assemble 和渲染适配器；旧 Node 网页只兼容转交。
- 安全边界包含 localhost、随机会话、同源、路径 allowlist、原素材只读、凭据不落 DB/日志、付费不自动重试。
- 每项有先失败后通过的测试；Checkpoint A 检交互，Checkpoint B 检真实出片；无实现占位项。
