# 视觉素材来源与绑定

在制作任务里按段落检索，先确定“对象＋动作＋表达目的”，再选择静态插图、SVG/UI、Lottie 或透明视频。默认由对话检索与判断，无需用户逐张选择。

## 可优先查找的真实来源

- Microsoft Fluent Emoji：https://github.com/microsoft/fluentui-emoji 。MIT 仓库，含多种外观；3D PNG 本身是静图，需要本机编排位置、遮罩或动作后才能称为动画。保留版权与许可。
- Lucide：https://lucide.dev/icons/ ，许可 https://lucide.dev/license 。SVG 可用于画面图解和 UI，按项目许可保留声明；部分来自 Feather 的图标另有对应 MIT 声明。描边、连线、路径和状态变化由本机制作。
- LottieFiles：https://lottiefiles.com/free-animations 。逐条核对能否取得、具体许可、外部图片/字体依赖与文字内容；https://lottiefiles.com/page/license 不能替代具体资源核实。不假定 Premium 可免费取得，不批量镜像其动画库。
- Wikimedia Commons：https://commons.wikimedia.org/ 。逐文件查看来源、许可和署名要求，适合已确认对象和示意图片。

来源不是排他白名单；主题需要时可搜索其他作者的正式发布页。不得仅凭搜索缩略图或“免费”标签推定可商用或已下载。

## 实际使用方式

1. 从脚本形成中英文关键词，例如“搬迁：forklift relocation / production restart”“产权：key ownership / direct agreement”“交付：delivery truck / calendar schedule”。避免向外部搜索服务发送客户私密资料。
2. 检查实物图是否对应主题、角色是否属于同一风格、动画的关键动作是否匹配口播。UI 使用用户实际截图或明确的示意界面，不制作伪证件或虚构客户记录。
3. 缓存使用到的资源、来源页面、作者、许可 URL、下载时间、SHA256、适用段落和本机修改说明。静图动画化与来源自带动画分别标记。
4. 优先取少量语义匹配候选；同任务缓存复用。没有合适资源时，可用本机 SVG/Canvas/Remotion 等设计原创图解，不为填画面采用无关插图。
5. 在任务所用渲染器中验证兼容性与实际输出。Lottie、透明 WebM、SVG 并非每个模板都可直接使用；必要时在许可允许的范围内预渲染成兼容媒体，保留源文件。

## 音乐与音效

沿用当前任务明确锁定的声音；否则按情绪、节奏、口播密度和可用时长匹配。使用本地合法素材或联网取得的可用来源，记录许可。不自动使用 MotionGit 个人 Key 的音乐、贴纸或素材接口，也不因免费搜索失败转为付费生成。
