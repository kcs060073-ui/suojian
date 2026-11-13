# 剪辑工作台产品代码审查（2026-09-20）

> 本文保留修前审查事实。F01–F20 后续已在本地修复，当前结果与验证边界见 [20 项修复验收](./2026-09-20-product-review-fixes.md)；尚未发布更新下载包。

审查基线：`92d1434`，版本 `0.9.0-beta.5`，同时检查工作区原有 `media_catalog/reviews.py` 未提交补丁。未修改产品代码、真实账本、真实素材或凭据，未触发付费生成。首轮 Session：`20260920-112249-codex-0efc`；补齐审查 Session：`20260920-115606-codex-d9b8`。

**结论：当前不建议作为稳定版本向普通同事推广。** 已有快照、门禁、事务和较多回归测试，但素材合并会丢业务记录，正常返工与发布修正存在断路，部分用户确认没有落实到成片。以下问题按实际影响定级，不包含代码风格建议。

## 审查范围与证据边界

已完整阅读 Git 跟踪的自有源码、测试、SQL、安装/启动脚本及 HTML/CSS：**241/241 个文件，72,486 行，未完整阅读 0**；另读生产配置、manifest、依赖声明和入口文档，合计 **263 个文件**。[完整覆盖清单](./2026-09-20-review-coverage.md)记录文件及行数，机器账本保存 SHA-256 与本轮行范围。已补齐上轮剩余 111 个文件，并补上当时漏计的 HTML/CSS 和中文 BAT 文件。

本报告共 **20 项已确认问题：3 项 P1、17 项 P2**，其中本轮新增 F13–F20 共 8 项。三个优先阻断点为 **F01 素材合并丢记录、F03 正常返工不能生成新版、F13 发布包缺源码导致出片失败**。第三方依赖、自动生成锁文件、媒体与业务数据、全部历史设计文档不在逐行产品代码范围；真实 `.env` 和凭据未读取。

隔离复现使用临时 SQLite、合成文件、内存凭据后端以及本机无头浏览器。使用既有测试夹具的项目注明替代了哪些外部调用。结论不等于真实团队电脑、移动硬盘、云端供应商或付费渲染端到端验收。

P1：应在稳定版推广前修复的数据完整性/关键生产流程问题。P2：明确影响局部功能、质量或门禁可靠性的缺陷。未发现可确认的 P0。

## 已确认问题

### F01 · P1 · 确认重复素材会删除已有镜头、选材与隐私记录

- 位置：`media_catalog/scanner.py:243`，`_merge_assets` 删除重复 asset 的语句；关联表外键级联删除。
- 触发：先在重复素材之一建立镜头卡、任务素材关系、人工选择或隐私确认，再确认两份素材内容相同。
- 原因：只迁移 `analysis_runs` 和 `asset_locations`，随后删除 asset；没有迁移其他业务引用。
- 复现：`scene_cards/task_assets/privacy_flags/selection_decisions` 均由 1 变成 0，画面锁的 `changed_since_lock` 仍为 0。原视频文件没有被删除，但用户的剪辑与审核记录丢失。
- 修复方向：在同一事务内迁移所有依赖关系并处理冲突；受影响任务必须重新计算门禁与快照。不得靠禁用外键掩盖问题。
- 证据：`runtime/review-20260920/repro_catalog_review.py`、`catalog-confirmed.log`。置信度：高，真实扫描与数据库复现。

### F02 · P2 · 归并后的某份文件变化，仍沿用共同素材身份

- 位置：`media_catalog/scanner.py:375`，`_catalog_path` 对已存在路径的内容变化处理。
- 触发：两份相同文件完成去重归并后，原位修改其中一份，再扫描素材库。
- 原因：修改共享 asset 的大小和指纹，没有先把发生变化的位置从原共同身份中拆出。
- 复现：两文件实际大小分别为 27 和 15 字节，数据库仍只有 1 个 asset、2 个位置，两个位置查出的大小均为 15。结果受扫描先后影响。
- 影响：本机副本与移动盘副本分叉后可能解析到不同内容；既有选择、摘要和来源身份失去可靠性。
- 修复方向：按位置核验内容代际，分离发生变化的副本，保留未改变副本身份，并失效相关分析与任务依赖。
- 证据：同 F01。置信度：高，使用模拟文件字节验证身份算法，不依赖视频解码。

