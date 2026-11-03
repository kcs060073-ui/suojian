/**
 * 流水线 C · 产品宣传片渲染（独立引擎，不动 render-mix / render-ad）。
 *
 * 形态：PC 截图为主画面，APP 竖屏录屏作右侧手机画中画，烧 SRT 字幕（卖点蓝色高亮），
 *       配音为主音轨 + BGM 压低。专为「截图/录屏类产品介绍片」设计，区别于：
 *         · render-mix  数字人口播混剪（竖屏、倍速、整条素体）
 *         · render-ad   信息流广告（逐句配片、AI 配音驱动）
 *
 * 一次 filter_complex 落实：
 *   1. 主画面逐段：静态图 Ken Burns 缓慢推/拉（zoompan），视频段 scale+裁切铺满
 *   2. 段间 xfade 交叉溶解（0.4s），避免 PPT 翻页
 *   3. APP 画中画：竖屏小窗叠右侧，细边框，按时间窗显隐
 *   4. 烧 SRT 字幕：白色粗体 + 黑阴影，卖点短语蓝色高亮
 *   5. 配音主轨 + 可选 BGM 压低混入
 *   6. 横版 1920x1080 / 竖版 1080x1920，30fps，H.264
 *
 * 输入: public/scripts/<name>.storyboard.json（见下方 Storyboard 契约）
 *       配音 public/ad_audio/<name>.voice.mp3（先跑 promo-tts）
 * 输出: out/<name>.mp4
 *
 * 用法: ts-node scripts/render-promo.ts <name> [bgm路径]
 *
 * 中文路径坑：素材/字体/字幕全拷到临时 ASCII 工作目录，ffmpeg 在那跑、相对名引用，最后移回 out/。
 */
import "./load-env";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { execFileSync } from "child_process";

const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(ROOT, "out");
const FONTS = path.join(ROOT, "assets", "fonts");
const SCRIPTS = path.join(ROOT, "public", "scripts");

const T = 0.4;          // 主画面段间 xfade 转场时长(s)
const FPS = 30;
const IMG_EXT = /\.(png|jpe?g|webp)$/i;
const ZOOM_AMT = 0.12;  // Ken Burns 推拉幅度（1.0 → 1.12）
const HILITE = "&HF6823B&"; // 高亮蓝 #3B82F6（ASS 为 BBGGRR）
const WHITE = "&HFFFFFF&";
// 手机样机（常量须与 scripts/make-phone-frame.py 一致）：竖屏 APP 录屏放 iPhone 边框 + 深色背景
const PHONE = { SW: 422, SH: 936, BEZEL: 18 };
const PHONE_FRAME = path.join(ROOT, "assets", "phone_frame.png");
const DEFAULT_BG = "0x0E0E12"; // 默认深色背景（近黑钛灰）
const CARD = { W: 1480, H: 860, BORDER: "0x2A2A2E" }; // card 悬浮卡片最大盒子 + 细边

// layout: "full" 满屏铺满裁切（默认）；"phone" 手机样机+深色背景（特斯拉式）；
//         "text" 纯深色背景大字（Keynote 开场/收尾，文字走 subtitle ASS）；"card" PC 截图裁成悬浮卡片放深色背景
interface MainSeg { start: number; end: number; src?: string; motion?: "zoom-in" | "zoom-out" | "none"; layout?: "full" | "phone" | "text" | "card"; crop?: string }
interface PipSeg { start: number; end: number; src: string }
interface Storyboard {
  name: string;
  ratio: "16:9" | "9:16";
  main: MainSeg[];
  pip?: PipSeg[];
  pip_box?: { w_pct?: number; x_pct?: number; y_pct?: number }; // 小窗宽/左上角，画布占比
  subtitle?: string;       // SRT 路径（绝对或相对 ROOT）
  highlights?: string[];   // 需蓝色高亮的短语
  voice?: string;          // 配音路径（默认 ad_audio/<name>.voice.mp3）
  bgm?: string;            // BGM 路径（命令行参数优先）
  bg?: string;             // 手机样机段的背景色（#RRGGBB 或 0xRRGGBB），默认深钛灰
}

