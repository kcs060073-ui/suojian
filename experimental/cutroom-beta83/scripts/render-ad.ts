import { appendRefinedLayers } from "./refined-layers";
import { applySubtitleAppearance, SubtitleAppearance } from "../src/subtitle-appearance";
import { escapeAssText, protectedSpans } from "../src/subtitle-ass";
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
import { AUDIO_MIX_POLICY, OUTPUT_AUDIO_RATE } from "../src/audio-mix-policy";
import * as path from "path";
import * as os from "os";
import { execFileSync } from "child_process";
import { AD_SUBTITLE } from "./ad-style";
import { assertOutputDirection, assertSourceDirection } from "./media-display-geometry";
import { resolveAdRecipePayload } from "./render-recipe";
import { renderFullAudio } from "./render-full-audio";
import type { MixSfx } from "../src/types";
import { projectAdPreviewWindow } from "./ad-preview-window";

const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(ROOT, "out");
const FONTS = path.join(ROOT, "assets", "fonts");

const T = 0.4; // 转场时长(s)
const IMG_EXT = /\.(png|jpe?g|webp)$/i;

// 探素材真实时长（秒）；探不到返回 0
function probeDur(file: string): number {
  try {
    const o = execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file], { encoding: "utf8" });
    return parseFloat(o.trim()) || 0;
  } catch { return 0; }
}
// 剪映“号”→像素，首版估值，渲后按观感微调
const FONT_PX: Record<"h" | "v", number> = { h: 52, v: 70 };
const MARGIN_V: Record<"h" | "v", number> = { h: 90, v: 620 }; // 字幕距底(px)；竖版 620=抖音安全区（避开底部 UI 遮挡）
const SHADOW: Record<"h" | "v", number> = { h: 4, v: 5 };
const MAX_CHARS: Record<"h" | "v", number> = { h: 30, v: 13 }; // 每行最多字数，超了手动断行

// libass 对无空格中文不自动换行，超长手动插 \N：尽量在标点处断，否则硬断
function wrapText(t: string, max: number, terms: string[] = []): string {
  if (t.length <= max) return t;
  const punct = "，。！？、；：,.!?;:";
  const lines: string[] = [];
  let rest = t;
  while (rest.length > max) {
    let cut = -1;
    for (let i = Math.min(max, rest.length - 1); i >= Math.ceil(max * 0.5); i--) {
      if (punct.includes(rest[i])) { cut = i + 1; break; }
    }
    if (cut < 0) cut = max;
    // Decimal points are not sentence boundaries. Keep numbers with their
    // units and every keyword occurrence intact, as in digital subtitles.
    const span = protectedSpans(rest, terms).find(
      (candidate) => candidate.start < cut && cut < candidate.end,
    );
    if (span) cut = span.start > 0 ? span.start : span.end;
    lines.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest) lines.push(rest);
  return lines.join("\\N");
}

interface SubtitleSegment { text: string; start: number; end: number; displayText?: string; highlightTerms?: string[] }
interface RSeg { i: number; text: string; start: number; end: number; dur: number; src: string | null; matched: boolean; sourceStart?: number; sourceEnd?: number; segmentId?: string | null; highlightTerms?: string[] }
interface AdSubtitlePreset {
  id: string; fontname_guess: string; size: number; primary_hex: string; highlight_hex?: string;
  outline_hex?: string; shadow_hex?: string; back_hex?: string; back_alpha_hex?: string;
  bold: boolean; alignment: number; margin_v: number; outline?: number; shadow?: number;
  border_style: number; spacing?: number; max_chars_per_line?: number;
  fade_in_ms?: number; fade_out_ms?: number; ass_style?: string;
}
interface AdOutputProfile { maxDuration?: number; width?: number; height?: number; crf?: number }
interface AdRender {
  subtitleSegments?: SubtitleSegment[];
  name: string; ratio: string; total: number; voice: string | { src: string; presetId?: string }; segments: RSeg[];
  subtitlePreset?: AdSubtitlePreset; output?: AdOutputProfile;
  subtitleHighlight?: boolean;
  subtitleAppearance?: SubtitleAppearance;
  bgm?: { src: string; gain?: number; duck?: boolean };
  sfx?: MixSfx[];
  titles?: Array<{ designVersion?: string; contentLayout?: any; role?: string; text: string; start: number; end: number; x?: number; y?: number; scale?: number; presetId?: string; style?: { fontFamily?: string; primaryHex?: string; backgroundHex?: string } }>;
  stickers?: Array<{ src?: string; start: number; end: number; x?: number; y?: number; scale?: number; opacity?: number; entrance?: string }>;
  fancyTexts?: Array<{ designVersion?: string; contentLayout?: any; role?: string; text: string; presetId?: string; start: number; end: number; x?: number; y?: number; scale?: number; opacity?: number; entrance?: string; style?: { fontFamily?: string; primaryHex?: string; outlineHex?: string; backgroundHex?: string; glowHex?: string | null } }>;
  watermark?: { src: string; opacity?: number; position?: { x?: number; y?: number; width?: number } };
}