### F03 · P1 · 数字人首版完成后，镜头或包装返工无法产生新候选

- 位置：`media_catalog/workbench_actions.py:599` 至 `_completed_action`；另见 `media_catalog/assemble.py` 对已有候选的短路分支。
- 触发：首版渲染/QA 后，用户修改镜头或换包装，重新确认并要求重渲染。
- 原因：每个任务固定只有 `render-action.json`；有文件就复用，输入摘要变化则抛 `ACTION_STATE_STALE`。没有把输入变化作为创建新候选版本的条件；仅去掉该文件也仍会遇到历史候选复用分支。
- 影响：产品允许返工、会使下游阶段失效，但执行层无法完成承诺的重跑。
- 修复方向：按当前输入代际管理动作记录与候选，旧候选仅作历史；相同输入重试幂等，新输入创建新版本并重新走 QA/审片。
- 证据：`runtime/review-20260920/repro_render_rework.py`。置信度：高。首版使用既有测试的渲染替身；真实 SQLite 输入变更后，第二次调用报 `ACTION_STATE_STALE`，且 `assemble_calls=0`。

### F04 · P2 · 重复创建供应商账号会清掉已有凭据

- 位置：`workbench/api.py:572`，及创建失败后的 `delete_provider_secret` 清理分支。
- 触发：对已有 `accountId` 再次提交账号创建请求，包括客户端重复提交相同请求。
- 原因：数据库唯一性检查之前，先写入该账号固定引用的凭据；随后 INSERT 失败，清理逻辑又删除同一引用。
- 复现：第二次 POST 抛 `IntegrityError`，凭据数量从 1 变 0，数据库原账号行仍为 1。
- 影响：原本可工作的模型账号突然不可用，需要用户重新录入密钥；失败请求产生破坏性副作用。
- 修复方向：先校验请求及唯一性，使用不会覆盖原凭据的暂存引用，或实现可回滚的原值恢复与幂等创建。
- 证据：`runtime/review-20260920/security-repro.py`；使用真实凭据适配函数与纯内存后端，未操作 Windows 真实凭据。置信度：高。

### F05 · P2 · 替换源文件后仍向用户展示旧预览

- 位置：`media_catalog/preview.py:483`，`_missing_kinds`；关联 `scanner.py` 的内容变更失效处理。
- 触发：已生成预览的文件被原位替换，再扫描和技术探测，重新生成预览。
- 原因：是否需要预览只看 `preview_cache` 是否已有该 asset/kind，不校验源内容身份；扫描改变内容后没有清理该缓存关系。
- 复现：源图片由红色变为蓝色，预览任务 `discovered=0/completed=0`，缩略图仍为红色。
- 影响：用户按旧画面选材，渲染实际读取新内容。代理/音频等缓存也需要检查相同失效边界。
- 修复方向：缓存键绑定源内容代际；内容变化时失效全部相关派生产物和必要的人工作业结论。
- 证据：同 F01，真实 PNG 与缩略图像素验证。置信度：高。

### F06 · P2 · 自选包装提交了字符串 `undefined`

- 位置：`web-ui/public/app.mjs:830`；接口字段在 `workbench/api.py:1367`。
- 触发：在包装组合编辑器中选择字幕/BGM等素材并点“更新当前组合”。
- 原因：API 输出 `assetRef`，页面却读取 `asset.id` 作为 `<option>` 的 value。
- 复现：真实浏览器中选中值和请求 `assetRef` 均为字符串 `undefined`，后续确认报 `PACKAGING_MANIFEST_UNKNOWN: subtitle/undefined`。
- 影响：可选列表看起来正常，但自选组合不能正确确认。
- 修复方向：统一数据契约，增加使用实际 API payload 的 DOM 交互测试，并在更新接口提前验证素材引用。
- 证据：`runtime/review-20260920/security-ui-repro.mjs`。置信度：高，真实 API 目录数据与原页面函数复现。

### F07 · P2 · 完整观看门禁的时间容差可以重复累积

