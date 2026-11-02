/**
 * 实体信息流广告 · P4 渲染（转场 + 字幕 + 配音 + BGM，横/竖版）。
 * 读 <name>.adrender.json（compile-ad 产出，每段 src/start/end/dur/text）+ voice.mp3 →
 *   1. 每段素材 scale+居中裁切到画布、trim 到该段时长（+转场重叠量）
 *   2. 段间 xfade 淡入淡出转场（延长每段 T 重叠，保证画面与配音不脱轨）
 *   3. 烧 ASS 字幕（阿里妈妈数黑·白字·黑阴影，样式见 ad-style.ts）
 *   4. 配音为主音轨，可选 BGM 压低混入
 *   5. 横版 1920x1080 / 竖版 1080x1920，按 adrender.ratio
 *
 * 用法: ts-node scripts/render-ad.ts <name> [bgm路径]
 *   产物：out/<name>.mp4
 *
 * 中文路径坑：把素材/配音/字幕全拷到临时 ASCII 工作目录、ffmpeg 在那跑、相对名引用，最后移回 out/。
 */
import "./load-env";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { execFileSync } from "child_process";
import { AD_SUBTITLE } from "./ad-style";
const { layoutSubtitle } = require("../src/ad-subtitle-layout.js") as {
  layoutSubtitle(text: string, maxChars: number, maxLines: number, protectedTerms?: string[]): string;
};

const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(ROOT, "out");
const FONTS = path.join(ROOT, "assets", "fonts");

let T = 0.4; // 转场时长(s)
const IMG_EXT = /\.(png|jpe?g|webp)$/i;
const PLACEHOLDER_COLOR = "0x14213d"; // 缺素材占位屏（深品牌蓝）

// 探素材真实时长（秒）；探不到返回 0
function probeDur(file: string): number {
  try {
    const o = execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file], { encoding: "utf8" });
    return parseFloat(o.trim()) || 0;
  } catch { return 0; }
}
// Normalize source footage BEFORE SDR subtitles/graphics are composited.
// Inheriting HLG/PQ tags makes browsers tone-map the subtitle palette as HDR.
function sourceColorFilter(file: string): string {
  const result = JSON.parse(execFileSync("ffprobe", ["-v", "error", "-select_streams", "v:0",
    "-show_entries", "stream=color_space,color_transfer,color_primaries,color_range", "-of", "json", file], { encoding: "utf8" }));
  const stream = result.streams?.[0] ?? {};
  const hdr = ["arib-std-b67", "smpte2084"].includes(stream.color_transfer);
  const tags = "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709,";
  if (hdr) {
    const matrix = stream.color_space === "bt709" ? "709" : "2020_ncl";
    const primaries = stream.color_primaries === "bt709" ? "709" : "2020";
    const transfer = stream.color_transfer === "smpte2084" ? "smpte2084" : "arib-std-b67";
    const range = stream.color_range === "pc" ? "full" : "limited";
    return `zscale=matrixin=${matrix}:primariesin=${primaries}:transferin=${transfer}:rangein=${range}:transfer=linear:npl=100,`
      + "format=gbrpf32le,zscale=primaries=709,tonemap=tonemap=hable:desat=0,"
      + "zscale=transfer=709:matrix=709:range=limited,format=yuv420p,"
      + "sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA,sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL," + tags;
  }
  // Unspecified SDR sources use FFmpeg's matrix inference; explicitly normalize output.
  return "scale=out_color_matrix=bt709:out_range=tv," + tags;
}

// 剪映“号”→像素，首版估值，渲后按观感微调
const FONT_PX: Record<"h" | "v", number> = { h: 52, v: 70 };
const MARGIN_V: Record<"h" | "v", number> = { h: 90, v: 620 }; // 字幕距底(px)；竖版 620=抖音安全区（避开底部 UI 遮挡）
const SHADOW: Record<"h" | "v", number> = { h: 4, v: 5 };
const MAX_CHARS: Record<"h" | "v", number> = { h: 30, v: 13 }; // 每行最多字数，超了手动断行

// ASS syntax is escaped after layout; raw copy cannot introduce override tags.
function subtitleAssText(text: string): string {
  return text.split("\n").map(line => line.replace(/\\/g, "\\\\").replace(/\{/g, "\\{").replace(/\}/g, "\\}")).join("\\N");
}

