# 剪辑工作台 20 项产品审查修复计划

**目标**：修复审查 F01–F20，保留已有数据、候选与交付历史，每个原失败场景具备回归保护。
**依据**：`docs/reviews/2026-09-20-product-code-review.md`；原复现位于 `runtime/review-20260920/`。
**技术**：Python/SQLite、TypeScript/FFmpeg、浏览器 DOM、PowerShell。
**授权**：用户已明确要求修复全部20项；直接实施本地可回滚修复。不得操作真实素材/账本/凭据、付费API或发布线上版本。保留原 reviews.py 补丁。

每项执行：阅读真实调用链 → 将报告复现固化为会失败的行为测试并确认失败 → 最小实现 → 定向测试 → 交叉审查 → 完整回归。独立模块按文件所有权并行；共用文件由主代理协调。

| 项目 | 修改范围与预期行为 | 核心验收 |
|---|---|---|
| F01 | scanner 合并迁移所有业务引用，冲突安全且失效任务门禁 | 镜头、人工选择、隐私和任务关系不丢；外键检查通过 |
| F02 | scanner 单位置内容变化分离身份 | 已合并两副本分别解析正确内容 |
| F03 | workbench_actions/assemble 按输入代际产生新候选 | 原候选保留，合法返工生成第二版，相同输入重试幂等 |
| F04 | API 创建账号不覆盖旧凭据 | 重复ID/数据库失败后旧凭据仍可用 |
| F05 | scanner/preview 内容变化使派生缓存失效 | 红→蓝替换后刷新出蓝色缩略图 |
| F06 | app 使用 assetRef；后端拒绝无效引用 | 实际目录payload选择后可确认组合 |
| F07 | full_review 观看累计受真实时间预算约束 | 高频小tick不能提前过门禁；正常播放/暂停/回看可用 |
| F08 | publish/workbench_actions 版本化发布尝试 | 被阻断文案改正可重新生成，历史不可覆盖 |
| F09 | app 显式append发布卡片子节点 | 非空七文件包标题状态文件均可见 |
| F10 | ad_actions/render-ad 传递并应用源窗 | 选5–8秒片段的实际产物来自该源窗 |
| F11 | 广告渲染应用确认的字幕预设 | 两个不同预设产生对应ASS样式 |
| F12 | render-mix PNG持续帧与动画时间 | 默认fade中段花字可见 |
| F13 | release 包含src且验证解压执行 | 用真实发布清单构建后依赖解析及字幕生成通过 |
| F14 | MCP TOML只移除目标表 | 数组表、quoted点号键、多行字符串及其他服务保留 |
| F15 | legacy生成在付费前检查文件和query冲突 | 已有结果不创建付费任务；失败有非成功退出 |
| F16 | privacy重复扫描刷新与CLI单片异常隔离 | 扫A、新增B、重扫库正常且人工决定不丢 |
| F17 | 真实试听入口保存正确错误类别 | 音色专属失败禁用，暂时鉴权/网络错误不误禁 |
| F18 | 模型重测结构错误关闭旧可用状态 | 数组/null/异常成员返回稳定错误且不能绑定 |
| F19 | match读audio_analysis.segments文本 | 无语义文件名仍按已保存转写匹配 |
| F20 | dedup按时长采样且返回真实相对位置 | 中后段重复能识别；配置FFmpeg路径有效 |

## 文件所有权

- 素材组：F01/F02/F05/F16/F19/F20；scanner、preview、privacy、cli、match、dedup及各自测试。
- 渲染组：F03/F08/F10/F11/F12/F15；ad_actions、assemble、publish、workbench_actions、render-ad、render-mix、generate-broll及测试。交付模型组接续独立复审并修复生成清单锁竞态和发布复用竞态。
- 交付模型组：F13/F14/F17/F18；发布清单/打包、Codex安装脚本、model_profile_probe、tts_voice_catalog及测试。
- 主代理：F04/F06/F07/F09；full_review、workbench/api、app及对应测试。统一README/手册、跨模块复审和最终回归。

## 集成验收

- [x] 20项失败→通过记录完整，原报告保留为修前事实，新增修复验收表。
- [x] Python当前771项通过记录覆盖完整（765项首轮+117项最终改动复测+44项迁移复测）；TypeScript82/82、Web59/59与tsc通过；实际FFmpeg/浏览器验证通过。
- [x] 发布包离线解压使用检查；不能以源码测试代替发布物验证。
- [x] git diff --check，检查README和相关手册同步需求。
- [x] 独立复审修改范围；新发现只处理修复引入或原目标必需的问题。
- [x] Session `20260920-121245-codex-09ef` 已完成归档并同步项目页与sidecar；本地修复未提交、未发布，不等同于同事现场验收。
