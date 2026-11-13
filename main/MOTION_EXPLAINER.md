# 溯剪动画讲解 · 首轮项目实验

入口：http://127.0.0.1:8788/motion?job=a89d53f9
首页已有“动画讲解实验室”链接。

## 已跑通

现有项目实拍与配音 → HDR 转 SDR → Remotion 动态文字与空间示意 → 网页实时预览 → 保存画面参数 → 本机生成 H.264 MP4。

当前画布 1280×720、30 fps、1067 帧（约35.57秒），使用嘉兴项目 a89d53f9 的已有素材。没有新调用配音或生成式 AI API。

参数编辑只影响画面，口播和底部字幕仍沿用已有配音文案。生成的新文件使用独立编号，保留上次输出。相同组件用于网页预览和渲染，降低两者样式差异。重启服务后会重新读取保存的画面方案与导出结果；渲染中断会提示重试。

## 首轮边界

- 这是项目动画讲解模板原型，尚未对指定“知识大赏”作者或视频逐镜复刻；等待具体参考链接。
- 首轮固定使用嘉兴示例，不是任意素材库的自动动画生成器。
- 配音、字幕和包装以现有任务数据为依据；项目参数未独立核验。
- 示意线条并非实测建筑结构图。最后一段素材轻微慢放以覆盖口播，不再循环或停帧。
- 可导出 MP4，尚未转为剪映原生可编辑文字/图形/动画图层。

## 教程与 Skill

- 官方 Skill：https://www.remotion.dev/docs/ai/skills
- 官方动画原理：https://www.remotion.dev/docs/idea
- 网页播放器：https://www.remotion.dev/docs/player
- 下载的规则包：experiments/video-skills/remotion-best-practices（包含 create、markup、render、saas 等规则）

## 维护

引擎项目位于 ban/motion-park。溯剪 templates/motion-engine.json 指向引擎目录。网页入口直接由溯剪8788服务提供，无需单独启动Remotion Studio。

修改引擎后，在引擎目录执行 npx tsc --noEmit、node build-player.cjs，随后从网页重新生成成片。依赖版本已锁定为 Remotion 4.0.535。

后续优先级：参考视频拆解 → 拓展知识型场景组件 → 文案与配音联动 → 任意素材库选镜 → 动画图层映射剪映草稿。