interface RSeg { i: number; text: string; displayText?: string; displaySize?: number; numberPreset?: string; subtitleStart?: number; subtitleEnd?: number; start: number; end: number; dur: number; src: string | null; matched: boolean; sourceStart?: number; sourceEnd?: number; segmentId?: string | null }
interface AdSubtitlePreset {
  id: string; fontname_guess: string; size: number; primary_hex: string;
  outline_hex?: string; shadow_hex?: string; back_hex?: string; back_alpha_hex?: string;
  bold: boolean; alignment: number; margin_v: number; outline?: number; shadow?: number;
  border_style: number; spacing?: number; max_chars_per_line?: number;
  fade_in_ms?: number; fade_out_ms?: number; ass_style?: string; max_lines?: number; protected_terms?: string[];
}
interface AdOutputProfile { maxDuration?: number; width?: number; height?: number; crf?: number }
interface AdRender {
  name: string; ratio: string; total: number; voice: string | { src: string; presetId?: string }; segments: RSeg[];
  subtitlePreset?: AdSubtitlePreset; subtitleEvents?: RSeg[]; transitionMode?: "cut" | "fade"; output?: AdOutputProfile;
  bgm?: { src: string; gain?: number; duck?: boolean; start?: number; fadeIn?: number; fadeOut?: number };
  sfx?: Array<{ src: string; start: number; gain?: number }>;
  titles?: Array<{ text: string; start: number; end: number; presetId?: string; style?: { fontFamily?: string; primaryHex?: string; backgroundHex?: string } }>;
  stickers?: Array<{ src?: string; start: number; end: number; x?: number; y?: number; scale?: number; opacity?: number }>;
  fancyTexts?: Array<{ text: string; start: number; end: number; x?: number; y?: number; scale?: number; opacity?: number; entrance?: string; style?: { fontFamily?: string; primaryHex?: string; outlineHex?: string; backgroundHex?: string; glowHex?: string | null } }>;
  emphasisOverlays?: Array<{ src: string; start: number; end: number; x: number; y: number; scale: number; opacity: number; entrance?: string }>;
  watermark?: { src: string; opacity?: number; position?: { x?: number; y?: number; width?: number } };
}

interface RenderArgs {
  name: string;
  bgm?: string;
  explicitPlan?: string;
  explicitOutput?: string;
  maxDuration?: number;
  width?: number;
  height?: number;
  crf?: number;
}

function parseArgs(argv: string[]): RenderArgs {
  const explicit = argv.some((value) => value.startsWith("--"));
  if (!explicit) {
    const [name, bgm, ...extra] = argv;
    if (!name || extra.length) throw new Error("用法: ts-node scripts/render-ad.ts <name> [bgm路径]");
    return { name, bgm };
  }
  const allowed = new Set(["--plan", "--output", "--bgm", "--max-duration", "--width", "--height", "--crf"]);
  const values = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith("--") || !value || value.startsWith("--")) throw new Error("显式调用缺少参数值");
    if (!allowed.has(flag)) throw new Error(`未知参数：${flag}`);
    values.set(flag, value);
  }
  const explicitPlan = values.get("--plan");
  const explicitOutput = values.get("--output");
  if (!explicitPlan || !explicitOutput) throw new Error("显式调用必须提供 --plan 和 --output");
  const numeric = (flag: string): number | undefined => {
    const raw = values.get(flag);
    if (raw === undefined) return undefined;
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error(`${flag} 必须是数字`);
    return value;
  };
  const maxDuration = numeric("--max-duration");
  const width = numeric("--width");
  const height = numeric("--height");
  const crf = numeric("--crf");
  if (maxDuration !== undefined && (maxDuration <= 0 || maxDuration > 12)) throw new Error("--max-duration 必须在 0 到 12 秒之间");
  if ((width === undefined) !== (height === undefined)) throw new Error("--width 和 --height 必须同时提供");
  if (width !== undefined && height !== undefined && (
    !Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0
    || width > 1080 || height > 1920 || width % 2 !== 0 || height % 2 !== 0
  )) throw new Error("预览尺寸必须是范围内的正偶数");
  if (crf !== undefined && (!Number.isInteger(crf) || crf < 18 || crf > 32)) throw new Error("--crf 必须是 18 到 32 的整数");
  return {
    name: path.basename(explicitPlan).replace(/\.adrender\.json$/i, "") || "ad-task",
    bgm: values.get("--bgm"),
    explicitPlan,
    explicitOutput,
    maxDuration,
    width,
    height,
    crf,
  };
}

