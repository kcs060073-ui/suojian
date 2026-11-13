# 分镜工作台本地渲染接口

当前用户选择为 `分镜工作台-写实设备与半身讲解员预览.mp4`，selectedBaseline=`realistic-halfbody`。默认6镜头 original-0..5、static/minimal，在时间线不变时复用Main完整原始画面；原HUD、字幕、设备、入口、半身人物与过渡一并还原。第90帧与指定成片解码帧SSIM=0.988145，已目视对照。编码视频与PNG不能要求逐字节相同。

默认音频最终为 `selected-reference`。`public/audio/selected-realistic-halfbody.m4a` 是所选MP4的AAC无转码提取；FFmpeg音频bitstream SHA256与原片相同：`ef0b3e7d1234bb5bfb94f0e5a4b213a659799d1c2282671ee27c597a8896f210`。直接播放此AAC保留原混音。Remotion重新导出MP4会再次编码AAC，不能称新输出AAC字节与原片一致。旧9分轨作为可编辑混音备选保存，默认静音。

所有新增代码、素材副本和验证片均在本任务目录。依赖为现有 `ban/motion-park/node_modules` junction，没有安装或升级依赖。未启动或停止共享服务。

## CLI

```powershell
node engine/bridge.cjs catalog
node engine/bridge.cjs preview --config C:/absolute/config.json
node engine/bridge.cjs manifest --config C:/absolute/config.json --output C:/absolute/export-manifest.json
node engine/bridge.cjs still --config C:/absolute/config.json --output C:/absolute/frame.png --frame 90
node engine/bridge.cjs render --config C:/absolute/config.json --output C:/absolute/video.mp4
node engine/bridge.cjs render --config C:/absolute/config.json --output C:/absolute/smoke.mp4 --from 70 --to 99
```

输出为 JSON Lines。`catalog` 更新 default-config.json；`preview` 规范化配置并返回尺寸、时长，不渲染或配音；`manifest` 提供真实混音事件、重排后的单语字幕和烘焙边界。

渲染复用已有 Chromium。可用 `REMOTION_BROWSER_EXECUTABLE` 指定其他已有浏览器。`render.cjs render --fixed-mouth` 的历史入口保留；`WorkbenchRoot.tsx` 为独立制作版。

## 浏览器播放器

静态引用 `engine/player.js`，音视频资源通过 `/media/` 映射 `work/park-fusion/public`。

```js
const player = window.mountVibePlayer(element, {...config, assetBase:'/media'}, event => {
  // {type:'frame',frame} 或 {type:'update',duration}
});
player.update(nextConfig); player.seek(90); player.play(); player.pause();
player.getCurrentFrame(); player.destroy();
```

同一元素亦发 `vibe-frame` CustomEvent。修改配置不会重配音。现有旁白按镜头的 sourceOffset/speed 重映射；自带音频在全片时间线上从0播放。改文案不会改现有旁白。

## 配置与输出边界

- `shots` 顺序为输出顺序，`from` 自动顺排，`duration` 单位帧，至少15帧，`speed` 0.25–4。sourceOffset 同时映射现有旁白及其字幕。实拍卡片按源时长循环；原厂房片使用已知1050帧预算，自选视频通过媒体元数据读取时长。偏移大于源片长时按片长取模。
- `source` 支持资源URL或本地绝对路径，`sourceType:'image'` 可放静图，默认视频。CLI 自动把 `/media/...` 映射 public，将本地绝对路径素材按SHA256复制到本任务 public/workbench-imports。
- 23个可执行配方包含6个 `original-0..5` 原场景图形和17个项目适配动作。新配方继承三份skill的方法，并非逐像素复刻其全部独立原生demo。原场景主标题支持title换行、fontSize与textColor覆盖，默认值不覆盖原样；typography=kinetic提供逐字入场。原场景其余图形标签仍是既有内容。
- `style` 为 fusion/night/fable/explainer/ink，`background` 为 footage/stars/dots/flat。样式预设可同时改背景、人声和人物，单项设置仍独立可选。`camera` 的 continuous/depth 是慢推/透视运动，原始场景也支持；只移动场景内容，不移动全局HUD/字幕。它不自动生成完整项目空间模型。
- language:'zh'|'en' 为单语选择，不翻译，不生成双语；subtitleMode:'off' 关闭字幕。subtitleStyle:{fontSize,color,background}可改字幕字号、颜色和底色，不传时保持原Main字幕。transitionDuration为可选过渡帧数，默认原Main12帧。
- `narrationMode:'existing'|'silent'|'custom'`，自带音频用 narrationFile；musicFile 可替换音乐，musicGain/voiceGain 为0–2。默认 audioProfile:'selected-reference' 播放从用户所选MP4无转码提取的完整AAC混音。manifest保留参考混音gain1与9个缓存分轨gain0/mutedAlternative，并记录alternativeGain供编辑；缓存分轨重混与历史成片不完全相同，不能冒称从原混音分离或逐样本保真。选selected-baseline才启用9分轨与Main曲线。修改音量、SFX或音频文件后自动离开reference；改时序则切workbench。不要同时启用参考混音和备用分轨。
- `sfxEvents:[{source,from,duration,gain}]` 在默认转场音效之外插入音效，受sfxEnabled总开关控制。musicGain=0即可无BGM对照。
- `simulation` 用设备高度和入口净高作几何比较，并按同一比例画出关系；只作示意，不能代替现场勘察、通道转弯或吊装核验。
- presenter.enabled 可关闭人物。当前用户选定 realistic-halfbody，placement:'side'，grounded/fixedHead/fixedMouth默认false，保留所选原片的半身构图与头嘴动作。中央固定版仍可通过placement:'center'、grounded/fixedHead/fixedMouth:true独立选择。
- `endCredit` 为用户提供的单语片尾署名，默认无。`highlight:{enabled,from,duration,color}` 为一处卡片重点光圈。
- `plate:true` 仅移除独立字幕和全部音频；保留画面内部花字、图形、人物、相机和布局。复杂动画在MP4内烘焙。manifest列出可独立建轨的字幕和音频；不应把整条原旁白冒充重排后音轨。

## 已实测

- TypeScript `--noEmit` 通过。
- `player.js` production bundle 成功。
- 竖屏1080×1920 / 30fps / 30帧、H264+AAC短渲成功，f90原尺寸抽帧目视通过。
- 横屏1920×1080 / 30fps / 15帧、自带音频 `/media/audio/full-narration.wav`、点阵背景和入口模拟短渲成功。
- clean plate与关闭字幕版同帧PNG SHA256一致：`BCAAEC81FD706859788069AC9B363ECA807268C7D98AB9DE1ECA5D8DD86851CA`。plate manifest音频/字幕均0。
- 关闭人声和SFX的BGM-only实际导出：mean -39.4 dB / max -28.7 dB，确认音乐真实进入导出；未人工试听全片。
- 自定义sparkle SFX事件在musicGain=0 / voiceGain=0时独立短渲成功；原素材开头音量较轻，mean -66.8 dB / max -51.3 dB。
- 自选1秒MP4用于5秒镜头、sourceOffset=70，真实元数据与循环抽帧成功并目视检查。Renderer曾输出被替换帧请求取消日志，最终抽帧成功。

QA 位于 `work/park-fusion/qa`。本轮没有全长重新渲染基线，也没有运行配音生成。