const resolveSrc = (s: string) => (path.isAbsolute(s) ? s : path.join(ROOT, s));
const probeDur = (f: string): number => {
  try { return parseFloat(execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", f], { encoding: "utf8" }).trim()) || 0; }
  catch { return 0; }
};

// ── 字幕：SRT → ASS（白粗体 + 黑阴影，highlights 蓝色高亮）──
interface Cue { start: number; end: number; text: string }
const parseTime = (t: string): number => {
  const m = t.trim().match(/(\d+):(\d+):(\d+)[,.](\d+)/);
  return m ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]) + (+m[4]) / 1000 : 0;
};
function parseSrt(file: string): Cue[] {
  const raw = fs.readFileSync(file, "utf8").replace(/\r/g, "");
  return raw.split(/\n\s*\n/).map((b) => b.trim()).filter(Boolean).map((b) => {
    const lines = b.split("\n");
    const tl = lines.find((l) => l.includes("-->")) || "";
    const [s, e] = tl.split("-->");
    const text = lines.slice(lines.indexOf(tl) + 1).join(" ").trim();
    return { start: parseTime(s), end: parseTime(e), text };
  }).filter((c) => c.text);
}
const cs = (sec: number) => {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${s.toFixed(2).padStart(5, "0")}`;
};
// 高亮：longest-first，把命中短语包成 {\c蓝}短语{\c白}
function highlight(text: string, phrases: string[]): string {
  let out = text;
  for (const p of [...phrases].sort((a, b) => b.length - a.length)) {
    if (!p) continue;
    out = out.split(p).join(`{\\c${HILITE}}${p}{\\c${WHITE}}`);
  }
  return out;
}
function buildAss(cues: Cue[], W: number, H: number, fontName: string, highlights: string[]): string {
  const fontPx = W >= H ? 54 : 70;
  const marginV = W >= H ? 96 : 560; // 距底(px)
  const shadow = W >= H ? 4 : 5;
  const head =
    `[Script Info]\nScriptType: v4.00+\nPlayResX: ${W}\nPlayResY: ${H}\nScaledBorderAndShadow: yes\nWrapStyle: 2\n\n` +
    `[V4+ Styles]\n` +
    `Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n` +
    `Style: Default,${fontName},${fontPx},${WHITE},${WHITE},&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,2,${shadow},2,80,80,${marginV},1\n\n` +
    `[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n`;
  const lines = cues.map((c) => `Dialogue: 0,${cs(c.start)},${cs(c.end)},Default,,0,0,0,,${highlight(c.text.replace(/\n/g, " "), highlights)}`);
  return head + lines.join("\n") + "\n";
}

function main() {
  const name = process.argv[2];
  const bgmArg = process.argv[3];
  if (!name) { console.error("用法: ts-node scripts/render-promo.ts <name> [bgm路径]"); process.exit(1); }

  const sbPath = path.join(SCRIPTS, `${name}.storyboard.json`);
  if (!fs.existsSync(sbPath)) { console.error(`缺分镜: ${sbPath}`); process.exit(1); }
  const sb: Storyboard = JSON.parse(fs.readFileSync(sbPath, "utf8"));

  const [W, H] = sb.ratio === "9:16" ? [1080, 1920] : [1920, 1080];
  const voiceRel = sb.voice || path.join("public", "ad_audio", `${name}.voice.mp3`);
  const voiceAbs = resolveSrc(voiceRel);
  if (!fs.existsSync(voiceAbs)) { console.error(`缺配音 ${voiceAbs}（先跑 promo-tts）`); process.exit(1); }

  const main = sb.main;
  const pips = sb.pip || [];
  const box = { w: sb.pip_box?.w_pct ?? 0.18, x: sb.pip_box?.x_pct ?? 0.79, y: sb.pip_box?.y_pct ?? 0.30 };
  const bgm = bgmArg || sb.bgm;
  const bgColor = sb.bg ? sb.bg.replace(/^#/, "0x") : DEFAULT_BG;
  const usesPhone = main.some((s) => s.layout === "phone");
  if (usesPhone && !fs.existsSync(PHONE_FRAME)) {
    console.error(`缺手机样机边框 ${PHONE_FRAME}（先跑 .venv/Scripts/python scripts/make-phone-frame.py）`); process.exit(1);
  }

  // 临时 ASCII 工作目录
  const work = fs.mkdtempSync(path.join(os.tmpdir(), `promo_${name}_`));
  fs.mkdirSync(path.join(work, "fonts"));
  if (usesPhone) fs.copyFileSync(PHONE_FRAME, path.join(work, "phone_frame.png"));
  let fontName = "Source Han Serif SC Heavy";
  const fontFiles = fs.existsSync(FONTS) ? fs.readdirSync(FONTS).filter((f) => /\.(otf|ttf|ttc)$/i.test(f)) : [];
  for (const f of fontFiles) fs.copyFileSync(path.join(FONTS, f), path.join(work, "fonts", f));
  if (!fontFiles.some((f) => /SourceHanSerif/i.test(f)) && fontFiles[0]) fontName = path.parse(fontFiles[0]).name;

  // ── 输入与 filter 组装 ──
  const inputs: string[] = [];
  const parts: string[] = [];
  let idx = 0;

  // 主画面段：图 → Ken Burns zoompan；视频 → scale/裁切/trim（短的循环铺满）
  main.forEach((s, i) => {
    const L = +(s.end - s.start + T).toFixed(3); // 含转场重叠料
    const durF = Math.max(2, Math.round(L * FPS));

    // 纯深色背景段（Keynote 大字开场/收尾）：文字由 subtitle ASS 烧上去，本段只出背景
    if (s.layout === "text") {
      parts.push(`color=c=${bgColor}:s=${W}x${H}:r=${FPS}:d=${L},setsar=1,format=yuv420p[v${i}]`);
      return;
    }

    const ext = path.extname(s.src!) || ".png";
    const dst = `m${String(i).padStart(2, "0")}${ext}`;
    fs.copyFileSync(resolveSrc(s.src!), path.join(work, dst));

    // 卡片段：PC 截图（可裁切到重点区域）缩成悬浮卡片，居中放深色背景上，细边框
    if (s.layout === "card") {
      inputs.push("-loop", "1", "-i", dst);
      const cidx = idx++;
      const cropPart = s.crop ? `crop=${s.crop},` : "";
      parts.push(
        `color=c=${bgColor}:s=${W}x${H}:r=${FPS}:d=${L}[cbg${i}]`,
        `[${cidx}:v]${cropPart}scale=${CARD.W}:${CARD.H}:force_original_aspect_ratio=decrease,setsar=1,pad=iw+4:ih+4:2:2:color=${CARD.BORDER}[card${i}]`,
        `[cbg${i}][card${i}]overlay=(W-w)/2:(H-h)/2,trim=duration=${L},fps=${FPS},setpts=PTS-STARTPTS,format=yuv420p[v${i}]`
      );
      return;
    }

    // 手机样机段：竖屏录屏垫在深色背景上、对齐挖孔，再叠 iPhone 边框（特斯拉式）
    if (s.layout === "phone") {
      const rd = probeDur(path.join(work, dst));
      inputs.push(...(IMG_EXT.test(ext) ? ["-loop", "1", "-i", dst]
        : rd > 0 && rd < L + 0.05 ? ["-stream_loop", "-1", "-i", dst] : ["-i", dst]));
      const appIdx = idx++;
      inputs.push("-loop", "1", "-i", "phone_frame.png");
      const frmIdx = idx++;
      const { SW, SH, BEZEL } = PHONE;
      const FW = SW + 2 * BEZEL, FH = SH + 2 * BEZEL;
      const sX = Math.round((W - SW) / 2), sY = Math.round((H - SH) / 2);
      const fX = sX - BEZEL, fY = sY - BEZEL;
      parts.push(
        `color=c=${bgColor}:s=${W}x${H}:r=${FPS}:d=${L}[pbg${i}]`,
        `[${appIdx}:v]trim=duration=${L},setpts=PTS-STARTPTS,scale=${SW}:${SH},setsar=1[psc${i}]`,
        `[${frmIdx}:v]scale=${FW}:${FH}[pfr${i}]`,
        `[pbg${i}][psc${i}]overlay=${sX}:${sY}[pcs${i}]`,
        `[pcs${i}][pfr${i}]overlay=${fX}:${fY},trim=duration=${L},fps=${FPS},setpts=PTS-STARTPTS[v${i}]`
      );
      return;
    }

    if (IMG_EXT.test(ext)) {
      inputs.push("-loop", "1", "-i", dst);
      // 先放大铺满画布的 ~1.4 倍（zoom 时保持清晰），再 zoompan 线性推/拉。用 on/durF 做确定性轨迹，避开 zoompan 抖动。
      const sw = Math.round(W * 1.4), sh = Math.round(H * 1.4);
      const motion = s.motion ?? "zoom-in";
      const z = motion === "zoom-out"
        ? `1+${ZOOM_AMT}*(1-on/${durF})`
        : motion === "none" ? `1.0` : `1+${ZOOM_AMT}*on/${durF}`;
      parts.push(
        `[${idx}:v]scale=${sw}:${sh}:force_original_aspect_ratio=increase,crop=${sw}:${sh},setsar=1,` +
        `zoompan=z='${z}':d=${durF}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=${W}x${H}:fps=${FPS},` +
        `trim=duration=${L},setpts=PTS-STARTPTS[v${i}]`
      );
    } else {
      const rd = probeDur(path.join(work, dst));
      inputs.push(...(rd > 0 && rd < L + 0.05 ? ["-stream_loop", "-1", "-i", dst] : ["-i", dst]));
      parts.push(`[${idx}:v]scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H},setsar=1,fps=${FPS},trim=duration=${L},setpts=PTS-STARTPTS[v${i}]`);
    }
    idx++;
  });

  // 主画面 xfade 链
  let last = "v0";
  if (main.length === 1) {
    parts.push(`[v0]null[mbase]`);
  } else {
    let off = main[0].end - main[0].start; // = D0 - T
    for (let i = 1; i < main.length; i++) {
      const out = i === main.length - 1 ? "mbase" : `mc${i}`;
      parts.push(`[${last}][v${i}]xfade=transition=fade:duration=${T}:offset=${off.toFixed(3)}[${out}]`);
      off += main[i].end - main[i].start;
      last = out;
    }
  }
  last = "mbase";

  // APP 画中画：竖屏小窗 → 细白边框 → 叠右侧，按时间窗显隐
  const boxW = Math.round(box.w * W);
  const boxX = Math.round(box.x * W);
  const boxY = Math.round(box.y * H);
  const BORDER = 3;
  pips.forEach((p, i) => {
    const win = +(p.end - p.start).toFixed(3);
    const ext = path.extname(p.src) || ".mp4";
    const dst = `p${String(i).padStart(2, "0")}${ext}`;
    fs.copyFileSync(resolveSrc(p.src), path.join(work, dst));
    const rd = probeDur(path.join(work, dst));
    inputs.push(...(IMG_EXT.test(ext) ? ["-loop", "1", "-i", dst]
      : rd > 0 && rd < win + 0.05 ? ["-stream_loop", "-1", "-i", dst] : ["-i", dst]));
    // 缩到小窗宽（高按比例），细白边框，PTS 平移到 p.start 起播
    parts.push(
      `[${idx}:v]trim=duration=${win},setpts=PTS-STARTPTS+${p.start}/TB,` +
      `scale=${boxW - 2 * BORDER}:-2,setsar=1,pad=iw+${2 * BORDER}:ih+${2 * BORDER}:${BORDER}:${BORDER}:color=white[pp${i}]`
    );
    const nb = `pb${i}`;
    parts.push(`[${last}][pp${i}]overlay=${boxX}:${boxY}:enable='between(t,${p.start},${p.end})'[${nb}]`);
    last = nb;
    idx++;
  });

  // 烧字幕：subtitle 给 .ass 则直接烧（Keynote 自定义样式，用系统字体）；给 .srt 则套内置白字样式
  let vout = last;
  if (sb.subtitle) {
    const subAbs = resolveSrc(sb.subtitle);
    if (fs.existsSync(subAbs)) {
      if (/\.ass$/i.test(subAbs)) {
        fs.copyFileSync(subAbs, path.join(work, "sub.ass"));
        parts.push(`[${last}]ass=sub.ass[vout]`); // 不限 fontsdir，让 libass 用系统字体（微软雅黑等）
      } else {
        fs.writeFileSync(path.join(work, "sub.ass"), buildAss(parseSrt(subAbs), W, H, fontName, sb.highlights || []), "utf8");
        parts.push(`[${last}]ass=sub.ass:fontsdir=fonts[vout]`);
      }
      vout = "vout";
    }
  }
  if (vout === last) parts.push(`[${last}]null[vout]`);

  // 音频：配音主轨 + 可选 BGM 压低
  fs.copyFileSync(voiceAbs, path.join(work, "voice.mp3"));
  inputs.push("-i", "voice.mp3");
  const voiceIdx = idx++;
  let audioMap = `${voiceIdx}:a`;
  if (bgm && fs.existsSync(resolveSrc(bgm))) {
    fs.copyFileSync(resolveSrc(bgm), path.join(work, "bgm.mp3"));
    inputs.push("-stream_loop", "-1", "-i", "bgm.mp3");
    const bgmIdx = idx++;
    parts.push(`[${bgmIdx}:a]volume=0.10,aformat=channel_layouts=stereo[bg];[${voiceIdx}:a][bg]amix=inputs=2:duration=first:normalize=0[aout]`);
    audioMap = "[aout]";
  }

  const args = ["-y", ...inputs, "-filter_complex", parts.join(";"),
    "-map", "[vout]", "-map", audioMap, "-c:v", "libx264", "-preset", "medium", "-crf", "20",
    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", "_out.mp4"];

  console.log(`▶ 产品宣传渲染 ${name}（${sb.ratio} ${W}x${H} | 主画面 ${main.length} 段 | PiP ${pips.length} | 字幕${sb.subtitle ? "✓" : "✗"} | BGM${bgm ? "✓" : "✗"}）...`);
  execFileSync("ffmpeg", args, { cwd: work, stdio: "inherit" });

  if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });
  const outFile = path.join(OUT, `${name}.mp4`);
  fs.copyFileSync(path.join(work, "_out.mp4"), outFile);
  fs.rmSync(work, { recursive: true, force: true });
  console.log(`✔ 成片 → out/${name}.mp4`);
}

main();
