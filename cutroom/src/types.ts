// 数字人混剪流水线的数据契约。
// 坐标/尺寸统一用 0-1 的相对比例，渲染时乘画布宽高，分辨率无关。

export interface InsertPosition {
  x: number; // 左上角 X，画布宽度的比例 0-1
  y: number; // 左上角 Y，画布高度的比例 0-1
  width: number; // 宽度，画布宽度的比例 0-1
  height: number; // 高度，画布高度的比例 0-1
}

export interface InsertStyle {
  borderRadius?: number; // px
  border?: string; // CSS border，如 "4px solid #fff"
  opacity?: number; // 0-1
}

export interface InsertItem {
  time: number; // 出现时间点（秒）
  duration: number; // 持续时长（秒）
  type: "image" | "video";
  src: string; // public/inserts/ 下的文件名
  position: InsertPosition;
  style?: InsertStyle;
}

export interface SubtitleItem {
  start: number; // 起始时间（秒）
  end: number; // 结束时间（秒）
  text: string;
}

export interface Storyboard {
  id: string;
  outputName: string; // 输出文件名（不含扩展名）
  fps: number;
  duration: number; // 总时长（秒）
  digitalHuman: {
    src: string; // public/digital_human/ 下的文件名
  };
  inserts: InsertItem[];
  subtitles: SubtitleItem[];
}

// ── P1 统一引擎：一条时间轴驱动「倍速 + 粗宋橘黄字幕 + 画中画 + 全屏B-roll + 标题卡」一次烧成 ──
// 所有 start/end 都是「最终成片时间轴」（已含倍速换算），与 make-ass 产出的 .ass 同一时间轴。

// 原素材声音策略（M3 原声策略+BGM压低）：
//   keep   同期声对现场感/信息有价值 → 保留原声（侧链压在主声之下）
//   mute   闲聊/隐私/无关 → 静音（默认，不进混音）
//   duck   环境声可保留但必须低于主旁白 → 侧链压低
//   clean  有价值人声/环境声需基础降噪(高通+FFT降噪) → 降噪后侧链压低
//   replace 原声不可用 → 由主旁白/BGM替代（等同静音，不进混音）
export type AudioPolicy = "keep" | "mute" | "duck" | "clean" | "replace";
export const AUDIO_POLICIES: AudioPolicy[] = ["keep", "mute", "duck", "clean", "replace"];

export interface MixInsert {
  // broll = 全屏空镜，临时盖住素体；image/video = 画中画小图/小视频
  type: "broll" | "image" | "video";
  src: string; // public/inserts/ 下的文件名
  start: number; // 出现时间（秒，最终成片时间轴）
  end: number; // 消失时间（秒）
  sourceStart?: number; // 素材源时间窗开始秒；旧计划省略时按 0 处理
  sourceEnd?: number; // 素材源时间窗结束秒；旧视频计划省略时按成片槽时长处理，图片固定 0
  position?: InsertPosition; // 画中画用；broll 省略=全屏铺满
  audio?: AudioPolicy; // 原素材声音策略；省略=默认 mute（不进混音）
}

export interface MixTitle {
  text: string; // 标题文字（顶部大字）
  start: number;
  end: number;
  src?: string; // 可选背景空镜/图；省略则标题直接压在素体上
  presetId?: string; // assets/manifests/visual-cards.json 中的标题卡预设
}

// 角标/水印：全程固定挂在一角的图（logo/标识）。position 用相对比例，高度自适应保持原图比例。
export interface MixWatermark {
  src: string; // public/inserts/ 或 public/ 下的图（建议透明 PNG）
  position?: InsertPosition; // 只用 x/y/width；height 自适应。省略=左上小角标
  opacity?: number; // 0-1，默认 1
}

// 贴纸：定时出现在指定位置的透明 PNG（图标/文字标签）。复用 watermark overlay 模式 + 时间窗。
export interface MixSticker {
  src?: string;      // 直接给文件路径（public/inserts/ 或 public/ 下）
  presetId?: string; // 或引用 assets/manifests/stickers.json 的贴纸 id（二选一）
  x?: number;        // 左上角归一化横坐标 0-1，默认 0.04
  y?: number;        // 左上角归一化纵坐标 0-1，默认 0.04
  start: number;     // 出现时间（秒，最终成片时间轴）
  end: number;       // 消失时间（秒）
  scale?: number;    // 宽度占画布比例，默认 0.2
  opacity?: number;  // 0-1，默认 1
}

// 花字：动态生成的透明 PNG（渐变/描边/发光/圆角底/阴影）。entrance 在渲染侧用 fade/scale 滤镜近似。
export interface MixFancyText {
  text: string;          // 花字文案
  presetId: string;      // 引用 assets/manifests/fancy-text-presets.json 的预设 id
  x?: number;            // 左上角归一化横坐标 0-1，默认 0.04
  y?: number;            // 左上角归一化纵坐标 0-1，默认 0.04
  start: number;         // 出现时间（秒，最终成片时间轴）
  end: number;           // 消失时间（秒）
  scale?: number;        // 宽度占画布比例，默认 0.25
  opacity?: number;      // 0-1，默认 1
  entrance?: "none" | "fade" | "scale" | "pop" | "bounce" | "slide"; // 默认 fade
}

// 背景音乐：垫在口播音轨下方，自动循环铺满；主声区间自动压低（sidechain ducking）
export interface MixBgm {
  src: string; // public/inserts/ 或 public/ 下的音频
  gain?: number; // 音量系数（基线），默认 0.18（≈-15dB，口播为主，BGM 衬底）
  duck?: boolean; // 是否随主口播自动压低，默认 true
}

export interface MixSfx {
  src: string;
  start: number;
  gain?: number;
}

export interface MixOutputProfile {
  maxDuration?: number;
  width?: number;
  height?: number;
  crf?: number;
}

export interface MixPlan {
  speed?: number; // 倍速，默认 1.25
  inserts?: MixInsert[];
  titles?: MixTitle[];
  watermark?: MixWatermark; // 全程角标
  stickers?: MixSticker[]; // 定时贴纸层（图标/文字标签）
  fancyTexts?: MixFancyText[]; // 花字层（动态透明 PNG）
  bgm?: MixBgm; // 背景音乐
  sfx?: MixSfx[]; // 按最终时间轴触发的短音效
  subtitlePresetId?: string; // 已编译的字幕预设，用于渲染溯源
  voice?: { src: string; presetId?: string }; // 已存在的任务配音缓存；不得隐式生成
  output?: MixOutputProfile; // 低成本预览输出；正式渲染省略
}

// 上 1/3 区的默认插入位置，避开数字人居中偏下的脸部
export const DEFAULT_INSERT_POSITION: InsertPosition = {
  x: 0.1,
  y: 0.08,
  width: 0.8,
  height: 0.34,
};