- 位置：`media_catalog/full_review.py:182` 至 `190`。
- 触发：在已认证观看会话中，快速、连续提交每次增加 0.4 秒的播放进度。
- 原因：每个 tick 无条件加 0.5 秒容差，没有按会话累计真实耗时限制覆盖总量。
- 复现：使用真实服务端时钟，250 次进度提交耗时约 5.93 秒，即可将标记为 100 秒的隔离候选片记为 100% 覆盖，并取得 `approved`。未使用模拟时钟加速。
- 影响：声称“服务端验证完整观看”的门禁并不能建立该事实。此缺陷需要现有授权会话，不是未认证远程入侵。
- 修复方向：对累计已接受媒体时间施加真实累计播放耗时预算，容差只作为有限总预算；保留回看区间合并和拖动防跳过逻辑。
- 证据：`runtime/review-20260920/repro_review_publish.py`、`repro-review-publish.log`。置信度：高；候选媒体使用测试字节，验证对象是门禁计算与审批入库。

### F08 · P2 · 发布文案被阻断后无法修正重试

- 位置：`media_catalog/publish.py:258` 至 `262`；另见 `media_catalog/workbench_actions.py:720` 的固定动作记录。
- 触发：首次发布文案命中检查，被标记 `blocked`；用户改成正常文案后再次生成。
- 原因：被阻断的发布包仍占用按 task_id 固定的目录；后续一律拒绝目录存在。工作台的固定 `publish-action.json` 也拒绝变化后的输入摘要。
- 复现：首次标题 `100%` 得到 `blocked`，第二次标题改为 `normal` 得到 `FileExistsError`。
- 影响：用户完成视频和审片后，因为一次文案修正被迫进入人工清理或重建任务。
- 修复方向：发布尝试采用版本目录；相同内容允许幂等重试，不同内容重新检查并保留历史，禁止覆盖已正式交付版本。
- 证据：同 F07。封面提取被替换为隔离写入，其他发布与账本逻辑真实执行。置信度：高。

### F09 · P2 · 发布包列表把 DOM 元素当文字，文件清单不可见

- 位置：`web-ui/public/app.mjs:1113` 及后续 `package-files` 的创建调用；`element` 定义在 `144`。
- 原因：`element(tag, className, text)` 只支持文本第三参，却被当作支持可变子节点的函数调用。
- 复现：真实浏览器显示 `[object HTMLElement]` / `[object HTMLSpanElement]`，文件区域子元素数为 0。
- 影响：已成功生成的发布包无法在该列表正常显示标题、状态和文件明细，削弱用户确认交付物的能力。
- 修复方向：显式 append 子节点，或统一 helper 契约；覆盖非空发布包状态的真实 DOM 测试。
- 证据：同 F06。置信度：高。

### F10 · P2 · 广告渲染丢弃已经锁定的源素材入点/出点

- 位置：`media_catalog/ad_actions.py:799` 至 `803`，以及 `scripts/render-ad.ts:183`。
- 触发：广告镜头选择长素材中的指定时间窗，锁定画面后生成成片。
- 原因：适配器只传素材路径和 TTS 时间，不传 `enter_tc/exit_tc/segment_id`；渲染器从文件开头执行 `trim=duration`。
- 复现：锁定源窗为 5–8 秒，实际传给渲染器的 segment 无任何源窗字段；因此源画面仍从第 0 秒开始。QA 不验证选中的是哪段画面。
- 修复方向：把选定源窗贯穿广告渲染契约，并约束转场余量，增加带可辨认时间标记素材的端到端检查。
- 证据：`runtime/review-20260920/repro_ad_contract.py`、`ad-contract-result.json`。置信度：高，真实适配器与锁定状态；编码和 QA 使用现有测试替身，没有声称执行了完整浏览器选材流程。

### F11 · P2 · 广告字幕始终使用固定样式，忽略包装确认

- 位置：`scripts/render-ad.ts:110` 至 `112`；上游 `media_catalog/ad_actions.py:805`、`848` 未传字幕预设。
- 触发：广告任务确认 `sub-heavy-orange` 或其他字幕预设后渲染。
- 原因：广告 ASS 只读取静态 `AD_SUBTITLE`，不读取已确认方案。包装快照记录了选择，实际产物没有应用。
- 复现：适配器结果记录 `sub-heavy-orange`（思源粗宋/橘黄），渲染入参只有 plan/output；ASS 生成固定使用阿里妈妈数黑/白字及固定字号边距。
- 修复方向：接通已确认预设到广告 ASS；若暂不支持，应收窄页面可选项和确认校验，不能允许用户确认无效选择。
- 证据：同 F10，加真实 TS 样式生成代码和 manifest 对照。置信度：高；未跑完整 1080p 编码。

