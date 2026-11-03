import { appendRefinedLayers, assHighlightTerms } from "./refined-layers";
import { applySubtitleAppearance } from "../src/subtitle-appearance";
import { escapeAssText } from "../src/subtitle-ass";
/**
 * P1 统一引擎：把过去裂开的两条管线（画中画插入 / 粗宋橘黄字幕）合并成「一条时间轴、一次 filter_complex 烧成片」。
 *
 * 一次渲染同时落实：
 *   · 1.25 倍速（视频 setpts + 音频 atempo 保持音高）
 *   · 思源粗宋·重点橘黄字幕（烧录 make-ass 产出的 <name>.ass）
 *   · 全屏 B-roll 空镜临时盖住素体（type=broll）
 *   · 画中画小图/小视频（type=image|video，按相对 position 摆放）
 *   · 开场/中插标题卡（titles：顶部大字，可压在空镜上）
 *
 * 输入:
 *   public/digital_human/<name>.mp4
 *   public/scripts/<name>.ass          （可选；有则烧字幕，make-ass 产出）
 *   public/scripts/<name>.plan.json    （可选；MixPlan，描述插入与标题）
 *   public/inserts/<src>               （插入/标题用到的素材）
 * 输出:
 *   out/<name>.mp4
 *
 * 用法: ts-node scripts/render-mix.ts <name> [speed]
 *   speed 命令行优先；否则取 plan.speed；再否则 1.25
 *
 * 时间轴约定：plan.json 与 .ass 里的所有时间都是「最终成片秒数」（已含倍速换算）。
 *   素体经 setpts=PTS/speed 提速后，overlay 的半开显示区间 [start,end) 使用这条最终时间轴，三者对齐。
 */
import * as fs from "fs";
import { AUDIO_MIX_POLICY, OUTPUT_AUDIO_RATE } from "../src/audio-mix-policy";
import * as path from "path";
import * as os from "os";
import { execFileSync } from "child_process";
import { renderFullAudio } from "./render-full-audio";
import type { MixPlan, MixInsert, MixTitle, InsertPosition, MixSticker, MixFancyText } from "../src/types";
import { formatRenderMixTaskPaths, parseRenderMixOptions, resolveRenderMixAsset, withRenderMixWorkdir } from "./render-mix-options";
import { assertOutputDirection, assertSourceDirection } from "./media-display-geometry";
import { resolveDigitalRecipePayload } from "./digital-render-recipe";

const ROOT = path.resolve(__dirname, "..");
const FONT_FILE = path.join(ROOT, "assets", "fonts", "SourceHanSerifSC-Heavy.otf");
const FONT_NAME = "Source Han Serif SC Heavy";

let CANVAS_W = 1080;
let CANVAS_H = 1920;
const COLOR_WHITE = "&H00FFFFFF&";
const TITLE_SIZE = 78; // 标题卡字号（顶部）

type TitlePreset = {
  id: string;
  font?: string;
  fontname_guess?: string;
  background_hex?: string;
  accent_hex?: string;
};

function assColor(hex: string | undefined, fallback: string): string {
  if (!hex) return fallback;
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!match) throw new Error(`标题卡颜色无效: ${hex}`);
  return `&H00${match[3]}${match[2]}${match[1]}&`.toUpperCase();
}

