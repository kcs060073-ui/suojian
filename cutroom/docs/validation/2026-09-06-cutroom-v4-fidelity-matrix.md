# 剪辑工作台 V4 原型一致性矩阵

状态：Task 11–13 的适用页面与状态已完成实际视觉检查；Task 15 已接入并实际播放真实候选，完整观看后的批准状态和新任务完整纵切仍待 Checkpoint B。更新日期：2026-09-08。

基准和页面/实现映射见 Phase 1 实施计划“原型实现合同”。默认条件：1536×864、100% 缩放、同主题/同交互状态；截图在 runtime/workbench/acceptance/fidelity/。原型列与产品列必须是可访问的实际文件路径，不能以“截图已保存”替代。

| 页面/状态 | Task | 检查重点 | 原型截图 | 产品截图 | 实现版本/观察/差异 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 外壳＋任务看板/列表，明亮/暗色/跟随系统 | 11，先验代表页 | 三栏、尺寸、导航、卡片、主题、折叠 | runtime/workbench/acceptance/fidelity/task11-v4-board-light-1536x864.png；task11-v4-list-light-1536x864.png；task11-v4-board-dark-collapsed-1536x864.png | runtime/workbench/acceptance/fidelity/task11-product-board-light-1536x864.png；task11-product-list-light-1536x864.png；task11-product-board-dark-collapsed-1536x864.png | 1536×864、100% 实际查看。V4 的 app/top/shell/rail/taskboard/kanban/listview/theme 结构迁入 index.html＋styles.css，app.mjs/state.mjs 只替换真实任务投影和偏好。1480px 外壳、82/242/主区、五列、任务快速列表及明暗令牌一致；正式版按计划增加真实重任务状态并移除反馈投票。发现 V4 折叠写法会令主区落入 242px 列，已修为 242px＋主区并补回归测试；此项属于可用性缺陷修复。 | 通过 |
| 创建与预检，齐全/缺输入 | 4/12 | 字段顺序、主通用目录、预检及主动作 | runtime/workbench/acceptance/fidelity/task12-v4-create-light-1536x864.png；task12-v4-preflight-light-1536x864.png | runtime/workbench/acceptance/fidelity/task12-create-real-light-1536x864.png；task12-preflight-pending-real-light-1536x864.png；task12-preflight-missing-real-light-1536x864.png | 1536×864、100% 实际查看。路线→子类型→禅镜 MP4→最终文案→主目录→通用目录→用途/时长→预检顺序保留；项目/信息流诚实禁用。真实工具探测约需数十秒，首轮发现无等待反馈及步骤条仍停在第 1 步；已补“正在检查本机环境”、禁止重复提交与第 3 步高亮。缺输入返回 11 项实际检查，创建按钮禁用。 | 通过 |
| 文案与分镜，未锁定/锁定/候选 | 2/13 | 十二步、下一动作、候选采用与拒绝 | runtime/workbench/acceptance/fidelity/task12-v4-preflight-light-1536x864.png；task13-v4-rough-light-1536x864.png | runtime/workbench/acceptance/fidelity/task13-storyboard-light-1536x864.png | V4 没有独立的 AI 分镜页，而是在预检后模拟跳到粗剪；正式版按已确认十二步合同补出真实 AI 分镜阶段。实际查看显示当前动作、已完成门禁、下一步、主/通用范围，以及通用文本模型的输入、影响和费用；按钮只入持久队列，不伪造候选。候选产出由 Task 14 handler 接入；逐镜头采用、替换或删除在粗剪工作区完成。 | Task 13 交互通过；真实候选待 Task 14 |
| 粗剪及右侧面板，真实素材预览/选择/拆分/撤销/重做 | 5/13 | 时间线、对象信息、真实图片/视频、候选预览、连续播放、锁定门禁、真实状态恢复 | 原 V4 色块截图仅作布局参考，不再作为功能验收证据 | [真实主预览与时间线](./evidence/rough-cut-real-media/actual-task-after-edit.png)；[91 个真实候选素材](./evidence/rough-cut-real-media/actual-candidate-gallery.png)；[离线素材与锁定阻断](./evidence/rough-cut-real-media/offline-lock-button.png)；[专项验收报告](./2026-09-20-rough-cut-real-media-acceptance.md) | 2026-09-20 撤回旧色块实现的“通过”结论。当前页面已显示任务实际素材；候选点选仅预览，确认后才写入；替换、排序、5 秒播放头拆分和画面锁定已在隔离的 9 镜头/91 候选真实任务上落账。图片不显示无效裁剪；视频片段裁剪由浏览器及后端回归覆盖。离线副本明确指出第 1 镜头并同时由前后端拒绝锁定，恢复后解除。 | 粗剪真实素材与锁定门禁通过；该遗留任务缺包装方案和配音资产，未计入最终成片端到端验收 |
| 镜头素材库，正常/空/离线 | 3/12 | 目录分类、来源、更新及恢复入口 | runtime/workbench/acceptance/fidelity/task12-v4-library-light-1536x864.png | runtime/workbench/acceptance/fidelity/task12-library-real-light-1536x864.png；task12-library-normal-offline-real-light-1536x864.png；task12-library-empty-real-light-1536x864.png | 使用隔离 SQLite 实际查看主素材、通用补充、在线、离线及空账本。目录、素材数、最后同步、用途、登记目录和检查更新均来自真实 API；离线目录仍保留，不显示为已删除。 | 通过 |
| 包装素材库、本片组合及确认 | 6/12/13 | 库与方案区分、整套/单项、预览与确认 | runtime/workbench/acceptance/fidelity/task12-v4-packlib-light-1536x864.png；task13-v4-packaging-light-1536x864.png | runtime/workbench/acceptance/fidelity/task12-packlib-real-light-1536x864.png；task13-packaging-light-1536x864.png | 七类长期包装资产与“本片包装方案”明确分开。实际查看系统推荐和我的自选组合同时存在；选择卡片只预览，确认是独立动作；可复制推荐为自选组合并逐槽位替换授权资产。修复了数字人内部路线名导致推荐失败的问题，方案列表顺序稳定。 | 通过 |
| 设置，已配置/未配置/连接失败 | 12 | 能力分工、配置反馈、密钥遮蔽 | runtime/workbench/acceptance/fidelity/task12-v4-settings-light-1536x864.png | runtime/workbench/acceptance/fidelity/task12-settings-real-light-1536x864.png | 实际查看通用文本、视频生成、TTS、素材匹配四项独立能力及已配置/未配置状态。密钥输入遮蔽，响应不含明文或 credential ref，SQLite 只留引用，Windows Credential Manager 保存；.env 仅兼容读取。真实供应商连接失败态留待 Task 15 联调，不在此伪造成功。 | Task 12 范围通过 |
| 队列、异常恢复、锁定后修改 | 7/13 | 排队、离线、影响弹窗与下一步 | runtime/workbench/acceptance/fidelity/task13-v4-recovery-light-1536x864.png；task13-v4-rough-light-1536x864.png | runtime/workbench/acceptance/fidelity/task13-recovery-light-1536x864.png；task13-locked-impact-light-1536x864.png；task11-product-board-light-1536x864.png | 实际 SQLite 夹具构造离线主素材和失败作业。任务优先进入安全暂停；可选择移动后的新目录，保持 library ID 与既有镜头引用，再入真实扫描队列。失败/中断只在服务端 canResume 时显示继续，付费或供应商结果未知时明确不自动重试；运行/排队允许服务端授权的取消。锁定后修改使用真实 impact token，影响阶段已翻译为中文。 | 通过 |
| 完整审片与发布包，未看完/看完/新版本 | 8/13/15 | 播放布局、审批状态与发布入口 | runtime/workbench/acceptance/fidelity/task13-v4-review-light-1536x864.png；task13-v4-publish-light-1536x864.png | runtime/workbench/acceptance/fidelity/task13-review-light-1536x864.png；task13-publish-light-1536x864.png；task15-review-pending-real-1536x864.png；task15-review-check-1536x864.png | 1536×864 实际查看候选、QA、观看与发布状态。Task 15 已用受控媒体接口加载真实 69.88 秒成片，浏览器 `readyState=4` 且无媒体错误；Range、当前 QA 候选签名和客户端断开均由服务端处理。任务详情只恢复与当前 QA 快照匹配的观看会话，确认按钮服从服务端 `can_approve`，新候选不会沿用旧观看。当前尚无人工完整观看会话，旧审批和旧发布记录不会放行当前阶段。 | 真实播放接入通过；完整观看后状态与新发布包待 Checkpoint B |
| 窄屏与键盘焦点 | 11–13 | 无遮挡/横向溢出，焦点和操作可达 | runtime/workbench/acceptance/fidelity/task13-v4-rough-narrow-760x1000.png | runtime/workbench/acceptance/fidelity/task13-rough-narrow-760x1000.png | 760×1000、100% 实际查看。一级导航改为顶部四项，十二步变为两列，工作区与右栏纵向排列，时间线局部横向滚动且页面无整体横向溢出；按钮保持语义元素并有 focus-visible 样式。 | 通过 |