### F12 · P2 · render-mix 默认淡入花字全程透明

- 位置：`scripts/render-mix.ts:343`，关联 `348`、`359`。
- 触发范围：通过 render-mix CLI/MixPlan 使用 `fancyTexts`，保持默认 fade，或使用其他同样带 fade 的入场效果。当前工作台尚未转译该包装项，不扩大为工作台全部成片问题。
- 原因：花字 PNG 是单帧输入，唯一的 t=0 帧被淡入滤镜设为全透明；overlay 后续一直重复这张透明帧。
- 复现：同一 1 秒黑底及红色 PNG，0.2–0.8 秒展示窗，在 0.5 秒采样：`entrance=none` 像素为 `[252,0,0]`，`fade` 为 `[0,0,0]`；两次 FFmpeg 均成功退出。
- 修复方向：为静态图生成持续帧及正确时间戳再应用动画，并以帧像素/实际图像验证效果。
- 证据：`runtime/review-20260920/repro_render.py`、`render-fixtures/result.json`。置信度：高，离线 FFmpeg 实测。

### F13 · P1 · 发布 ZIP 漏掉 src，安装完成后仍无法生成字幕或运行依赖它的渲染脚本

- 位置：`release/release-files.json:16–25`，关联 `scripts/build_release.py:154–170`。
- 触发：按当前发布清单构建 ZIP，普通同事解压安装后制作数字人视频。
- 原因：目录白名单没有 `src`，构建器也不补复制；`make-ass.ts`、`render-mix.ts` 等依赖 `../src`，Python 字幕预检直接要求 `src/subtitle-ass.ts` 与 `src/subtitle-alignment.ts`。安装器仅装环境依赖，不会补回项目源码。
- 复现：使用当前清单、真实 build_release 和隔离最小源树；源树含字幕源码，产物含 make-ass.ts，但 ZIP 的 `src/` 条目为 0。解压后调用真实字幕 `_runtime_signatures`，直接得到 `SUBTITLE_SOURCE_MISSING`。
- 影响：开发目录的单元测试成功，仍会交付无法完成核心出片流程的安装包。未据此声称已下载并核验线上既存 ZIP；结论针对当前构建代码与清单。
- 修复方向：补齐 src，并从解压后的发布包运行离线字幕生成、模块解析及最小渲染冒烟；测试应使用真实发布清单，而非仅验证合成目录。
- 证据：`runtime/review-20260920/repro-delivery-release.py`、`delivery-repro/release-result.json`。置信度高。仅替换临时目录分配以避开沙箱 ACL，未替换复制、ZIP 或依赖核验逻辑；未运行真实安装器。

### F14 · P2 · 注册或卸载剪辑工作台 MCP 会误删其他合法 TOML 设置

- 精确位置：`scripts/install-codex-mcp.ps1:31–39`；相同代码在 `scripts/uninstall-codex-mcp.ps1:24–32`。最小评论范围为安装器 31–33 行。
- 触发：已有 `[mcp_servers.cutroom]` 后跟合法 `[[skills.config]]` 等数组表；或者另有 `[mcp_servers."cutroom.extra"]` 这样的独立服务名。
- 原因：删除函数只把单层 `[table]` 当段落边界，无法识别 `[[array-of-tables]]`，因此删除状态持续到下一张普通表；还先剥除所有引号再按 `mcp_servers.cutroom.` 前缀判断，使独立的带点号服务名被错误当成剪辑工作台子表。
- 影响：用户执行正常重注册或卸载，其他技能设置和其他 MCP 服务被静默删除；原子文件替换无法补救语义上的误删。脚本成功提示仍声称只修改剪辑工作台，且临时备份随后被删除。
- 隔离复现：`repro-delivery-toml.ps1` 从真实脚本 AST 取出原 `Remove-CutroomTomlSection` 函数执行，只向 `runtime/review-20260920/delivery-repro/` 写合成输入/输出；没有执行安装器主体，也没有访问用户配置。Python `tomllib` 确認输入中的 `skills.config` 和 `mcp_servers['cutroom.extra']` 均为合法独立字段。
- 结果：两份脚本均 `removedSkills=true`、`removedUnrelatedQuotedServer=true`、`preservedOther=true`。输出仅余 `model` 和普通 `mcp_servers.other`。
- 证据：`delivery-repro/before.toml`、两份 `*.after.toml`、`delivery-repro/toml-result.json`。置信度：高，实际 PowerShell 原函数执行。
- 修复方向：用理解 TOML 键和表结构的方式仅移除目标服务器，并在写回前验证解析后的非目标配置完全一致；测试覆盖数组表、带点号的 quoted key 及多行字符串边界。