interface RenderArgs {
  name: string;
  bgm?: string;
  explicitPlan?: string;
  explicitOutput?: string;
  audioReviewOutput?: string;
  subtitleOutput?: string;
  maxDuration?: number;
  width?: number;
  height?: number;
  crf?: number;
  previewWindow?: { start: number; end: number };
}

function parseArgs(argv: string[]): RenderArgs {
  const explicit = argv.some((value) => value.startsWith("--"));
  if (!explicit) {
    const [name, bgm, ...extra] = argv;
    if (!name || extra.length) throw new Error("用法: ts-node scripts/render-ad.ts <name> [bgm路径]");
    return { name, bgm };
  }
  const allowed = new Set(["--plan", "--output", "--bgm", "--max-duration", "--width", "--height", "--crf", "--preview-start", "--preview-end", "--audio-review-output", "--subtitle-output"]);
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
  const previewStart = numeric("--preview-start");
  const previewEnd = numeric("--preview-end");
  if ((previewStart === undefined) !== (previewEnd === undefined)
    || (previewStart !== undefined && previewEnd !== undefined
      && (previewStart < 0 || previewEnd <= previewStart || maxDuration !== undefined))) {
    throw new Error("AD_PREVIEW_WINDOW_INVALID: 必须提供有效窗口起止且不能与时长上限并用");
  }
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
    audioReviewOutput: values.get("--audio-review-output"),
    subtitleOutput: values.get("--subtitle-output"),
    maxDuration,
    width,
    height,
    crf,
    ...(previewStart === undefined ? {} : { previewWindow: { start: previewStart, end: previewEnd! } }),
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

function emphasizeAdText(text: string, terms: string[], color: string, primary: string): string {
  const spans: Array<{ start: number; end: number }> = [];
  for (const term of terms.filter((value) => value && !/[{}\\]/u.test(value))) {
    let from = 0;
    for (let index = text.indexOf(term, from); index >= 0; index = text.indexOf(term, from)) {
      spans.push({ start: index, end: index + term.length });
      from = index + term.length;
    }
  }
  for (const match of text.matchAll(/\d+(?:[.．]\d+)?(?:多)?(?:亿|万|千|百|平方米|平米|万元|亿元|元|亩|人|家|%)/gu)) {
    spans.push({ start: match.index, end: match.index + match[0].length });
  }
  spans.sort((a, b) => a.start - b.start || b.end - a.end);
  let cursor = 0;
  let result = "";
  for (const span of spans) {
    if (span.start < cursor) continue;
    result += text.slice(cursor, span.start)
      + `{\\c${color}}${text.slice(span.start, span.end)}{\\c${primary}}`;
    cursor = span.end;
  }
  return result + text.slice(cursor);
}

function buildAss(
  segs: SubtitleSegment[], W: number, H: number, o: "h" | "v",
  preset?: AdSubtitlePreset, titles: AdRender["titles"] = [],
  fancyTexts: AdRender["fancyTexts"] = [],
  highlightEnabled = false,
  appearance?: SubtitleAppearance,
): string {
  const st = AD_SUBTITLE[o];
  const font = preset?.fontname_guess ?? st.fontFamily[0];
  const titleStyle = titles[0]?.style;
  const fancyStyle = fancyTexts[0]?.style;
  const titleFont = titleStyle?.fontFamily || font;
  const fancyFont = fancyStyle?.fontFamily || font;
  const titlePrimary = assColor(titleStyle?.primaryHex || "#FFFFFF");
  const titleBack = assColor(titleStyle?.backgroundHex || "#000000", 64);
  const titleStyles = (titles ?? []).map((cue, index) => {
    const cuePrimary = assColor(cue.style?.primaryHex || "#FFFFFF");
    const cueBack = assColor(cue.style?.backgroundHex || "#000000", 64);
    return `Style: PackTitle${index},${cue.style?.fontFamily || font},${Math.max(28, Math.round(W * .072))},${cuePrimary},${cuePrimary},${cueBack},${cueBack},-1,0,0,0,100,100,1,0,3,8,0,8,40,40,${Math.round(H * .08)},1`;
  }).join("\n");
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
    `${titleStyles}\n` +
    `Style: PackFancy,${fancyFont},${Math.max(24, Math.round(W * 0.06))},${fancyPrimary},${fancyPrimary},${fancyOutline},${fancyBack},-1,0,0,0,100,100,1,0,1,2,2,7,40,40,${Math.round(H * 0.18)},1\n\n` +
    `[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n`;
  const fade = preset && (preset.fade_in_ms || preset.fade_out_ms)
    ? `{\\fad(${preset.fade_in_ms ?? 0},${preset.fade_out_ms ?? 0})}` : "";
  const lines = segs.map((s) => {
    const wrapped = s.displayText === undefined
      ? wrapText(s.text.replace(/\n/g, " ").trim(), preset?.max_chars_per_line ?? MAX_CHARS[o], s.highlightTerms || [])
      : escapeAssText(s.displayText).replace(/\r\n?|\n/g, "\\N");
    const text = highlightEnabled
      ? wrapped.split("\\N").map((line) => emphasizeAdText(
        line, s.highlightTerms || [], assColor(preset?.highlight_hex || "#FD9437"), primary,
      )).join("\\N") : wrapped;
    return `Dialogue: 0,${cs(s.start)},${cs(s.end)},Default,,0,0,0,,${fade}${text}`;
  });
  for (const [index, cue] of (titles ?? []).entries()) {
    const positioned = cue.x === undefined || cue.y === undefined ? "" :
      `{\\an7\\pos(${Math.round(cue.x * W)},${Math.round(cue.y * H)})` +
      `\\fs${Math.max(24, Math.round(W * .072 * (cue.scale ?? .84) / .84))}}`;
    lines.push(`Dialogue: 1,${cs(cue.start)},${cs(cue.end)},PackTitle${index},,0,0,0,,${positioned}${escapeAssText(cue.text).replace(/\r\n?|\n/g, "\\N")}`);
  }
  for (const cue of fancyTexts ?? []) {
    lines.push(`Dialogue: 2,${cs(cue.start)},${cs(cue.end)},PackFancy,,0,0,0,,{\\fad(180,180)}${escapeAssText(cue.text).replace(/\r\n?|\n/g, "\\N")}`);
  }
  return applySubtitleAppearance(head + lines.join("\n") + "\n", appearance, W, H);
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
  const source: AdRender = resolveAdRecipePayload(
    JSON.parse(fs.readFileSync(arPath, "utf8"))
  );
  const profile: AdOutputProfile = { ...(source.output ?? {}) };
  const previewWindow = cli.previewWindow;
  if (cli.maxDuration !== undefined) profile.maxDuration = cli.maxDuration;
  if (cli.width !== undefined) profile.width = cli.width;
  if (cli.height !== undefined) profile.height = cli.height;
  if (cli.crf !== undefined) profile.crf = cli.crf;
  if (previewWindow && profile.maxDuration !== undefined) {
    throw new Error("AD_PREVIEW_WINDOW_INVALID: 冻结预览窗口不能与全片截短配置并用");
  }
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
  const bgm = cli.bgm ?? ar.bgm?.src;

  const missing = ar.segments.filter((s) => !s.src);
  if (missing.length) {
    throw new Error(`AD_SCENE_ASSET_REQUIRED: ${missing.map((segment) => segment.i).join(", ")}`);
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
  const lockedVoiceEnd = source.segments.at(-1)?.end;
  if (source.total !== undefined && (
    !Number.isFinite(source.total) || source.total <= 0
    || typeof lockedVoiceEnd !== "number"
    || Math.abs(source.total - lockedVoiceEnd) > 0.03
  )) {
    throw new Error("AD_TIMELINE_MISMATCH: 计划总时长与锁定镜头终点不一致");
  }
  const measuredVoiceDuration = probeDur(voiceAbs);
  if (typeof lockedVoiceEnd !== "number" || !Number.isFinite(lockedVoiceEnd)
    || measuredVoiceDuration <= 0
    || Math.abs(measuredVoiceDuration - lockedVoiceEnd) > 0.1) {
    throw new Error("AD_VOICE_TIMING_MISMATCH: 配音实测时长与锁定时间线不一致，不能截断或补时长");
  }
  const projected = previewWindow
    ? projectAdPreviewWindow(ar.segments, previewWindow.start, previewWindow.end, T)
    : undefined;
  const renderSegments = projected?.segments ?? ar.segments;

  if (ar.ratio !== "9:16" && ar.ratio !== "16:9")
    throw new Error(`AD_ASPECT_RATIO_INVALID: ${ar.ratio}`);
  assertOutputDirection(W, H, ar.ratio);
  for (const segment of ar.segments) {
    const src = segment.src!;
    const sourcePath = path.isAbsolute(src) ? src : path.join(ROOT, src);
    if (!fs.existsSync(sourcePath)) throw new Error(`AD_SCENE_ASSET_OFFLINE: segment ${segment.i}`);
    assertSourceDirection(sourcePath, ar.ratio, `segment ${segment.i}`,
      IMG_EXT.test(sourcePath) ? "image" : "video");
  }

  // 临时 ASCII 工作目录，规避中文路径
  const work = fs.mkdtempSync(path.join(os.tmpdir(), `ad_${name}_`));
  const n = renderSegments.length;
  // Selected in/out points describe the narration interval; the source must
  // also have real frames after out for the transition handle.
  for (const segment of ar.segments) {
    if (segment.sourceStart === undefined && segment.sourceEnd === undefined) continue;
    const start = segment.sourceStart, end = segment.sourceEnd;
    if (start === undefined || end === undefined || !Number.isFinite(start) || !Number.isFinite(end)
      || start < 0 || end < start) {
      throw new Error(`AD_SOURCE_WINDOW_INVALID: segment ${segment.i}`);
    }
    if (!IMG_EXT.test(segment.src ?? "") && end - start + 1e-6 < segment.dur) {
      throw new Error(`AD_SOURCE_WINDOW_TOO_SHORT: segment ${segment.i} needs ${segment.dur.toFixed(3)}s narration`);
    }
  }
  // 每段真实视频必须覆盖播出区间及转场把手，图片可以保持静帧。
  const segInputs: string[][] = [];
  renderSegments.forEach((s, i) => {
    const L = s.dur + T; // 该段需要的画面时长（含转场重叠料）
    if (!s.src) throw new Error(`AD_SCENE_ASSET_REQUIRED: segment ${s.i}`);
    const ext = path.extname(s.src) || ".mp4";
    const dst = `clip_${String(i).padStart(2, "0")}${ext}`;
    const source = path.isAbsolute(s.src) ? s.src : path.join(ROOT, s.src);
    if (!fs.existsSync(source)) throw new Error(`AD_SCENE_ASSET_OFFLINE: segment ${s.i}`);
    if (!previewWindow || IMG_EXT.test(ext)) fs.copyFileSync(source, path.join(work, dst));
    if (IMG_EXT.test(ext)) {
      segInputs.push(["-loop", "1", "-i", dst]); // 图片：无限帧，trim 到 L（避免只剩 1 帧导致段崩）
    } else {
      const rd = probeDur(previewWindow ? source : path.join(work, dst));
      const requiredEnd = Math.max(s.sourceEnd ?? 0, (s.sourceStart ?? 0) + L);
      if (rd <= 0 || requiredEnd > rd + 0.01) {
        throw new Error(`AD_SOURCE_WINDOW_OUT_OF_RANGE: segment ${s.i} needs source through ${requiredEnd.toFixed(3)}s; available ${rd.toFixed(3)}s`);
      }
      segInputs.push(previewWindow
        ? ["-ss", String(s.sourceStart ?? 0), "-t", String(L + .2), "-i", source]
        : ["-i", dst]);
    }
  });
  try {
  const fullAudio = renderFullAudio({
    voice: voiceAbs, speed: 1, duration: source.total, defaultGain: .12,
    bgm: bgm ? { ...source.bgm, src: path.resolve(ROOT, bgm) } : undefined,
    sfx: (source.sfx ?? []).map((effect) => ({ ...effect, src: path.resolve(ROOT, effect.src) })),
    reviewOutput: cli.audioReviewOutput,
  }, work);
  // ASS stays on the full output canvas; libass scales text and margins for previews.
  // 字体拷进工作目录，ass 用 fontsdir=. 找
  for (const f of fs.readdirSync(FONTS)) if (/\.(otf|ttf|ttc)$/i.test(f)) fs.copyFileSync(path.join(FONTS, f), path.join(work, f));
  fs.writeFileSync(
    path.join(work, "sub.ass"),
    buildAss((ar.subtitlePreset as any)?.design_version === "refined-v1" ? [] : ar.subtitleSegments ?? ar.segments, defaultW, defaultH, o, ar.subtitlePreset, (ar.titles ?? []).filter(cue => cue.designVersion !== "refined-v1"),
      (ar.fancyTexts ?? []).filter((cue) => !cue.presetId),
      ar.subtitleHighlight === true, ar.subtitleAppearance),
    "utf8",
  );

  // filter_complex：每段 scale+crop+trim（延长 T 给转场重叠），再链式 xfade，最后烧字幕
  const parts: string[] = [];
  renderSegments.forEach((s, i) => {
    const L = (s.dur + T).toFixed(3); // 延长 T 作转场重叠料
    const window = !previewWindow && s.sourceStart !== undefined && s.sourceEnd !== undefined && !IMG_EXT.test(s.src ?? "")
      ? `trim=start=${s.sourceStart.toFixed(3)}:end=${(s.sourceStart + s.dur + T).toFixed(3)},setpts=PTS-STARTPTS,` : "";
    parts.push(`[${i}:v]${window}scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H},setsar=1,fps=30,trim=duration=${L},setpts=PTS-STARTPTS[v${i}]`);
  });
  let prev = "v0";
  if (n === 1) {
    parts.push(`[v0]null[vbase]`);
  } else {
    let off = renderSegments[0].dur; // 首段 offset = D0 - T = (d0+T) - T = d0
    for (let i = 1; i < n; i++) {
      const out = i === n - 1 ? "vcat" : `vt${i}`;
      parts.push(`[${prev}][v${i}]xfade=transition=fade:duration=${T}:offset=${off.toFixed(3)}[${out}]`);
      off += renderSegments[i].dur; // 增量 = D_i - T = d_i
      prev = out;
    }
    parts.push(`[vcat]null[vbase]`);
  }

  // Both preview and final video read the same complete-timeline mix.
  const inputs: string[] = [];
  segInputs.forEach((a) => inputs.push(...a));
  inputs.push(...(previewWindow ? ["-ss", String(previewWindow.start)] : []),
    "-i", fullAudio);
  let nextInputIdx = n + 1;
  parts.push(`[${n}:a]asetpts=PTS-STARTPTS[aout]`);
  let audioMap = "[aout]";

  let videoBase = "vbase";
  if (projected) {
    parts.push(`[vbase]setpts=PTS+${projected.timelineStart}/TB[vglobal]`);
    videoBase = "vglobal";
  }
  const overlays = [
    ...(ar.watermark?.src ? [{
      src: ar.watermark.src,
      start: 0,
      end: ar.total,
      x: ar.watermark.position?.x ?? 0.04,
      y: ar.watermark.position?.y ?? 0.02,
      scale: ar.watermark.position?.width ?? 0.28,
      opacity: ar.watermark.opacity ?? 1,
      entrance: 'none',
    }] : []),
    ...(ar.stickers ?? []).filter((item) => Boolean(item.src) &&
      (!previewWindow || item.end > previewWindow.start && item.start < previewWindow.end)).map((item) => ({
      src: item.src!, start: item.start, end: item.end, x: item.x ?? 0.72,
      y: item.y ?? 0.12, scale: item.scale ?? 0.2, opacity: item.opacity ?? 1,
      entrance: item.entrance ?? 'none',
    })),
  ];
  for (const [index, overlay] of overlays.entries()) {
    if (overlay.start >= ar.total) continue;
    const overlayEnd = Math.min(overlay.end, ar.total);
    if (!fs.existsSync(overlay.src)) throw new Error(`PACKAGING_VISUAL_MISSING: ${overlay.src}`);
    if (!Number.isFinite(overlay.start) || !Number.isFinite(overlay.end)
      || overlay.start < 0 || overlayEnd <= overlay.start) {
      throw new Error(`PACKAGING_VISUAL_TIME_INVALID: visual ${index}`);
    }
    const ext = path.extname(overlay.src) || ".png";
    const staged = `visual_${index}${ext}`;
    fs.copyFileSync(overlay.src, path.join(work, staged));
    const image = IMG_EXT.test(ext);
    if (!image && probeDur(path.join(work, staged)) + 0.01 < overlayEnd - overlay.start) {
      throw new Error(`PACKAGING_VISUAL_WINDOW_OUT_OF_RANGE: visual ${index} needs ${(overlayEnd - overlay.start).toFixed(3)}s`);
    }
    inputs.push(...(image ? ["-loop", "1", "-i", staged] : ["-i", staged]));
    const width = Math.round(overlay.scale * W);
    const x = Math.round(overlay.x * W);
    const y = Math.round(overlay.y * H);
    const fadeIn = image && overlay.entrance !== 'none' ? `,fade=t=in:st=${overlay.start}:d=0.25:alpha=1` : '';
    const overlayX = overlay.entrance === 'slide'
      ? `'if(lt(t,${overlay.start + .25}),${x}-${width}*(1-max(0,(t-${overlay.start})/.25)),${x})'` : String(x);
    parts.push(`[${nextInputIdx}:v]${image ? "" : `setpts=PTS-STARTPTS+${overlay.start}/TB,`}scale=${width}:-1,format=rgba${fadeIn},colorchannelmixer=aa=${overlay.opacity}[pack${index}]`);
    parts.push(`[${videoBase}][pack${index}]overlay=${overlayX}:${y}:enable='between(t,${overlay.start},${overlayEnd})'[packout${index}]`);
    videoBase = `packout${index}`;
    nextInputIdx++;
  }
  // Preset-backed workbench fancy text uses the same transparent graphic and
  // normalized geometry as render-mix, including gradients, glow and entrance.
  for (const [index, cue] of (ar.fancyTexts ?? []).entries()) {
    if (cue.designVersion === "refined-v1" || !cue.presetId || cue.start >= ar.total || (previewWindow &&
      (cue.end <= previewWindow.start || cue.start >= previewWindow.end))) continue;
    const end = Math.min(cue.end, ar.total);
    if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end)
      || cue.start < 0 || end <= cue.start)
      throw new Error(`PACKAGING_VISUAL_TIME_INVALID: fancy ${index}`);
    const png = path.join(work, `fancy_${index}.png`);
    execFileSync(path.join(ROOT, ".venv", "Scripts", "python.exe"), [
      path.join(ROOT, "scripts", "gen-fancy-text.py"), "--text", cue.text,
      "--preset-id", cue.presetId, "--out", png, "--base-size", "200",
    ], { cwd: ROOT, stdio: "pipe" });
    inputs.push("-loop", "1", "-framerate", "30", "-t", String(end), "-i", png);
    const width = Math.round((cue.scale ?? .25) * W);
    const x = Math.round((cue.x ?? .04) * W), y = Math.round((cue.y ?? .04) * H);
    const entrance = cue.entrance ?? "fade", t0 = cue.start.toFixed(3), t1 = (cue.start + .25).toFixed(3);
    const scaling = ["scale", "pop", "bounce"].includes(entrance)
      ? `scale='if(between(t,${t0},${t1}),${width}*(0.78+0.22*(t-${t0})/0.25),${width})':-1:eval=frame`
      : `scale=${width}:-1`;
    parts.push(`[${nextInputIdx}:v]${scaling},format=rgba${entrance === "none" ? "" :
      `,fade=t=in:st=${t0}:d=0.25:alpha=1`},colorchannelmixer=aa=${cue.opacity ?? 1}[fancy${index}]`);
    const overlayX = entrance === "slide"
      ? `'if(lt(t,${t1}),${x}-${width}*(1-max(0,(t-${t0})/0.25)),${x})'` : String(x);
    parts.push(`[${videoBase}][fancy${index}]overlay=${overlayX}:${y}:enable='between(t,${cue.start},${end})'[fancyout${index}]`);
    videoBase = `fancyout${index}`;
    nextInputIdx++;
  }
  const refinedCues: any[] = [...(ar.titles ?? []),...(ar.fancyTexts ?? [])].filter(cue => cue.designVersion === "refined-v1");
  if ((ar.subtitlePreset as any)?.design_version === "refined-v1") refinedCues.push(...(ar.subtitleSegments ?? ar.segments).map(cue => ({...cue, text: 'displayText' in cue ? cue.displayText : cue.text, role:"subtitle", highlight:ar.subtitleHighlight === true})).filter(cue => cue.text));
  const refined = appendRefinedLayers({root:ROOT,work,width:W,height:H,cues:refinedCues.filter(cue=>!previewWindow || cue.end>previewWindow.start && cue.start<previewWindow.end),
    appearance:ar.subtitleAppearance,inputs,parts,inputIndex:nextInputIdx,videoBase});
  nextInputIdx=refined.inputIndex; videoBase=refined.videoBase;
  parts.push(`[${videoBase}]ass=sub.ass:fontsdir=.[${previewWindow ? "fullv" : "vout"}]`);
  if (previewWindow) {
    parts.push(`[fullv]trim=start=${previewWindow.start}:end=${previewWindow.end},setpts=PTS-STARTPTS[vout]`);
    parts.push(`[aout]atrim=duration=${previewWindow.end - previewWindow.start},asetpts=PTS-STARTPTS[windowa]`);
    audioMap = "[windowa]";
  }

  const args = ["-y", ...inputs, "-filter_complex", parts.join(";"),
    "-map", "[vout]", "-map", audioMap, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
    "-crf", String(profile.crf ?? 23), "-c:a", "aac", "-ar", String(OUTPUT_AUDIO_RATE), "-b:a", AUDIO_MIX_POLICY.bitrate, "-shortest",
    ...(previewWindow ? ["-t", String(previewWindow.end - previewWindow.start)]
      : maxDuration === undefined ? [] : ["-t", String(maxDuration)]),
    "-movflags", "+faststart", "_out.mp4"];

  console.log(`▶ 渲染 ${name}（${ar.ratio} ${W}x${H} | ${n} 段 | xfade ${T}s | ${bgm ? "配音+BGM" : "仅配音"}）...`);
  execFileSync("ffmpeg", args, { cwd: work, stdio: "inherit" });

  if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });
  const outFile = explicitOutput ? path.resolve(explicitOutput) : path.join(OUT, name + ".mp4");
  fs.mkdirSync(path.dirname(outFile), { recursive: true });
  fs.copyFileSync(path.join(work, "_out.mp4"), outFile);
  if (cli.subtitleOutput) {
    const subtitleFile = path.resolve(cli.subtitleOutput);
    fs.mkdirSync(path.dirname(subtitleFile), { recursive: true });
    fs.copyFileSync(path.join(work, "sub.ass"), subtitleFile);
  }
  // 不删 work 临时目录 —— safe-delete 守卫会 abort 进程，try-catch 拦不住。系统重启时自然清理。
  console.log(`✔ 成片 → ${outFile}`);
  } finally {
    // These two generated PCM files can be large; never retain them on failure.
    for (const name of ["full-mix.wav", "review-stems.wav"]) {
      const generated = path.join(work, name);
      if (fs.existsSync(generated)) fs.unlinkSync(generated);
    }
  }
}

main();