允许差异：真实数据替换示例数据；原型模拟提示替换真实 API 反馈；项目/信息流、付费视频生成按 Phase 1 已定范围诚实禁用。其他布局、文案、交互或功能删减需记录具体依据。

## 代表页面门禁

- 外壳＋任务中心实际对照：已于 2026-09-07 使用五种持久状态各一条的隔离 SQLite 数据，在 1536×864、100% 下检查看板、列表、暗色与折叠状态；通过。
- 开发者检查记录、成对截图及差异关闭：已完成。第一轮额外撑满视口、眉题与程序聚焦边框已移除；折叠列错位已修复并复验。
- Task 13 展开条件：已满足。粗剪、恢复、包装、审片、发布及窄屏均完成同视口实际查看；未把 Task 14–15 的真实出片与视频播放记作通过。
- Checkpoint A：开发自查完成；Peter 已于本轮明确确认通过，随后进入 Task 14–15。

## 最终双重验收

- 功能与数据：自动化和 `p2-xinjiang-v2` 真实候选/QA/历史七文件包已验证；新任务完整纵切、当前候选完整观看及新审批仍待完成。引用 [Task 15 真实验收报告](./2026-09-05-workbench-phase1-acceptance.md)。
- 原型一致性：Task 11–13 适用范围通过，Task 15 真实媒体加载通过；完整观看完成态与新发布包页仍需在 Checkpoint B 复验。
- 两者通过且完成项目原有用户检查点后，才可声明本批次完成。