### F15 · P2 · 普通 Seedance CLI 重跑先创建付费任务，再因已有文件而丢弃新结果

- 精确位置：`scripts/generate-broll.ts:233–238`；下载排他写入位于 `106` 行，异常仅警告位于 `242–246` 行。
- 触发：通过保留的 `<name>` legacy 模式再次处理仍含已生成 query 的 pending 文件，或不同 query 经 slug 归一化得到相同文件名；目标 `public/library/seed_<query>.mp4` 已存在。
- 原因：legacy 循环在供应商 `createTask` 和 `pollTask` 之后才决定目标路径，没有在创建前检查输出/manifest。共享 download 现在使用 `fs.promises.open(destination, "wx")` 拒绝已有文件，因而新云端结果无法保存；catch 吞掉失败后打印完成并正常返回。
- 影响：可避免的重复视频生成费用已经发生，但本地仍是旧素材，新任务 ID 也未留存至 legacy manifest；调用方不能从正常退出辨认全部生成失败。影响范围为现存 legacy CLI，不扩大为有严格预检的显式三请求模式或工作台生产作业。
- 隔离复现：`repro-delivery-legacy.cjs` 在内存 VM 中转译真实 `generate-broll.ts`，仅额外导出原 `runLegacyMode` 供调用，将 `__dirname` 指向复现目录。`load-env` 被替换为空，provider HTTP 全部为离线 mock（创建、成功轮询、下载字节）；实际文件系统排他打开保留原实现，未读取真实密钥或访问网络。
- 结果：已有文件场景 `createCalls=1`、`totalProviderCalls=3`；随后真实 `EEXIST`，`originalUnchanged=true`、`returnedSuccessfully=true`，日志仍有“完成”。
- 证据：`delivery-repro/legacy-result.json` 及 `delivery-repro/legacy/` 合成文件。置信度：高；证明本地控制流与不可保存输出，未声称实际扣费。
- 修复方向：付费 create 前核验缓存/文件冲突与 query 身份；有效缓存跳过，确需新版本时分配唯一输出并持久化 task ID。任何未完成项需返回明确失败状态。

### F16 · P2 · 已扫描过的隐私命中会让下一次批量扫描崩溃，后续新素材无法得到隐私标记

- 精确位置：`media_catalog/privacy.py:183–189`，每次扫描 INSERT 固定的 `asset_id:000` 等主键；`media_catalog/cli.py:1292`，异常处理引用未导入的 `subprocess.CalledProcessError`。
- 正常触发：素材 A 已扫描并产生一个联系方式/人脸/二维码标记；新增素材 B 后再次运行 `privacy --json scan --library <id>`。批量查询包含所有探测完成的视频，也包括 A。
- 影响链：A 第二次 `_persist` 违反 `privacy_flags.flag_id` 唯一约束；异常列表求值又触发 `NameError: name 'subprocess' is not defined`。批任务失败并停止，B 不再扫描。重试会再次遇到相同旧标记；输出也不再满足主 CLI 稳定 JSON 错误协议。即使只修 import，旧素材的新增隐私结果仍不能刷新，因此应一起修复可重复扫描语义和异常隔离。
- 隔离复现结果：第一次 `scan_privacy` 返回 1 个 contact 标记；第二次真实调用抛 `IntegrityError: UNIQUE constraint failed: privacy_flags.flag_id`；随后真实 `cli.main` 批扫描抛 NameError，`jobs.status='failed'`，标记表只有 asset-a，asset-b 没有标记。
- 证据：`repro_integration_privacy.py`、`integration-privacy-result.json`、`integration-privacy-fixture/catalog.sqlite`。真实 SQLite、标记检测、标记持久化和 CLI 作业代码；使用 example.invalid 邮箱及合成素材，仅替换 FFmpeg 路径发现，fixture 不提供时长故不抽帧；不涉及真实隐私信息或外网。
- 修复方向：将检测结果按源内容代际及稳定命中身份进行可重复刷新，并保留有效人工决定；补 import 或移除无用异常类型；单片失败计入失败摘要并继续其他素材，避免报成功却漏扫。增加“扫描 A → 新增 B → 再扫描库”的回归。
- 置信度：高。既有报告 F01–F12 未包含本问题。