const cs = (sec: number) => {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${s.toFixed(2).padStart(5, "0")}`;
};
const assColor = (hex: string, alpha = 0) => {
  const c = hex.replace("#", "");
  const rr = c.slice(0, 2), gg = c.slice(2, 4), bb = c.slice(4, 6);
  return `&H${alpha.toString(16).padStart(2, "0").toUpperCase()}${bb}${gg}${rr}`.toUpperCase();
};

function buildAss(
  segs: RSeg[], W: number, H: number, o: "h" | "v",
  preset?: AdSubtitlePreset, titles: AdRender["titles"] = [],
  fancyTexts: AdRender["fancyTexts"] = [],
): string {
  const st = AD_SUBTITLE[o];
  const font = preset?.fontname_guess ?? st.fontFamily[0];
  const titleStyle = titles[0]?.style;
  const fancyStyle = fancyTexts[0]?.style;
  const titleFont = titleStyle?.fontFamily || font;
  const fancyFont = fancyStyle?.fontFamily || font;
  const titlePrimary = assColor(titleStyle?.primaryHex || "#FFFFFF");
  const titleBack = assColor(titleStyle?.backgroundHex || "#000000", 64);
  const fancyPrimary = assColor(fancyStyle?.primaryHex || "#FFB300");
  const fancyOutline = assColor(fancyStyle?.outlineHex || "#000000");
  const fancyBack = assColor(fancyStyle?.backgroundHex || "#000000", fancyStyle?.backgroundHex ? 32 : 112);
  const primary = assColor(preset?.primary_hex ?? st.color, 0); // 白，不透明
  const back = preset
    ? assColor(preset.back_hex ?? preset.shadow_hex ?? "#000000", parseInt(preset.back_alpha_hex ?? "80", 16))
    : assColor(st.shadowColor, Math.round((1 - st.shadowOpacity) * 255)); // 阴影：alpha=透明度反码
  const defaultStyle = `Style: Default,${font},${preset?.size ?? FONT_PX[o]},${primary},${primary},${assColor(preset?.outline_hex ?? "#000000")},${back},${preset ? (preset.bold ? -1 : 0) : -1},0,0,0,100,100,${preset?.spacing ?? 0},0,${preset?.border_style ?? 1},${preset?.outline ?? 0},${preset?.shadow ?? (preset ? 0 : SHADOW[o])},${preset?.alignment ?? 2},60,60,${preset?.margin_v ?? MARGIN_V[o]},1`;
  const style = preset?.ass_style ? preset.ass_style.replace(/^Style:[^,]+,/, "Style: Default,") : defaultStyle;
  const head =
    `[Script Info]\nScriptType: v4.00+\nPlayResX: ${W}\nPlayResY: ${H}\nScaledBorderAndShadow: yes\nWrapStyle: 0\n\n` +
    `[V4+ Styles]\n` +
    `Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n` +
    `${style}\n` +
    `Style: PackTitle,${titleFont},${Math.max(28, Math.round(W * 0.072))},${titlePrimary},${titlePrimary},${titleBack},${titleBack},-1,0,0,0,100,100,1,0,3,8,0,8,40,40,${Math.round(H * 0.08)},1\n` +
    `Style: PackFancy,${fancyFont},${Math.max(24, Math.round(W * 0.06))},${fancyPrimary},${fancyPrimary},${fancyOutline},${fancyBack},-1,0,0,0,100,100,1,0,1,2,2,7,40,40,${Math.round(H * 0.18)},1\n\n` +
    `[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n`;
  const fade = preset && (preset.fade_in_ms || preset.fade_out_ms)
    ? `{\\fad(${preset.fade_in_ms ?? 0},${preset.fade_out_ms ?? 0})}` : "";
  const lines = segs.flatMap((s) => {
    const text = s.displayText === undefined
      ? layoutSubtitle(s.text, preset?.max_chars_per_line ?? MAX_CHARS[o], preset?.max_lines ?? 2, preset?.protected_terms ?? [])
      : s.displayText;
    if (!text.trim()) return [];
    let displayed = subtitleAssText(text);
    if (s.numberPreset) {
      displayed = displayed.replace(/([0-9]+(?:\.[0-9]+)?(?:到[0-9]+)?(?:个月|米|吨|万|%|元)?)/g,
        (_, value: string) => `{\\fscx112\\fscy112\\t(0,150,\\fscx100\\fscy100)}${value}{\\fscx100\\fscy100}`);
    }
    const sizeTag = s.displaySize ? `{\\fs${s.displaySize}}` : "";
    return [`Dialogue: 0,${cs(s.subtitleStart ?? s.start)},${cs(s.subtitleEnd ?? s.end)},Default,,0,0,0,,${fade}${sizeTag}${displayed}`];
  });
  for (const cue of titles ?? []) {
    lines.push(`Dialogue: 1,${cs(cue.start)},${cs(cue.end)},PackTitle,,0,0,0,,${cue.text.replace(/\n/g, "\\N")}`);
  }
  for (const cue of fancyTexts ?? []) {
    lines.push(`Dialogue: 2,${cs(cue.start)},${cs(cue.end)},PackFancy,,0,0,0,,{\\fad(180,180)}${cue.text.replace(/\n/g, "\\N")}`);
  }
  return head + lines.join("\n") + "\n";
}

function main() {
  let cli: RenderArgs;
  try {
    cli = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error((error as Error).message);
    process.exit(1);
    return;
  }
  const { name, explicitPlan, explicitOutput } = cli;
  const arPath = explicitPlan ? path.resolve(explicitPlan) : path.join(ROOT, "public", "scripts", name + ".adrender.json");
  if (!fs.existsSync(arPath)) { console.error(`缺 ${arPath}（先跑 compile-ad）`); process.exit(1); }
  const source: AdRender = JSON.parse(fs.readFileSync(arPath, "utf8"));
  const profile: AdOutputProfile = { ...(source.output ?? {}) };
  if (cli.maxDuration !== undefined) profile.maxDuration = cli.maxDuration;
  if (cli.width !== undefined) profile.width = cli.width;
  if (cli.height !== undefined) profile.height = cli.height;
  if (cli.crf !== undefined) profile.crf = cli.crf;
  const maxDuration = profile.maxDuration;
  const segments = source.segments
    .filter((segment) => maxDuration === undefined || segment.start < maxDuration)
    .map((segment) => {
      if (maxDuration === undefined || segment.end <= maxDuration) return segment;
      const end = maxDuration;
      const dur = end - segment.start;
      const sourceEnd = segment.sourceStart === undefined ? segment.sourceEnd : segment.sourceStart + dur;
      return { ...segment, end, dur, sourceEnd };
    });
  if (!segments.length) throw new Error("AD_PREVIEW_TIMELINE_EMPTY: 没有可渲染片段");
  const ar: AdRender = {
    ...source,
    segments,
    total: maxDuration === undefined ? source.total : Math.min(source.total, maxDuration),
  };
  T = ar.transitionMode === "cut" ? 0 : .4;
  const bgm = cli.bgm ?? ar.bgm?.src;

  // 缺素材不再阻断整片：用品牌色占位屏兜底（字幕照常烧），并告警提示补素材后重渲提质。
  const missing = ar.segments.filter((s) => !s.src);
  if (missing.length) {
    console.warn(`⚠ 有 ${missing.length} 段没素材，用占位屏兜底（不阻断）。补素材后重跑可提质：`);
    for (const m of missing) console.warn(`    [${m.start}s] ${m.text}`);
  }
  const [defaultW, defaultH] = ar.ratio === "16:9" ? [1920, 1080] : [1080, 1920];
  const W = profile.width ?? defaultW;
  const H = profile.height ?? defaultH;
  const o: "h" | "v" = ar.ratio === "16:9" ? "h" : "v";
  // voice 兼容两种形态：字符串路径（compile-ad 产出）或 {src, presetId} 对象（包装层编译产出）
  const voiceRaw = ar.voice as unknown;
  const voiceSrc = typeof voiceRaw === "string" ? voiceRaw : (voiceRaw as { src?: string })?.src ?? "";
  if (!voiceSrc) { console.error("缺配音（adrender.json 没有 voice 路径）"); process.exit(1); }
  const voiceAbs = path.isAbsolute(voiceSrc) ? voiceSrc : path.join(ROOT, voiceSrc);
  if (!fs.existsSync(voiceAbs)) { console.error(`缺配音 ${voiceAbs}（先跑 tts）`); process.exit(1); }

  // 临时 ASCII 工作目录，规避中文路径
  const work = fs.mkdtempSync(path.join(os.tmpdir(), `ad_${name}_`));
  const n = ar.segments.length;
  // Validate all selected windows before FFmpeg; legacy plans without windows retain looping.
  for (const segment of ar.segments) {
    if (segment.sourceStart === undefined && segment.sourceEnd === undefined) continue;
    const start = segment.sourceStart, end = segment.sourceEnd;
    if (start === undefined || end === undefined || !Number.isFinite(start) || !Number.isFinite(end)
      || start < 0 || end < start || (!IMG_EXT.test(segment.src ?? "") && (end <= start || end - start + 1e-6 < segment.dur))) {
      throw new Error(`AD_SOURCE_WINDOW_INVALID: segment ${segment.i}`);
    }
  }
  // 每段一个输入；按「图片/短视频/缺素材」决定输入标志，保证每段都能铺满该段所需时长（dur+T）
  const segInputs: string[][] = [];
  const colorFilters: string[] = [];
  ar.segments.forEach((s, i) => {
    colorFilters[i] = "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709,";
    const L = ar.segments[i].dur + T; // 该段需要的画面时长（含转场重叠料）
    if (!s.src) {
      // 缺素材：品牌色占位屏兜底，时长略大于 L（字幕仍照常烧在上面）
      segInputs.push(["-f", "lavfi", "-i", `color=c=${PLACEHOLDER_COLOR}:s=${W}x${H}:r=30:d=${(L + 0.2).toFixed(3)}`]);
      return;
    }
    const ext = path.extname(s.src) || ".mp4";
    const dst = `clip_${String(i).padStart(2, "0")}${ext}`;
    const source = path.isAbsolute(s.src) ? s.src : path.join(ROOT, s.src);
    fs.copyFileSync(source, path.join(work, dst));
    if (!IMG_EXT.test(ext)) colorFilters[i] = sourceColorFilter(path.join(work, dst));
    if (IMG_EXT.test(ext)) {
      segInputs.push(["-loop", "1", "-i", dst]); // 图片：无限帧，trim 到 L（避免只剩 1 帧导致段崩）
    } else {
      const rd = probeDur(path.join(work, dst));
      if (s.sourceEnd !== undefined && (rd <= 0 || s.sourceEnd > rd + 0.05)) throw new Error(`AD_SOURCE_WINDOW_OUT_OF_RANGE: segment ${s.i}`);
      if (s.sourceEnd === undefined && rd > 0 && rd < L + 0.05) segInputs.push(["-stream_loop", "-1", "-i", dst]); // 短视频：循环铺满，避免播完冻结/黑帧
      else segInputs.push(["-i", dst]);
    }
  });
  fs.copyFileSync(voiceAbs, path.join(work, "voice.mp3"));
  if (bgm && fs.existsSync(bgm)) fs.copyFileSync(bgm, path.join(work, "bgm.mp3"));
  // 字体拷进工作目录，ass 用 fontsdir=. 找
  for (const f of fs.readdirSync(FONTS)) if (/\.(otf|ttf|ttc)$/i.test(f)) fs.copyFileSync(path.join(FONTS, f), path.join(work, f));
  fs.writeFileSync(
    path.join(work, "sub.ass"),
    buildAss(ar.subtitleEvents ?? ar.segments, W, H, o, ar.subtitlePreset, ar.titles, ar.fancyTexts),
    "utf8",
  );

  // filter_complex：每段 scale+crop+trim（延长 T 给转场重叠），再链式 xfade，最后烧字幕
  const parts: string[] = [];
  ar.segments.forEach((s, i) => {
    const L = (s.dur + T).toFixed(3); // 延长 T 作转场重叠料
    const window = s.sourceStart !== undefined && s.sourceEnd !== undefined && !IMG_EXT.test(s.src ?? "")
      ? `trim=start=${s.sourceStart.toFixed(3)}:end=${s.sourceEnd.toFixed(3)},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=${T},` : "";
    parts.push(`[${i}:v]${window}${colorFilters[i]}scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H},setsar=1,fps=30,trim=duration=${L},setpts=PTS-STARTPTS[v${i}]`);
  });
  let prev = "v0";
  if (n === 1) {
    parts.push(`[v0]null[vbase]`);
  } else if (T === 0) {
    parts.push(`${ar.segments.map((_,i) => `[v${i}]`).join("")}concat=n=${n}:v=1:a=0[vbase]`);
  } else {
    let off = ar.segments[0].dur; // 首段 offset = D0 - T = (d0+T) - T = d0
    for (let i = 1; i < n; i++) {
      const out = i === n - 1 ? "vcat" : `vt${i}`;
      parts.push(`[${prev}][v${i}]xfade=transition=fade:duration=${T}:offset=${off.toFixed(3)}[${out}]`);
      off += ar.segments[i].dur; // 增量 = D_i - T = d_i
      prev = out;
    }
    parts.push(`[vcat]null[vbase]`);
  }

  // 音频：配音为主；有 BGM 则以配音为检测源侧链压低（M3：BGM 主声区间自动压低，人声处压、停顿处恢复）
  const inputs: string[] = [];
  segInputs.forEach((a) => inputs.push(...a)); // 每段一个视频输入（含 -loop/-stream_loop/lavfi 标志）
  inputs.push("-i", "voice.mp3");
  const voiceIdx = n;
  let nextInputIdx = n + 1;
  let audioBase: string | undefined;
  let audioMap = `${voiceIdx}:a`;
  if (bgm && fs.existsSync(path.join(work, "bgm.mp3"))) {
    const music: Partial<NonNullable<AdRender["bgm"]>> = ar.bgm ?? {};
    const start = music.start ?? 0;
    if (!Number.isFinite(start) || start < 0) throw new Error("BGM_WINDOW_INVALID");
    inputs.push("-stream_loop", "-1", "-ss", String(start), "-i", "bgm.mp3");
    const fadeIn = Math.min(ar.total / 2, Math.max(0, music.fadeIn ?? .35));
    const fadeOut = Math.min(ar.total / 2, Math.max(0, music.fadeOut ?? 1.5));
    parts.push(`[${nextInputIdx}:a]atrim=duration=${ar.total},asetpts=PTS-STARTPTS,volume=${music.gain ?? 0.12},aformat=channel_layouts=stereo`
      + (fadeIn > 0 ? `,afade=t=in:st=0:d=${fadeIn}` : "")
      + (fadeOut > 0 ? `,afade=t=out:st=${Math.max(0,ar.total-fadeOut)}:d=${fadeOut}` : "") + "[bgraw]");
    if (music.duck !== false) {
      parts.push(`[${voiceIdx}:a]highpass=f=80,asplit=2[vokey][vo]`);
      parts.push(`[bgraw][vokey]sidechaincompress=threshold=0.01:ratio=6:attack=15:release=300[bgduck]`);
      parts.push(`[vo][bgduck]amix=inputs=2:duration=first:normalize=0[abase]`);
    } else {
      parts.push(`[${voiceIdx}:a]highpass=f=80[vo]`);
      parts.push(`[vo][bgraw]amix=inputs=2:duration=first:normalize=0[abase]`);
    }
    nextInputIdx++;
    audioBase = "abase";
  } else {
    parts.push(`[${voiceIdx}:a]highpass=f=80[abase]`);
    audioBase = "abase";
  }

  const sfxLabels: string[] = [];
  for (const [index, effect] of (ar.sfx ?? []).entries()) {
    if (!fs.existsSync(effect.src)) throw new Error(`PACKAGING_SFX_MISSING: ${effect.src}`);
    const ext = path.extname(effect.src) || ".wav";
    const staged = `sfx_${index}${ext}`;
    fs.copyFileSync(effect.src, path.join(work, staged));
    inputs.push("-i", staged);
    const label = `sfx${index}`;
    const delay = Math.round(effect.start * 1000);
    parts.push(`[${nextInputIdx}:a]adelay=${delay}|${delay},volume=${effect.gain ?? 0.35}[${label}]`);
    sfxLabels.push(label);
    nextInputIdx++;
  }
  if (sfxLabels.length) {
    if (!audioBase) throw new Error("PACKAGING_AUDIO_GRAPH_INVALID: 缺少主音轨");
    parts.push(`[${audioBase}]${sfxLabels.map((label) => `[${label}]`).join("")}amix=inputs=${sfxLabels.length + 1}:duration=first:normalize=0[amixed]`);
    audioBase = "amixed";
  }
  parts.push(`[${audioBase}]loudnorm=I=-16:TP=-1.5:LRA=11[aout]`);
  audioMap = "[aout]";

  let videoBase = "vbase";
  const overlays = [
    ...(ar.watermark?.src ? [{
      src: ar.watermark.src,
      start: 0,
      end: ar.total,
      x: ar.watermark.position?.x ?? 0.04,
      y: ar.watermark.position?.y ?? 0.02,
      scale: ar.watermark.position?.width ?? 0.28,
      opacity: ar.watermark.opacity ?? 1, entrance: "none",
    }] : []),
    ...(ar.stickers ?? []).filter((item) => Boolean(item.src)).map((item) => ({
      src: item.src!, start: item.start, end: item.end, x: item.x ?? 0.72,
      y: item.y ?? 0.12, scale: item.scale ?? 0.2, opacity: item.opacity ?? 1, entrance: "none",
    })),
    ...(ar.emphasisOverlays ?? []).map(item => ({...item,entrance:item.entrance ?? "fade"})),
  ];
  for (const [index, overlay] of overlays.entries()) {
    if (!fs.existsSync(overlay.src)) throw new Error(`PACKAGING_VISUAL_MISSING: ${overlay.src}`);
    const ext = path.extname(overlay.src) || ".png";
    const staged = `visual_${index}${ext}`;
    fs.copyFileSync(overlay.src, path.join(work, staged));
    const image = IMG_EXT.test(ext);
    inputs.push(...(image ? ["-loop", "1", "-i", staged] : ["-stream_loop", "-1", "-i", staged]));
    const width = Math.round(overlay.scale * W);
    const x = Math.round(overlay.x * W);
    const y = Math.round(overlay.y * H);
    const fade=overlay.entrance !== "none" ? `,fade=t=in:st=${overlay.start}:d=0.1:alpha=1,fade=t=out:st=${Math.max(overlay.start,overlay.end-.08)}:d=0.08:alpha=1` : "";
    const positionY=overlay.entrance === "slide" ? `${y}+if(lt(t,${overlay.start+.14}),${H*.03}*(1-(t-${overlay.start})/.14),0)` : String(y);
    parts.push(`[${nextInputIdx}:v]scale=${width}:-1,format=rgba${fade},colorchannelmixer=aa=${overlay.opacity}[pack${index}]`);
    parts.push(`[${videoBase}][pack${index}]overlay=x=${x}:y='${positionY}':enable='between(t,${overlay.start},${overlay.end})'[packout${index}]`);
    videoBase = `packout${index}`;
    nextInputIdx++;
  }
  parts.push(`[${videoBase}]ass=sub.ass:fontsdir=.[vout]`);

  const args = ["-y", ...inputs, "-filter_complex", parts.join(";"),
    "-map", "[vout]", "-map", audioMap, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
    "-crf", String(profile.crf ?? 23), "-c:a", "aac", "-ar", "48000", "-b:a", "192k", "-shortest",
    ...(maxDuration === undefined ? [] : ["-t", String(maxDuration)]),
    "-movflags", "+faststart", "_out.mp4"];

  console.log(`▶ 渲染 ${name}（${ar.ratio} ${W}x${H} | ${n} 段 | xfade ${T}s | ${bgm ? "配音+BGM" : "仅配音"}）...`);
  execFileSync("ffmpeg", args, { cwd: work, stdio: "inherit" });

  if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });
  const outFile = explicitOutput ? path.resolve(explicitOutput) : path.join(OUT, name + ".mp4");
  fs.mkdirSync(path.dirname(outFile), { recursive: true });
  // Copying the finished MP4 with fs.copyFileSync can produce an unreadable
  // destination on this Windows setup. Remux directly into the destination so
  // ffmpeg writes and finalizes the container (including its moov atom).
  execFileSync("ffmpeg", ["-y", "-i", "_out.mp4", "-c", "copy", "-movflags", "+faststart", outFile],
    { cwd: work, stdio: "inherit" });
  // 不删 work 临时目录 —— safe-delete 守卫会 abort 进程，try-catch 拦不住。系统重启时自然清理。
  console.log(`✔ 成片 → ${outFile}`);
}

main();