<!-- HUMAN-AI-HANDOFF:START -->
## 用户试用与反馈记录（2026-09-07 起）

本节追加协同记录，既有逐页开发证据与结论保持不变。开发者填写，Peter 可直接在聊天中反馈。用户试用结果独立于开发自查；没有可核验来源不补造历史认可。

### 每次交付的记录

记录体验节点、日期、实际产品入口、提交/工作区版本、数据环境、可用范围、具体动作与预期、开发自查证据、已知未完成项、用户结果及其确认依据。实际入口由执行任务核验后填写；当前本新增节尚未登记新的试用交付，不表示此前从未体验。

### 反馈处理表

| 反馈 ID | 日期/节点/版本 | 页面/动作及问题与预期 | 分类与处理建议 | 用户决定与依据（需要时） | 对应 Task | 修复版本/开发复验 | 用户复验（需要时） | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V4-F001 | 2026-09-08 / Checkpoint B / Task 15 待提交工作树 | 用户反馈“审片已完成”，但真实库没有新 `review_watch_sessions`，只有无 `watch_session_id` 的旧审批。当前“开始完整审片”和视频播放是两个动作，第一次使用者可能直接播放而未建立会话。预期是不看说明书也不会漏记。 | 实现/UX 缺陷。首次点击播放时自动创建审片会话，成功后才继续播放并上报；保留手动开始按钮作为备用，失败明确提示，不追认未记录播放。 | Peter 明确同意该交互修正。 | 8/13/15 | 已完成控制器和实际页面接线的红→绿测试，目标 Web 测试 25/25；8766 已重启加载新代码。 | 需重新完整播放并确认记录落库。 | 待复验 |

只登记实际反馈，不将模板示例当问题。实现偏差由开发者直接修；设计变更先提供具体差异与影响，取得明确决定后落实；历史记录补记必须有可核验依据。

状态使用：待处理 / 待用户决定 / 修复中 / 待复验 / 已关闭 / 经确认后置。技术复验通过但仍需要用户判断时保持待复验；未经确认不得将已承诺功能后置。每次交付分别列“功能与数据”“原型一致性”“用户试用结果”，不能用其中一项替代另一项。
<!-- HUMAN-AI-HANDOFF:END -->
