# CUTROOM 20 项审查问题修复验收

基线：`92d1434` / `0.9.0-beta.5`。原始发现保留在 [产品代码审查](./2026-09-20-product-code-review.md)，本文件记录后续修复，不能用原报告的修前结论描述当前工作区。

Session：`20260920-121245-codex-09ef`。以下记录原修复轮次的本地验收，不能作为后续版本的发布证明。发布复核发现广告重新确认包装后仍能发布旧候选的遗漏，已补回归和输入版本校验；本轮完整回归、提交与发布状态以 [Beta.6 发布验收](../validation/2026-09-20-beta6-release.md) 为准。

## 逐项结果

| 编号 | 修复行为 | 回归证据 |
| --- | --- | --- |
| F01 | 素材归并在事务内迁移业务引用；人工决定或任务角色冲突时整笔拒绝，保留双方记录；相关任务重新确认 | `tests/test_catalog_review_fixes.py`：引用、外键、冲突回滚、数字人绑定 |
| F02 | 已归并副本内容变化时，按位置分离新身份；单纯修改时间且内容相同不分裂 | 同上：两份文件分叉和触碰文件 |
| F03 | 输入改变后生成新候选，旧候选保留；相同输入重试校验候选与依赖 | `tests/test_workbench_actions.py`、`tests/test_assemble.py`；真实账本、快照与门禁，编码及QA使用替身 |
| F04 | 创建前完成参数校验并在数据库写锁内检查账号；重复请求不写旧凭据，新建失败只清理本次凭据 | `tests/test_review_api_regressions.py`：重复ID、非法参数、强制数据库失败 |
| F05 | 内容变化失效旧预览及分析，按源签名生成新缓存文件；生成期间源变化拒绝登记 | `tests/test_catalog_review_fixes.py`：实际图片更换与生成竞态 |
| F06 | 下拉框提交实际 `assetRef`；后端在写草稿前拒绝不存在的引用 | `tests/workbench-layout.test.mjs` 使用真实目录和页面函数；API 草稿保持为空 |
| F07 | 审片计时采用跨请求共享的有限容差，不能靠高频 tick 重复领取额度 | `tests/test_full_review.py`：同一时刻突发、暂停后突发、正常播放与回看 |
| F08 | 更正文案后生成独立发布版本，相同请求复用有效包，保留旧包与未知目录保护 | `tests/test_publish_package.py`、`tests/test_workbench_actions.py`：blocked→cleared→重复请求复用 |
| F09 | 发布卡片显式添加 DOM 子节点，完整显示标题、状态和七文件 | `tests/workbench-layout.test.mjs`：真实浏览器非空发布包 |
| F10 | 广告适配器传递选定源窗，渲染器裁切后补转场帧，不取窗外内容 | Python 广告适配测试；TS 真实 FFmpeg 检查蓝色选段及末帧 |
| F11 | 广告计划冻结并应用完整确认字幕预设，包括背板与阴影 | Python 适配测试；TS 检查三套实际 ASS 样式 |
| F12 | PNG 花字持续提供帧与时间戳，淡入后可见 | TS 对实际 FFmpeg 输出中段采样像素 |
| F13 | 发布清单包含 `src`，解压产物可解析业务依赖并生成字幕 | `tests/test_release_packaging.py`：真实清单打包、解压、make-ass、FFmpeg 烧录 |
| F14 | 解析 TOML 并核对编辑前后非目标语义，仅修改 CUTROOM；无法安全处理时原文件不变 | `tests/test_codex_toml.py`、`tests/test_mcp_install_scripts.py`：真实 PowerShell 5.1 安装/卸载 |
| F15 | 旧生成入口在付费前检查整批查询和路径冲突，核验缓存；持久化提交记录，失败返回非零状态 | `tests/render-review-regressions.test.ts`：外部网络替身、创建次数、重试日志及真实 CLI 退出码 |
| F16 | 重扫按检测身份更新隐私项，保留相同项的人工决定；库扫描单片失败继续并返回失败摘要 | `tests/test_catalog_review_fixes.py`：新增隐私项与单片异常 |
| F17 | 明确音色拒绝禁用该音色；鉴权、资源授权和网络故障不误禁，返回与落库一致的错误码 | `tests/test_delivery_model_regressions.py`：真实试听入口到任务音色绑定 |
| F18 | 模型重测先检查 JSON 结构，异常结果关闭旧可用状态并阻止任务绑定 | 同上：规划、视频、TTS 的数组/null/异常成员 |
| F19 | 匹配读取实际 `audio_analysis` 的已完成分段转写文本 | `tests/test_catalog_review_fixes.py`：无语义文件名仍匹配转写关键词 |
| F20 | 按真实时长分布抽帧，使用配置的 FFmpeg，返回实际媒体相对时间 | 同上：中后段重复与非零起始时间码；实际FFmpeg输出验证 |

