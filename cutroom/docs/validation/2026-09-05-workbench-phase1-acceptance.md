# CUTROOM Phase 1 真实纵切验收报告

状态：**开发验证通过，Checkpoint B 尚未通过**。更新日期：2026-09-08。

尚未通过的原因不是代码测试失败，而是两项真实流程尚未完成：新建任务的首次外部 AI 分镜请求因验收进程接口配置错误收到 404，第二次请求仍待重新授权；`p2-xinjiang-v2` 的审片漏记交互已修复，当前候选仍待用户重新从头完整播放并提交与本次观看会话绑定的审片批准。在这两项完成前，不声明 Phase 1 完成。

## 验收环境与数据边界

- 分支：`codex/colleague-workbench-phase1`。
- Task 15 开始基线：`4cb103af03b3fafce5b28bece4c41d4c99ddd81d`；本报告对应其后的待提交工作树。
- 临时验收入口：`http://127.0.0.1:8766/`；正式固定入口仍为 `http://127.0.0.1:8765/`。
- 只读源库：`runtime/media-catalog/p2-v2-acceptance.sqlite`。
- 隔离副本：`runtime/workbench/acceptance/p2-xinjiang-v2.sqlite`。
- 源库 SHA-256：`C4EAE867E787A7C3EF6EA4155342316A9A1759484E9FD98D0D3B66DF88AE1D69`；复制后及本轮复核时一致。
- `prepare_workbench_acceptance.py` 使用 SQLite backup API；目标已存在默认拒绝，`--replace` 会先备份旧副本，源库按只读 URI 打开并在安装前后复核哈希。
- 原型基准 SHA-256：`6F7A7977C7696DECF57F190FA7AF5DD40E3674FA4987A5BEB3486AAD4884721A`。

验收副本之外的原始素材、源库和历史快照未覆盖。启动密钥、Cookie、API Key 和凭据引用不写入本报告。

## 功能与数据结果

### 已验证

- 正式页面、看板和列表读取同一 SQLite 任务事实，十二步顺序固定；园区宣传片和信息流广告混剪诚实禁用，付费视频生成路由不存在。
- 真实任务 `p2-xinjiang-v2` 的当前可信候选由 `assemble-v1` 生成，绑定 mix 快照 `snap-1a832ee1f7c0`、QA 快照 `snap-bc7f5180dd82` 和 QA 报告 `qa-142edf9685b9`。
- 候选成片为 `E:\我的AI项目\视频自动化剪辑\out\p2-xinjiang-v2.mp4`，大小 42,056,052 字节，时长 69.88 秒，SHA-256 为 `569008172E89CAE3C7D8008D4F2F13A77D70750E2798A83991AFD9D6D633EDA6`。浏览器实际加载成功，`readyState=4`、无媒体错误。
- 候选成片只通过已登录的 `/api/tasks/<task>/review-media` 提供；服务端逐次核对当前 QA 候选及文件签名，支持受控 Range，拒绝变化后的候选和静态路径逃逸。
- 当前真实门禁：文案锁定、画面锁定、包装确认、自动 QA 为真；人工完整审片为假。因此工作流停在 `full_review / awaiting_user`，不会被 2026-09-04 的无观看会话旧审批或旧发布记录放行。
- 历史发布包目录包含且仅按固定顺序展示七个要求文件：`video-final.mp4`、三张封面、`publish-copy.md`、`publish-check.json`、`qa-report.md`；文件完整性检查为 7/7。
- 从真实页面新建了数字人任务 `task_d0e345b528d44f60424fc8d504e7ea03`，完成真实输入、主素材和通用补充目录绑定、预检、文案锁定与 faster-whisper 转写。转写作业 `job-e9b5166103b747bdb34ea1a7fdb62ee1` 已完成，页面正确进入 AI 分镜推荐阶段。
- 修复了两项真实链路缺陷：Windows 文本换行转换导致的锁定文案摘要不一致；ATR 严格 UTF-8 解码下 Python 子进程中文输出导致的启动失败。两项均先增加失败测试，再修改实现。
- 门禁绕过、未登录媒体读取、非法 Range、客户端中断、路径逃逸、候选文件变化和凭据字段泄露均有自动化反例覆盖。

### 尚待完成

- 外部 AI 分镜：用户已明确授权把页面新建任务 `task_d0e345b528d44f60424fc8d504e7ea03` 的锁定文案和可用素材摘要发送到当前配置的火山方舟 DeepSeek 通用文本模型，并接受可能费用。作业 `job-991429b2df6f4d2bab83a07977b084ce` 发出后被供应商以 404 拒绝，没有生成候选；根因是隔离工作树没有 `.env`，runner 回落到默认 `/api/v3`，而主项目为 `deepseek-v4-pro + /api/coding/v3`。生成式步骤不自动重试，修正验收进程配置后仍需再次明确批准新请求。
- 人工完整审片：用户反馈已完成播放，但当时 `review_watch_sessions` 没有 `p2-xinjiang-v2` 的新会话，旧批准的 `watch_session_id` 为空，因此不能追认。本次实际试用暴露“开始完整审片”和“播放”分成两个动作、不符合零培训目标；已按 TDD 改为首次播放先自动建立唯一观看会话，成功后才继续播放，手动按钮仍作备用。目标前端测试 25/25 通过，8766 服务已加载新代码；仍需用户重新完整播放。自动化不得代看或代点。
- 因以上两项未完成，新建任务还没有走完拒绝/替换镜头、排序及 undo/redo、画面锁、包装自选、真实渲染、QA、审片和新发布包；Checkpoint B 继续保持未通过。

## 自动化结果

| 范围 | 命令 | 结果 | 用时 |
| --- | --- | --- | --- |
| Python 全量 | `.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"` | 535/535 通过 | 210.325 秒 |
| TypeScript 全量 | `npm run test:ts` | 65/65 通过 | 23.283 秒 |
| Web 前端 | `npm run test:web` | 23/23 通过 | 0.326 秒 |

`smoke_workbench.py` 已在独立的 8767 临时实例实际运行；当前只报告 `human_reviewed` 和 `full_watch` 两项未满足，符合门禁前态。最终冒烟将在人工审片后重新启动验收服务、交换一次性本机会话并执行，成功输出不记录启动密钥。

## 原型一致性结果

Task 11–13 的静态页面和交互状态已按 1536×864、100% 缩放完成 V4 成对检查。Task 15 新增的真实媒体播放保留既有审片布局，并以受控 `<video>` 替换“等待安全预览服务”的占位状态；实际截图为 `runtime/workbench/acceptance/fidelity/task15-review-pending-real-1536x864.png` 和 `task15-review-check-1536x864.png`。

当前原型一致性结论为：**既有页面通过，真实媒体接入通过，完整观看后的状态与新发布包状态待 Checkpoint B 最终复验**。详细证据见 [CUTROOM V4 原型一致性矩阵](./2026-09-06-cutroom-v4-fidelity-matrix.md)。自动测试通过不替代该人工视觉和播放验收。

## Checkpoint B 判定

目前判定：**未通过，等待用户动作和外部调用授权**。

完成顺序：

1. 用户完整观看并批准 `p2-xinjiang-v2` 当前候选；
2. 用户明确授权本次通用文本模型调用后，让新任务继续完成真实分镜及后续纵切；
3. 重新运行最终冒烟，确认新观看、新批准和七文件发布包；
4. 复验真实播放完成态及发布页，更新本报告和一致性矩阵后再提交 Task 15。