### F17 · P2 · 音色试听被供应商拒绝后仍然可被任务选用

- 位置：`media_catalog/model_profile_probe.py:275-277`、`360-371`，关联 `media_catalog/tts_voice_catalog.py:274-284`。
- 触发：音色目录内某音色在试听时返回业务失败（例如合法 JSON `{"code":3001,"message":"voice not supported"}`，无音频）。
- 原因：真实试听适配器抛出默认 `MODEL_PROBE_FAILED` 并将它写入目录；禁用音色的唯一条件却是 `TTS_VOICE_TEST_FAILED`。后一个代码仅在 API 对外错误包装时产生，没有写回目录。新版 V3 的供应商拒绝同样使用其他代码。
- 影响：界面已告知试听失败，但该音色仍 `selectable=true`，`resolve_task_tts_voice` 仍接受它，继续创建任务后在实际配音处再次失败。不能仅凭测试中直接调用 `mark_voice_test_result(..., error_code='TTS_VOICE_TEST_FAILED')` 的成功断言认为此流程已覆盖。
- 隔离复现：`round2-models-repro.py`，内存 SQLite + 假 token + 仅替换 `_request_json` 网络边界，真实试听/状态保存/任务选择函数。输出 `testStatus=unavailable`、`lastTestErrorCode=MODEL_PROBE_FAILED`、`selectable=true`，并返回 `task_voice_still_accepted`。没有生成或访问真实媒体。
- 修复方向：在真实适配器区分音色特定拒绝与鉴权/网络错误，前者写入可触发禁用的统一错误码；后者保留可选状态。回归覆盖真实试听入口到任务选择的链路。

### F18 · P2 · 模型重测收到异常 JSON 结构后保留旧“可用”结果

- 位置：`media_catalog/model_profile_probe.py:456-457`、`492-500`（视频目录 `482-487` 同类）。
- 触发：一个24小时内已检测成功的模型重测时，上游或兼容网关返回 HTTP 200、合法 JSON 数组/标量而不是协议对象。例如 `[]`。
- 原因：解码后直接 `.get()`，抛出的 `AttributeError` 不在异常分支中，未调用 `mark_profile_check(available=False)`。目录 `data=null` 等还可触发未捕获 `TypeError`。
- 影响：重测失败后仍保留 `availabilityStatus=available`、旧检测时间、空错误码；新任务仍通过 `resolve_task_profile_snapshots`，实际协议已经不兼容却未阻断。此处不是主张任意异常响应都能攻击系统，而是明确的状态一致性缺陷。
- 隔离复现：同一脚本与 `round2-models-repro.json`。创建假配置并标记成功，仅将传输结果替换为 `('application/json', b'[]')`，得到 `AttributeError`；随后 `failed_profile_still_bindable=true`、`lastCheckedAtUnchanged=true`。
- 修复方向：先校验对象、集合、成员形状，再读取字段；统一将协议结构失败存为不可用并返回安全、可操作的错误。测试覆盖已可用配置发生失败重测，而不只新配置初次测试。

### F19 · P2 · 旧镜头匹配命令读不到真实保存的音频转写

- 位置：`media_catalog/match.py:95–101`；实际写入方 `media_catalog/audio_analysis.py:178–184`、`213–218`。
- 触发：通过 `analyze-audio` 分析普通相机文件名素材，再通过 `match shots` 按口播内容找镜头，或由脚本草稿适配器汇总素材文本。
- 原因：写入方以 `stage='audio_analysis'` 保存 `segments[].text`，读取方却查 `stage='transcript'` 并取顶层 `text`，两层契约均不匹配。
- 复现：真实 `_persist` 将“新疆数据中心”写入内存 SQLite；`candidate_text_for_asset` 只返回 `clip001`，对应文案的匹配分为 0。不是识别模型精度问题，转写已在数据库中。
- 影响范围：P3 CLI 镜头匹配及脚本适配器；不扩大为工作台当前独立的自动配片链路。
- 修复方向：统一读取 audio_analysis 的有效段文本并校验分析状态；用“实际分析结果落库→按转写内容匹配”的契约测试覆盖，避免只用有语义的文件名测试。
- 证据：`runtime/review-20260920/round2-root-repro.py`、`round2-root-repro.log`。置信度高；真实持久化和读取函数，合成文本，无真实语音识别调用。