## 原本地修复轮次验证结果（历史）

所有测试在隔离临时目录、SQLite 和合成媒体中运行；供应商请求和凭据后端使用测试替身。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| TypeScript 全量 `npm run test:ts` | 82 / 82 通过 | `runtime/fixes-20260920/ts-full-final.log` |
| Web 全量 `npm run test:web` | 59 / 59 通过 | `runtime/fixes-20260920/web-full-final.log` |
| TypeScript 类型检查 | 退出码 0 | `runtime/fixes-20260920/typecheck-final.log` |
| 账户/引用/迁移/审片定向 | 23 / 23 通过 | `runtime/fixes-20260920/root-green.log` |
| 工作台 API 完整模块 | 38 / 38 通过 | `runtime/fixes-20260920/api-full.log` |
| 交付/模型专项 | 60 / 60 通过 | `runtime/fixes-20260920/delivery-models-final.log` |
| 审片最终专项（含微小跳转缝隙） | 10 / 10 通过 | `runtime/fixes-20260920/full-review-final.log` |
| 素材专项 | 67 / 67 通过 | `runtime/fixes-20260920/catalog.md` |
| Python 全量首轮 | 765 项：764 通过、1 项旧迁移版本断言失败，后续已修正 | `runtime/fixes-20260920/python-full.log` |
| 最后变动的 8 模块联合复测 | 117 / 117 通过 | `runtime/fixes-20260920/python-final-changes.log` |
| 迁移断言修正后的 schema/数据库/审片复测 | 44 / 44 通过 | `runtime/fixes-20260920/schema-final.log` |
| 当前 Python 测试清单与通过记录核对 | 771 / 771 覆盖，遗漏 0 | `runtime/fixes-20260920/test-coverage.json` |
| 差异检查与独立复审 | 通过；确认问题全部补修并复测 | `runtime/fixes-20260920/diff-check.log` 及各组复审记录 |

Python 的 771 项为当前测试清单去重后的数量，不是把各次重叠运行相加。首轮运行期间新增的 6 项测试及最后变动均纳入联合复测；唯一旧版本断言由 31 更新为 32，并确认重复迁移保留新增字段。核对脚本为 `runtime/fixes-20260920/verify-test-coverage.py`。全部 Python/TS/Web 当前测试共 912 项均有通过证据。

失败到通过的过程记录：`runtime/fixes-20260920/catalog.md`、`render.md`、`delivery-models.md`，以及 `api-red.log`、`compose-red.log`、`full-review-red.log`、`ui-publish-red.log`。沙箱临时目录权限失败与 Windows 文件过滤差异单独记录，不作为产品失败复现。

## 交叉复审补强

独立复审与跨模块检查另外覆盖以下原修复边界：审片区间不能补齐被拒绝的微小跳转空隙；旧生成入口必须取得锁后才读取共享清单；非零起始时间码须换算为媒体相对位置；素材身份归并只延续原本有效的同内容文案门禁；归并后隐私复扫优先保留已完成人工决定的相同命中；发布复用返回前再次锁内核验审批、发布记录与候选；广告复用检查渲染运行依赖。

广告包装方案“已确认直接改回草稿”被现行 SQLite 触发器拒绝，不作为已复现缺陷；将包装验证移到复用之前属于防御性补强，异常状态测试明确使用读取边界替身。

## 原本地修复轮次边界（后续发布见 Beta.6 验收）

- 保留开始前已有的 `media_catalog/reviews.py` 修改。
- 新增迁移 032，升级时由原迁移机制添加审片容差字段；未对真实账本执行迁移。
- 同步 README、同事操作手册和 MCP 操作手册；安装/卸载 TOML 编辑需要项目 Python 3.11 环境。
- 解压发布验证复用本机第三方 Node 依赖，业务相对导入均来自解压目录；未运行真实同事电脑首次安装。
- 本次未读真实凭据、未调用付费服务、未发布线上版本；云端兼容与普通同事首片仍按原现场验收门禁执行。
