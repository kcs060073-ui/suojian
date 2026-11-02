# CUTROOM 数字人和信息流路线设计收口验证报告

**日期：** 2026-09-22
**范围：** 数字人混剪、信息流广告混剪
**结论口径：** 软件实现与自动化验证完成；真人/现场验收与园区宣传路线不在本结论内。

## 1. 结论

数字人混剪和信息流广告混剪已经接入同一套十二步工作流、四道人工门禁、发布级 QA、完整审片、候选绑定和本地交付规则。自动化测试覆盖本轮设计差距及既有粗剪、包装、审片、返工与发布绑定回归。

本报告不把自动化验证等同于真人使用或现场验收，也不代表园区宣传路线已经实现。

## 2. 已完成的软件能力

- 任务标题/ID 搜索、路线与状态筛选、稳定排序和可恢复空状态。
- 数字人与信息流创建前预检完整展示机器/人工时间、空间、API 费用边界、队列观测、四道确认及真实性/隐私/授权提醒；未知值保持未知。
- 任务冻结范围内的素材索引、目录/类型/在线/授权/关键词筛选、离线可见、来源与授权展示；冻结后新入库、离线或范围外素材不可作为候选。
- AI 推荐可拒绝、填写纠正要求、局部重荐并显式采用；候选不会静默覆盖人工决定。
- 单重任务队列的安全暂停、恢复、取消、阶段/进度、队列位置和有依据的 ETA；未知供应商结果不会自动重试。
- 字幕结构/时序/安全区、响度、候选视频、素材指纹、任务来源范围和商业授权的 fail-closed QA。
- 当前候选 15%/50%/85% 封面、外部图片、待补封面、发布标签及标准/可编辑交付。
- 可编辑包只复制真实存在且受信的冻结组件；缺失组件记录 `not_available`，发布清单与文件签名动态生成。
- 数字人与信息流共用发布级 QA 完整性清单；园区路线在预检和创建 API 两个写入口继续返回 `ROUTE_UNAVAILABLE`。

## 3. 自动化验证

下表记录本分支实际执行的最终验证命令、退出状态和测试计数，不使用估算数字。

| 验证项 | 命令 | 结果 |
|---|---|---|
| 双路线集成契约 | `.venv\Scripts\python.exe -m unittest tests.test_two_route_design_closure -v` | 3 项通过 |
| 受影响的数字人/信息流/API 回归 | `.venv\Scripts\python.exe -m unittest tests.test_qa_release_gate tests.test_ad_actions tests.test_workbench_api tests.test_assemble -v` | 95 项通过 |
| Python 全量 | `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q` | 902 项通过，1425.007 秒 |
| TypeScript 测试 | `npm run test:ts` | 91 项通过，176.453 秒 |
| Web/真实浏览器测试 | `npm run test:web` | 90 项通过，90.585 秒 |
| TypeScript 类型检查 | `npx tsc --noEmit` | 退出码 0 |
| Python 编译 | `.venv\Scripts\python.exe -m compileall -q media_catalog workbench scripts` | 退出码 0 |
| 发布包装与双路线集成回归 | `.venv\Scripts\python.exe -m unittest tests.test_release_packaging tests.test_two_route_design_closure -v` | 17 项通过，25.831 秒 |
| 基线干净检出烟测 | 临时 worktree 中复跑迁移、Python、TypeScript、Web 与发布包装命令 | 追加素材索引修复前的基线提交：迁移/集成 38 项、Python 900 项、TypeScript 91 项、Web 90 项、发布包装 14 项及类型检查均通过；临时 worktree 无跟踪改动并已移除 |
| 最终提交干净检出复核 | 从 `d2b974d` 创建临时 worktree，复跑素材索引/API、Web、发布包装/双路线集成及类型检查 | Python 7 项、Web 90 项、发布包装/双路线集成 17 项及类型检查均通过；首次 Web 运行仅在系统临时目录清理时遇到 `EPERM`，改用工作树内受控临时目录后 90 项全部通过；临时 worktree 无跟踪改动并已移除 |

## 4. 明确未完成

- 真人可用性测试和无指导操作测试。
- 普通同事现场验收、真实设备/权限/长路径环境复核。
- 真实现场素材与真实业务任务验收。
- 园区宣传路线的新建、预检、工作区、渲染和发布。
- 外部平台自动上传、Git 标签、GitHub Release 和安装包发布。

以上项目保留为后续门禁；不能用本报告中的自动化结果替代。
