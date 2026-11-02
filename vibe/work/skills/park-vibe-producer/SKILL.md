---
name: park-vibe-producer
description: Produce park Vibe videos from scripts and footage using a local MotionGit template library with authorized private originals, content-based motion and audio choices, and online illustration or animation sourcing. Use for park Vibe production or MotionGit local-library maintenance, preserving the user's selected style and versions.
---

# 园区 Vibe 制作

工作台准备任务；用户在有本机执行能力的对话明确启动后再制作。脚本、素材、画幅和指定模板是本次输入，不能用模板示例替代真实园区事实。

## 已保存任务的对话交接

用户明确启动 `VYYYYMMDD-xxxxxxxx` 任务时，先在溯剪工作目录用 `python -B -m workbench.production show V实际编号` 读取当前输入、修订和哈希。保存需求不代表授权开始制作。运行服务默认 `http://127.0.0.1:8796`，真实输入位置以 API 返回为准；完整字段见工作台 `workbench/DIRECTOR.md` 的“简洁工作台的制作任务”。

执行中用 `progress` 回写真实阶段；使用当前 `expectedRevision`、`inputHash`、`executor: dialogue`，遇到 409 重新读取输入。按任务已选画幅、素材、参考、模板包哈希和变化种子制作；若关闭公开素材检索，不外搜补画面。不能用模板库更新覆盖任务固定的包。

实际交付 MP4 放入运行模块 `outputs` 后，用 `python -B -m workbench.production deliver V实际编号 --file work/交付.json` 登记审看版本。声明 `finished-cut`、`native-project` 或 `shot-timeline` 须符合真实编辑能力；不要拿旧镜头工程关联新场景。保留输入修订、源工程和旧版本。用户的一句话修改及“换一套搭配”先保存新修订，明确启动后再制作。

## 本地模板与更新

本 skill 的 `assets/motiongit/library-index.json` 保存完整官方目录、已下载包与状态；`assets/motiongit/catalogue.html` 是本地预览清单。维护命令：

```text
python scripts/sync_motiongit.py
```

制作前若本次尚未检查，可同步一次；无网络时继续使用已经落地的包并说明检查时间。`--offline` 只重建本地清单，`--check-only` 不下载新包，`--no-images` 跳过封面。

用户在 2026-10-22 最新明确授权 **允许下载并本地保存带账户 Key 的个人包**，覆盖先前的仅公开包限制。保留完整原包，用 `scripts/import_personal_motiongit.py --authorized-personal` 从 Downloads 校验并导入终端 `work/secrets/motiongit-personal` 专用库；`assets/motiongit/personal-availability.json` 只保存脱敏目录。不要显示、提取或转发 Key，不把个人包放进网页、公开输出或分享压缩包；个人包更新与公开同步分开。

仅官方下载状态为 `direct-package` 的公开 URL 自动下载。保留历史包，以内容哈希标识；当前任务固定选中包的哈希。未开放或未实际导入的项目不可宣称已下载。个人包通过官网当前登录态准备下载，遵守服务排队；登录失效时请用户接管，不抓取凭据。已下载只证明本地存在与结构检查，不证明渲染可用；选定后先读包内公开 manifest/guide/README、核实依赖与素材合同，再做实际预览。

同步只下载资料，不执行新下载的代码，不更新已有工程来源，不改溯剪公共服务。不要为让状态变成成功而改写包、清理未完成操作或绕过权限。

## 自动搭配与联网补画面

所有园区 Vibe 剪辑遵守[口播、对象与动画联动原则](references/content-linked-motion.md)：按完整句意设计可见主体及动作，用真实配音时间串联，避免通篇实景卡片、堆字或文字与画面各说各话。用户允许联网时主动搜索匹配角色、插图、UI与动画并实际融入叙事；选定模板是视觉资源，不能替代内容编排。

模板在制作前选择；不让用户逐项配置卡片、变体、音乐和音效。先按脚本识别需要证明的对象、关系和动作，再选当前模板实际支持的表达。一个版本保持主要视觉语言，允许不用卡片。

在适配候选中变化，优先避开近期重复；随机只在合适候选里进行并固定种子。音乐通常整片一条，优先让出口播；音效绑定有信息作用的动作。用户说“换一套”时另存候选，保留脚本、事实及已锁定内容。

每条片按分镜联网检索补充插图、图标、UI示意或动画，参见 [视觉素材来源与绑定](references/visual-assets.md)。优先用开源/可用来源，下载后核对主体、动作、时长、透明通道与可读文字；记录来源与许可。素材缓存到任务，不批量囤积与视频无关的素材。

真实园区展示使用用户素材；通用插图、角色、动画和界面示意用于解释，不伪装成实拍、实测或真实客户。优先既有免费资源与本机动画；个人包可按已授权的本地入口制作；保存 Key 的授权不代表授权付费生成或把素材发送给新的外部服务。模板许可为个人／非商业用途时，不用于园区商业成片；先选择许可适配的模板。

## 制作与交付

选定模板后在独立任务目录制作：确认脚本 → 真实语音时间 → 语义选镜与素材 → 必要的小样 → 完整输出。新场景可单独设计，不固定六镜结构。现成模板走其实际公开入口，不能假设任意镜头卡可跨引擎直接混用。

检查实际输出尺寸、字幕与音画、事实、遮挡和来源后保存版本。复杂动画 MP4 与可编辑镜头、原生工程、剪映草稿分别标注。公共入口和共享服务变更由相关对话协调；本 skill 不据此自动修改它们。