function resolveTitlePreset(presetId: string): TitlePreset {
  const manifestPath = path.join(ROOT, "assets", "manifests", "visual-cards.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const preset = (manifest.items || []).find((item: TitlePreset) => item.id === presetId);
  if (!preset) throw new Error(`PACKAGING_TITLE_PRESET_UNKNOWN: ${presetId}`);
  return preset;
}

function fmtAssTime(t: number): string {
  if (t < 0) t = 0;
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = Math.floor(t % 60);
  const cs = Math.round((t - Math.floor(t)) * 100);
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(Math.min(cs, 99)).padStart(2, "0")}`;
}

// 全屏铺满 9:16：缩放到不留黑边再中心裁切
function fullscreenPrep(inLabel: string, isVideo: boolean, start: number, end: number, sourceStart: number, sourceEnd: number, out: string): string {
  const scaleCrop = `scale=${CANVAS_W}:${CANVAS_H}:force_original_aspect_ratio=increase,crop=${CANVAS_W}:${CANVAS_H},setsar=1`;
  if (isVideo) {
    // 视频：裁到时长并把 PTS 平移到 start 出现
    return `[${inLabel}]trim=start=${sourceStart.toFixed(3)}:end=${sourceEnd.toFixed(3)},setpts=PTS-STARTPTS+${start}/TB,${scaleCrop}[${out}]`;
  }
  return `[${inLabel}]${scaleCrop}[${out}]`;
}

// 画中画：按相对 position 缩放到目标小窗
function pipPrep(inLabel: string, isVideo: boolean, pos: InsertPosition, start: number, end: number, sourceStart: number, sourceEnd: number, out: string): string {
  const W = Math.round(pos.width * CANVAS_W);
  const H = Math.round(pos.height * CANVAS_H);
  if (isVideo) {
    return `[${inLabel}]trim=start=${sourceStart.toFixed(3)}:end=${sourceEnd.toFixed(3)},setpts=PTS-STARTPTS+${start}/TB,scale=${W}:${H},setsar=1[${out}]`;
  }
  return `[${inLabel}]scale=${W}:${H},setsar=1[${out}]`;
}

function sourceWindow(ins: MixInsert, isVideo: boolean): { sourceStart: number; sourceEnd: number } {
  if (!Number.isFinite(ins.start) || !Number.isFinite(ins.end) || ins.start < 0 || ins.end <= ins.start) {
    throw new Error(`MixInsert timeline is invalid for ${ins.src}`);
  }
  const duration = ins.end - ins.start;
  const sourceStart = ins.sourceStart ?? 0;
  const sourceEnd = ins.sourceEnd ?? (isVideo ? sourceStart + duration : 0);
  if (!Number.isFinite(sourceStart) || !Number.isFinite(sourceEnd) || sourceStart < 0 || sourceEnd < 0) {
    throw new Error(`MixInsert source window must contain finite non-negative values for ${ins.src}`);
  }
  if (!isVideo) {
    if (sourceStart !== 0 || sourceEnd !== 0) {
      throw new Error(`Image MixInsert source window must be 0/0 for ${ins.src}`);
    }
    return { sourceStart: 0, sourceEnd: 0 };
  }
  if (sourceEnd <= sourceStart || sourceEnd - sourceStart + 1e-9 < duration) {
    throw new Error(`MixInsert source window is reversed or shorter than its timeline slot for ${ins.src}`);
  }
  return { sourceStart, sourceEnd };
}

function main() {
  let options;
  try {
    options = parseRenderMixOptions(process.argv.slice(2), ROOT);
  } catch (error) {
    console.error((error as Error).message);
    console.error("用法: ts-node scripts/render-mix.ts <name> [speed] [--input <mp4>] [--plan <json>] [--ass <ass>] [--output <mp4>] [--asset-root <dir>] [--stickers <json>] [--fancy-texts <json>] [--dry-run]");
    process.exit(1);
  }
  const { name, inputVideo: video, assFile, planFile, outputVideo: outPath, assetSearchRoots, stickersJson: STICKERS_JSON, fancyTextsJson: FANCY_JSON, dryRun: DRY_RUN, previewWindow } = options;

  if (!fs.existsSync(video)) {
    console.error(`缺素体: ${video}`);
    process.exit(1);
  }

  const resolvedPlan = resolveDigitalRecipePayload(
    fs.existsSync(planFile) ? JSON.parse(fs.readFileSync(planFile, "utf8")) : {},
    video, assFile,
  );
  const plan: MixPlan = resolvedPlan.plan;
  const recipe = resolvedPlan.recipe;
  if (recipe && options.speed !== undefined && options.speed !== 1.25) {
    throw new Error("RECIPE_SPEED_MISMATCH: 冻结数字人配方只允许1.25倍速");
  }
  if (recipe && (STICKERS_JSON || FANCY_JSON)) {
    throw new Error("RECIPE_PACKAGING_OVERRIDE: 冻结配方不能用命令行覆盖包装实例");
  }
  const outputProfile = { ...(plan.output || {}), ...options.outputProfile };
  if (previewWindow && (outputProfile.maxDuration !== undefined ||
      (recipe && previewWindow.end > recipe.duration_us / 1_000_000 + 1e-6))) {
    throw new Error("PREVIEW_WINDOW_OUT_OF_RANGE: 预览窗口超出冻结时间轴或与时长上限冲突");
  }
  CANVAS_W = outputProfile.width ?? 1080;
  CANVAS_H = outputProfile.height ?? 1920;
  assertOutputDirection(CANVAS_W, CANVAS_H, "9:16");
  const speed = options.speed || plan.speed || 1.25;
  const inserts: MixInsert[] = plan.inserts ?? [];
  const refinedCues: any[] = [...(plan.titles ?? []),...(plan.fancyTexts ?? [])].filter(cue => cue.designVersion === "refined-v1");
  const refinedSubtitle = plan.subtitlePresetId === "sub-refined-v1";
  const titles: MixTitle[] = (plan.titles ?? []).filter(cue => cue.designVersion !== "refined-v1");
  const watermark = plan.watermark;
  const bgm = plan.bgm;
  const sfx = plan.sfx ?? [];
  if (!DRY_RUN) {
    assertSourceDirection(video, "9:16", "digital human source");
    for (const title of titles) {
      if (title.src)
        assertSourceDirection(resolveRenderMixAsset(title.src, assetSearchRoots), "9:16", "title background",
          /\.(png|jpe?g|webp)$/i.test(title.src) ? "image" : "video");
    }
    for (const insert of inserts) {
      assertSourceDirection(resolveRenderMixAsset(insert.src, assetSearchRoots), "9:16", "scene insert",
        /\.(png|jpe?g|webp)$/i.test(insert.src) ? "image" : "video");
    }
  }

  // 贴纸层：plan.stickers 或 --stickers 覆盖
  let stickers: MixSticker[] = plan.stickers ?? [];
  if (STICKERS_JSON) {
    try { stickers = JSON.parse(STICKERS_JSON); }
    catch (e) { console.warn(`⚠ --stickers JSON 解析失败，忽略: ${(e as Error).message}`); }
  }
  // 花字层：plan.fancyTexts 或 --fancy-texts 覆盖
  let fancyTexts: MixFancyText[] = plan.fancyTexts ?? [];
  if (FANCY_JSON) {
    try { fancyTexts = JSON.parse(FANCY_JSON); }
    catch (e) { console.warn(`⚠ --fancy-texts JSON 解析失败，忽略: ${(e as Error).message}`); }
  }

  // 工作目录放系统临时区（纯 ASCII），cwd 设此处 → ass 用相对名、字体放 fonts/ 子目录，规避中文/冒号路径转义
  withRenderMixWorkdir(os.tmpdir(), (workdir) => {
  fs.mkdirSync(path.join(workdir, "fonts"));
  fs.copyFileSync(FONT_FILE, path.join(workdir, "fonts", path.basename(FONT_FILE)));

  // ── 构建（可能注入标题事件的）字幕 ASS ──
  const hasSubs = fs.existsSync(assFile) || titles.length > 0;
  if (hasSubs) {
    let assText: string;
    if (fs.existsSync(assFile)) {
      assText = fs.readFileSync(assFile, "utf8");
      if (refinedSubtitle) {
        const emphasis=new Map(assText.split(/\r?\n/).filter(line=>line.startsWith("Dialogue:")).map(line=> {
          const raw=line.split(",").slice(9).join(",");
          return [raw.replace(/\{[^}]*\}/g,"").replace(/\\N/g,"\n"),assHighlightTerms(raw)] as const;
        }));
        if (recipe) refinedCues.push(...recipe.subtitle_cues.map(cue => ({text:cue.text,
          start:cue.start_us/1e6,end:cue.end_us/1e6,role:"subtitle",highlight:true,highlightTerms:emphasis.get(cue.text) ?? []})));
        else {
          const seconds=(t:string)=>t.split(":").reduce((sum,value)=>sum*60+Number(value),0);
          for (const line of assText.split(/\r?\n/).filter(line=>line.startsWith("Dialogue:"))) {
            const fields=line.split(","); refinedCues.push({role:"subtitle",start:seconds(fields[1]),end:seconds(fields[2]),
              text:fields.slice(9).join(",").replace(/\{[^}]*\}/g,"").replace(/\\N/g,"\n"),highlight:true,highlightTerms:assHighlightTerms(fields.slice(9).join(","))});
          }
        }
        assText=assText.split(/\r?\n/).filter(line=>!line.startsWith("Dialogue:")).join("\n");
      }
    } else {
      // 标题样式按1080×1920定义；无外部字幕时也保留相同逻辑画布，让libass统一缩放预览。
      assText =
        `[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n` +
        `[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n\n` +
        `[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n`;
    }
    if (titles.length > 0) {
      // libass positions use the script resolution, independent of preview pixels.
      const assWidth = Number(/^PlayResX:\s*(\d+)/m.exec(assText)?.[1]) || CANVAS_W;
      const assHeight = Number(/^PlayResY:\s*(\d+)/m.exec(assText)?.[1]) || CANVAS_H;
      const titleStyles = titles.map((title, index) => {
        if (!title.presetId) {
          return `Style: MixTitle${index},${FONT_NAME},${TITLE_SIZE},${COLOR_WHITE},&H000000FF&,&H00000000&,&H64000000&,0,0,0,0,100,100,1,0,1,4,2,8,60,60,120,1`;
        }
        const preset = resolveTitlePreset(title.presetId);
        if (preset.font) {
          const fontPath = path.join(ROOT, "assets", "fonts", preset.font);
          if (!fs.existsSync(fontPath)) {
            throw new Error(`PACKAGING_TITLE_FONT_MISSING: ${preset.font}`);
          }
          const target = path.join(workdir, "fonts", path.basename(fontPath));
          if (!fs.existsSync(target)) fs.copyFileSync(fontPath, target);
        }
        const fontName = preset.fontname_guess || FONT_NAME;
        const accent = assColor(preset.accent_hex, COLOR_WHITE);
        const background = assColor(preset.background_hex, "&H00171717&");
        return `Style: MixTitle${index},${fontName},${TITLE_SIZE},${accent},&H000000FF&,${background},${background},0,0,0,0,100,100,1,0,3,2,0,8,60,60,120,1`;
      }).join("\n") + "\n";
      assText = assText.replace(
        /(\[V4\+ Styles\]\nFormat:[^\n]*\n)/,
        `$1${titleStyles}`
      );
      const titleEvents = titles
        .map((t, index) => {
          const placed = t.x === undefined || t.y === undefined ? "" :
            `{\\an7\\pos(${Math.round(t.x * assWidth)},${Math.round(t.y * assHeight)})` +
            `\\fs${Math.max(24, Math.round(TITLE_SIZE * (t.scale ?? .84) / .84))}}`;
          return `Dialogue: 0,${fmtAssTime(t.start)},${fmtAssTime(t.end)},MixTitle${index},,0,0,0,,${placed}${escapeAssText(t.text).replace(/\r\n?|\n/g, "\\N")}`;
        })
        .join("\n");
      assText = assText.replace(/(\[Events\]\nFormat:[^\n]*\n)/, `$1${titleEvents}\n`);
    }
    assText = applySubtitleAppearance(assText, plan.subtitleAppearance, CANVAS_W, CANVAS_H);
    fs.writeFileSync(path.join(workdir, "sub.ass"), assText, "utf8");
  }

  // ── 组装输入列表：素体 + 每个插入/标题素材各占一个 -i ──
  // 同一 src 多次使用也分别 -i（简单可靠，P1 不做去重优化）
  const inputs: string[] = previewWindow
    ? ["-ss", String(previewWindow.start * speed),
       "-t", String((previewWindow.end - previewWindow.start) * speed + .2), "-i", video]
    : ["-i", video];
  let inputCount = 1; // 0 是素体
  type Overlay = { inputIdx: number; isVideo: boolean; start: number; end: number; sourceStart: number; sourceEnd: number; pos?: InsertPosition };
  const overlays: Overlay[] = [];

  const isVideoSrc = (src: string) => /\.(mp4|mov|mkv|webm|m4v)$/i.test(src);
  let insertSourceWindows: { sourceStart: number; sourceEnd: number }[];
  try {
    insertSourceWindows = inserts.map((ins) => sourceWindow(ins, ins.type === "video" || isVideoSrc(ins.src)));
  } catch (error) {
    console.error((error as Error).message);
    process.exit(1);
    return;
  }
  // 素材路径：存在的绝对路径 → 显式 asset-root → 旧 public/inserts → 旧 public/
  const resolveAsset = (src: string) => {
    return resolveRenderMixAsset(src, assetSearchRoots);
  };
  // 贴纸文件解析：presetId → stickers.json 的 file；src → 直接按资源解析
  const resolveStickerFile = (st: MixSticker): string | null => {
    let rel = st.src;
    if (!rel && st.presetId) {
      const p = path.join(ROOT, "assets", "manifests", "stickers.json");
      if (fs.existsSync(p)) {
        try {
          const m = JSON.parse(fs.readFileSync(p, "utf8"));
          const it = (m.items || []).find((x: any) => x.id === st.presetId);
          if (it?.file) rel = it.file;
        } catch { /* ignore */ }
      }
    }
    if (!rel) return null;
    const a = path.isAbsolute(rel) ? rel : path.join(ROOT, rel);
    if (fs.existsSync(a)) return a;
    const b = path.join(ROOT, "public", rel);
    if (fs.existsSync(b)) return b;
    return a;
  };
  // 花字 PNG 生成：调用 Python/PIL 生成器，把文字 + 预设样式渲染为透明 PNG
  const genFancyPng = (text: string, presetId: string, outPath: string) => {
    const py = path.join(ROOT, ".venv", "Scripts", "python.exe");
    const script = path.join(ROOT, "scripts", "gen-fancy-text.py");
    execFileSync(py, [script, "--text", text, "--preset-id", presetId, "--out", outPath, "--base-size", "200"], { cwd: ROOT });
  };
  const pushInput = (src: string) => {
    inputs.push("-i", resolveAsset(src));
    return inputCount++;
  };
  // 已解析为绝对路径的文件（如贴纸 manifest 指向的 assets/stickers/*.png）直接 -i，避免被 resolveAsset 二次拼接
  const pushInputPath = (absSrc: string) => {
    inputs.push("-i", absSrc);
    return inputCount++;
  };

  // 标题背景空镜（若 title.src 存在）也作为全屏 overlay；标题文字已在 ass 里
  for (const t of titles) {
    if (t.src) {
      const idx = pushInput(t.src);
      overlays.push({
        inputIdx: idx,
        isVideo: isVideoSrc(t.src),
        start: t.start,
        end: t.end,
        sourceStart: 0,
        sourceEnd: Math.max(0, t.end - t.start),
      });
    }
  }
  inserts.forEach((ins, insertIndex) => {
    const idx = pushInput(ins.src);
    overlays.push({
      inputIdx: idx,
      // 只要素材是视频文件就按视频处理（含 Seedance 生成的画中画 mp4）；type 仅决定全屏/画中画
      isVideo: ins.type === "video" || isVideoSrc(ins.src),
      start: ins.start,
      end: ins.end,
      ...insertSourceWindows[insertIndex],
      pos: ins.type === "broll" ? undefined : ins.position,
    });
  });

  // 角标/水印：全程固定挂角（叠在所有插入之上、字幕之下）
  let wmIdx = -1;
  if (watermark?.src) wmIdx = pushInput(watermark.src);

  const fullDuration = recipe ? recipe.duration_us / 1_000_000 : DRY_RUN ? undefined : Number(
    execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration",
      "-of", "default=noprint_wrappers=1:nokey=1", video],
      { encoding: "utf8", timeout: 30000, maxBuffer: 65536 }).trim()) / speed;
  const fullAudio = renderFullAudio({
    voice: video, speed, duration: fullDuration, defaultGain: .18,
    bgm: bgm?.src ? { ...bgm, src: resolveAsset(bgm.src) } : undefined,
    inserts: inserts.map((ins) => ({ ...ins, src: resolveAsset(ins.src) })),
    sfx: sfx.map((effect) => ({ ...effect, src: resolveAsset(effect.src) })),
    reviewOutput: options.audioReviewOutput, dryRun: DRY_RUN,
  }, workdir);
  const audioIdx = inputCount++;
  inputs.push(...(previewWindow ? ["-ss", String(previewWindow.start)] : []),
    "-i", fullAudio);

  // ── filter_complex ──
  const basePts = previewWindow
    ? `(PTS-STARTPTS)/${speed}+${previewWindow.start}/TB`
    : `PTS/${speed}`;
  const parts: string[] = [
    // Stabilize the frozen frame grid before overlay framesync; output -r alone is too late.
    `[0:v]setpts=${basePts}${recipe ? `,fps=${recipe.output.fps_num}/${recipe.output.fps_den}` : ""},scale=${CANVAS_W}:${CANVAS_H}:force_original_aspect_ratio=increase,crop=${CANVAS_W}:${CANVAS_H},setsar=1` +
      `${fullDuration === undefined ? "" : `,tpad=stop_mode=clone:stop_duration=${fullDuration}`}[base]`,
  ];
  let last = "base";
  overlays.forEach((ov, i) => {
    const prepOut = `ov${i}`;
    const nextBase = `b${i}`;
    if (ov.pos) {
      parts.push(pipPrep(`${ov.inputIdx}:v`, ov.isVideo, ov.pos, ov.start, ov.end, ov.sourceStart, ov.sourceEnd, prepOut));
      const X = Math.round(ov.pos.x * CANVAS_W);
      const Y = Math.round(ov.pos.y * CANVAS_H);
      parts.push(`[${last}][${prepOut}]overlay=${X}:${Y}:enable='gte(t,${ov.start})*lt(t,${ov.end})'[${nextBase}]`);
    } else {
      parts.push(fullscreenPrep(`${ov.inputIdx}:v`, ov.isVideo, ov.start, ov.end, ov.sourceStart, ov.sourceEnd, prepOut));
      parts.push(`[${last}][${prepOut}]overlay=0:0:enable='gte(t,${ov.start})*lt(t,${ov.end})'[${nextBase}]`);
    }
    last = nextBase;
  });

  // 角标/水印：缩放到目标宽度（高度自适应保原比例）+ 透明度，全程叠加
  if (wmIdx >= 0) {
    const pos = watermark!.position;
    const W = Math.round((pos?.width ?? 0.28) * CANVAS_W);
    const X = Math.round((pos?.x ?? 0.04) * CANVAS_W);
    const Y = Math.round((pos?.y ?? 0.02) * CANVAS_H);
    const op = watermark!.opacity ?? 1;
    parts.push(`[${wmIdx}:v]scale=${W}:-1,format=rgba,colorchannelmixer=aa=${op}[wm]`);
    parts.push(`[${last}][wm]overlay=${X}:${Y}[wmout]`);
    last = "wmout";
  }

  // 贴纸层：复用 watermark overlay 模式（scale→rgba→opacity→overlay），加时间窗+定位；置于水印之上、字幕之下
  let skN = 0;
  for (const st of stickers) {
    const src = resolveStickerFile(st);
    if (!src) { console.warn(`（贴纸 ${st.presetId || st.src || "#" + skN} 无法解析，跳过）`); continue; }
    const idx = pushInputPath(src);
    const W = Math.round((st.scale ?? 0.2) * CANVAS_W);
    const X = Math.round((st.x ?? 0.04) * CANVAS_W);
    const Y = Math.round((st.y ?? 0.04) * CANVAS_H);
    const op = st.opacity ?? 1;
    parts.push(`[${idx}:v]scale=${W}:-1,format=rgba,colorchannelmixer=aa=${op}[sk${skN}]`);
    parts.push(`[${last}][sk${skN}]overlay=${X}:${Y}:enable='between(t,${st.start},${st.end})'[sko${skN}]`);
    last = `sko${skN}`;
    skN++;
  }

  // 花字层：动态生成透明 PNG，复用贴纸 overlay 模式，并叠加 fade/scale entrance 近似动画；置于贴纸之上、字幕之下
  let ftN = 0;
  for (const ft of fancyTexts.filter(cue => cue.designVersion !== "refined-v1")) {
    const ftPng = path.join(workdir, `fancy${ftN}.png`);
    genFancyPng(ft.text, ft.presetId, ftPng);
    // Animated PNG overlays need a continuing timeline; a single t=0 frame stays transparent.
    inputs.push("-loop", "1", "-framerate", "30", "-t", String(ft.end), "-i", ftPng);
    const idx = inputCount++;
    const W = Math.round((ft.scale ?? 0.25) * CANVAS_W);
    const X = Math.round((ft.x ?? 0.04) * CANVAS_W);
    const Y = Math.round((ft.y ?? 0.04) * CANVAS_H);
    const op = ft.opacity ?? 1;
    const entrance = ft.entrance ?? "fade";
    const t0 = ft.start;
    const t1 = ft.start + 0.25;

    let prep = `[${idx}:v]`;
    if (entrance === "scale" || entrance === "pop" || entrance === "bounce") {
      prep += `scale='if(between(t,${t0.toFixed(3)},${t1.toFixed(3)}),${W}*(0.78+0.22*(t-${t0.toFixed(3)})/0.25),${W})':-1:eval=frame`;
    } else {
      prep += `scale=${W}:-1`;
    }
    prep += `,format=rgba`;
    if (entrance !== "none") {
      prep += `,fade=t=in:st=${t0.toFixed(3)}:d=0.25:alpha=1`;
    }
    prep += `,colorchannelmixer=aa=${op}[ft${ftN}]`;
    parts.push(prep);
    const overlayX = entrance === "slide"
      ? `'if(lt(t,${t1.toFixed(3)}),${X}-${W}*(1-max(0,(t-${t0.toFixed(3)})/0.25)),${X})'` : String(X);
    parts.push(`[${last}][ft${ftN}]overlay=${overlayX}:${Y}:enable='between(t,${ft.start},${ft.end})'[fto${ftN}]`);
    last = `fto${ftN}`;
    ftN++;
  }

  const refined = appendRefinedLayers({root:ROOT,work:workdir,width:CANVAS_W,height:CANVAS_H,
    cues:refinedCues.filter(cue=>!previewWindow || cue.end>previewWindow.start && cue.start<previewWindow.end),appearance:plan.subtitleAppearance,inputs,parts,inputIndex:inputCount,videoBase:last});
  inputCount=refined.inputIndex;last=refined.videoBase;
  if (hasSubs) {
    parts.push(`[${last}]ass=sub.ass:fontsdir=fonts[${previewWindow ? "fullv" : "outv"}]`);
  } else {
    parts.push(`[${last}]null[${previewWindow ? "fullv" : "outv"}]`);
  }
  if (previewWindow) {
    parts.push(`[fullv]trim=start=${previewWindow.start}:end=${previewWindow.end},setpts=PTS-STARTPTS[outv]`);
  }
  // Preview seeking happens after full-timeline DSP, exactly as in the final mix.
  parts.push(`[${audioIdx}:a]${previewWindow
    ? `atrim=duration=${previewWindow.end - previewWindow.start},` : ""}asetpts=PTS-STARTPTS[outa]`);
  const renderDuration = previewWindow ? previewWindow.end - previewWindow.start : recipe
    ? Math.min(recipe.duration_us / 1_000_000, outputProfile.maxDuration ?? Infinity)
    : outputProfile.maxDuration ?? fullDuration;
  const args = [
    "-y",
    ...inputs,
    "-filter_complex",
    parts.join(";"),
    "-map", "[outv]",
    "-map", "[outa]",
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", String(outputProfile.crf ?? 20),
    "-pix_fmt", "yuv420p",
    ...(recipe ? ["-r", `${recipe.output.fps_num}/${recipe.output.fps_den}`] : []),
    "-c:a", "aac",
    "-b:a", AUDIO_MIX_POLICY.bitrate,
    "-ar", String(recipe?.output.audio_rate ?? OUTPUT_AUDIO_RATE),
    ...(renderDuration === undefined ? ["-shortest"] : []),
    "-movflags", "+faststart",
    ...(renderDuration === undefined ? [] : ["-t", String(renderDuration)]),
    outPath,
  ];

  console.log(
    `▶ 统一渲染 ${name}：${speed}x | 字幕${hasSubs ? "✓" : "✗"} | 插入 ${inserts.length} | 标题 ${titles.length} | 水印${wmIdx >= 0 ? "✓" : "✗"} | 贴纸${stickers.length} | 花字${fancyTexts.length} | 音效${sfx.length} | BGM${bgm?.src ? "✓" : "✗"}${DRY_RUN ? " | [DRY-RUN]" : ""}`
  );
  if (DRY_RUN) {
    console.log("\n【DRY-RUN】不执行 ffmpeg，仅打印命令与 filter_complex：\n");
    console.log(formatRenderMixTaskPaths(options));
    console.log("ffmpeg " + args.map((a) => (a.includes(" ") || a.includes(";") || a.includes("'")) ? JSON.stringify(a) : a).join(" "));
    console.log("\n--- filter_complex ---\n" + parts.join(";\n"));
    return;
  }
  if (!fs.existsSync(path.dirname(outPath))) fs.mkdirSync(path.dirname(outPath), { recursive: true });
  execFileSync("ffmpeg", args, { cwd: workdir, stdio: "inherit" });
  console.log(`✔ 完成 ${outPath}`);
  });
}

main();