### F20 · P2 · 重复镜头检测的抽帧集中在片头，漏掉后续重复画面

- 位置：`media_catalog/dedup.py:65–68`，关联 `110–111`。
- 触发：使用 `dedup scan` 检查有较长时长的视频，重复内容在片头以后。
- 原因：FFmpeg 表达式中的两个 `n` 都是当前帧编号，`floor(n/12)` 不是总帧数间隔。在编号 12–23 时除数恒为 1，于是连续选择这 12 帧并达到输出上限。进程成功退出，不进入后备抽帧分支。
- 复现：10 秒、30 fps 合成视频，原表达式成功退出，前 12 个选中时间点全部为 0.400–0.767 秒；showinfo 还记录了编码输出停止前的下一帧 0.800 秒。没有覆盖视频主体，却在结果中按样本索引把 `t_rel` 标成 0–1。
- 影响：后半段的相同镜头无法被发现，返回的相对位置也不对应实际采样位置。范围仅为 P3 重复检测 CLI。
- 修复方向：先取真实时长/帧数，按固定目标时间点采样并保存真实 PTS；用仅在中后段共享画面的两段视频验收。
- 证据：同 F19；真实本机 FFmpeg、合成 testsrc、未调用模型。置信度高。

## 验证记录

- TypeScript：`npm run test:ts`，73/73 通过。
- Web/UI：`npm run test:web`，58/58 通过，包含本机无头浏览器测试。
- 类型检查：`tsc --noEmit`，退出码 0。
- Python：`.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'`，722/722 通过（954.094 秒）；最终退出码 0。三套回归共 853 项全部通过。
- 本轮新增 8 项问题全部有离线最小复现；发布包用真实打包与解压依赖检查，MCP 配置用真实 PowerShell 原函数，隐私扫描用真实隔离 SQLite，供应商调用使用网络边界替身。原产品代码未变，沿用同一工作区已通过的 853 项回归，不重复运行整套。
- 最小复现与日志集中在项目 `runtime/review-20260920/`，报告中的每项已注明真实执行和替身范围。
- 初次沙箱执行出现 Node `spawn EPERM`、Python 临时目录 `PermissionError`。已用批准后的本机权限重跑；沙箱失败不计入产品缺陷。
- AgentGateway 的四个人工门禁动作均被白名单拒绝；未确认 MCP 直接批准门禁的漏洞。输出哈希绑定、审批与发布时二次核验、原子暂存等防护确实存在。
- 两个怀疑项经实验排除：同一音频标签用于 sidechain/amix 的本机 FFmpeg 测试成功；20 段离线 MP3 配音未出现所怀疑的容器时长累积漂移。不把这些推测列为缺陷。
- 工作区原有 reviews.py 补丁允许 `assemble-ad-v1` 进入审片，改善了 HEAD 中广告候选不被接受的问题。本次新发现不重复计算该已在工作区修正的点；测试结果适用于当前工作区，不直接等同于已发布 ZIP。

## 修复与验收顺序

1. 先修 F13 的发布包缺失并从解压产物验证出片；同时修 F01 与 F03：保护已有工作，再打通“首版→驳回→编辑→重新确认→新版→审片”。以包含真实业务关联的重复素材测试和第二版成片测试验收。
2. 修素材身份/缓存（F02、F05）、凭据失败回滚（F04）；验证原有数据和账号在失败操作后保持可用。
3. 接通前后端与渲染契约（F06、F09–F12），用实际 API payload、页面操作和产物检查验收，不能只断言字符串存在或进程退出成功。
4. 修完整观看累计时间与发布修正重试（F07、F08），验证既不能跳过门禁，也能在合法修改后继续完成任务。

5. 修 F14 安装配置误删、F15 付费调用前缺少输出检查、F16 重复隐私扫描崩溃，补齐失败重试与数据保全。
6. 修 F17–F18 模型检测状态、F19–F20 旧 CLI 的数据契约和抽帧逻辑；保留与工作台主链的影响范围区分。

本次未实施修复。源码阅读与本轮覆盖核验已完成；修复后应重跑对应最小复现与受影响回归，再由普通同事完成一条含返工的真实任务。真实电脑安装升级、云端供应商兼容与长时间并发仍需动态验收，不再存在本报告范围内的未读产品代码。
