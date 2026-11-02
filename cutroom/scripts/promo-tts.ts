/**
 * 流水线 C · 产品宣传片配音（火山豆包语音合成-2.0）。
 * 与信息流的 tts.ts 不同：本脚本「锁定 SRT 时间轴」——读 <name>.srt 的每条字幕，
 * 逐句 TTS，再把每句**贴合到该字幕的精确时间窗**（短了补静音、长了轻微加速），
 * 拼成一条与 SRT 完全对齐的整轨（SRT 连续，总时长 = SRT 末尾 = 60s）。
 *
 * 这样配音、字幕、分镜三者天然对齐，成片正好卡到 SRT 排好的 0–60s。
 *
 * 用法: ts-node scripts/promo-tts.ts <name> <srt路径> [speed=1.0]
 *   产物：public/ad_audio/<name>/seg_NN.mp3（调试用分段）
 *         public/ad_audio/<name>.voice.mp3（与 SRT 对齐的整条配音）
 *
 * 环境变量（见 .env）：TTS_APPID / TTS_ACCESS_TOKEN / TTS_CLUSTER / TTS_VOICE_TYPE / TTS_ENDPOINT
 * 系统死代理会拦截 API，脚本内已清空 *_PROXY。
 */
import "./load-env";
import * as fs from "fs";
import * as path from "path";
import { execFileSync } from "child_process";
import { randomUUID } from "crypto";

for (const k of ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]) delete process.env[k];

const ROOT = path.resolve(__dirname, "..");
const AD_AUDIO = path.join(ROOT, "public", "ad_audio");

const APPID = process.env.TTS_APPID || "";
const TOKEN = process.env.TTS_ACCESS_TOKEN || "";
const CLUSTER = process.env.TTS_CLUSTER || "volcano_tts";
const VOICE = process.env.TTS_VOICE_TYPE || "";
const ENDPOINT = process.env.TTS_ENDPOINT || "https://openspeech.bytedance.com/api/v1/tts";

// 掐头去尾的静音/呼吸（leading + areverse 去 trailing）
const TRIM_AF =
  "silenceremove=start_periods=1:start_silence=0.03:start_threshold=-40dB:detection=peak,areverse," +
  "silenceremove=start_periods=1:start_silence=0.03:start_threshold=-40dB:detection=peak,areverse";

const MAX_ATEMPO = 1.5; // 配音比时间窗长时，最多加速到 1.5x，再长就裁尾（极少触发）

interface Cue { i: number; start: number; end: number; text: string }

// 火山 TTS HTTP：Authorization 头是 "Bearer;TOKEN"（分号，火山特有），data 字段是 base64 音频
async function synth(text: string, speed: number): Promise<Buffer> {
  const body = {
    app: { appid: APPID, token: TOKEN, cluster: CLUSTER },
    user: { uid: "promo-pipeline" },
    audio: { voice_type: VOICE, encoding: "mp3", speed_ratio: speed },
    request: { reqid: randomUUID(), text, operation: "query" },
  };
  const r = await fetch(ENDPOINT, {
    method: "POST",
    headers: { Authorization: `Bearer;${TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j: any = await r.json().catch(() => ({}));
  if (!r.ok || j.code !== 3000 || !j.data) {
    throw new Error(`TTS ${r.status} code=${j.code} msg=${j.message || ""} ${JSON.stringify(j).slice(0, 200)}`);
  }
  return Buffer.from(j.data, "base64");
}

function durationOf(file: string): number {
  const out = execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", file], { encoding: "utf8" });
  return parseFloat(out.trim());
}

// SRT 时间 00:00:04,500 → 秒
function parseTime(t: string): number {
  const m = t.trim().match(/(\d+):(\d+):(\d+)[,.](\d+)/);
  if (!m) return NaN;
  return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]) + (+m[4]) / 1000;
}

function parseSrt(file: string): Cue[] {
  const raw = fs.readFileSync(file, "utf8").replace(/\r/g, "");
  const blocks = raw.split(/\n\s*\n/).map((b) => b.trim()).filter(Boolean);
  const cues: Cue[] = [];
  for (const b of blocks) {
    const lines = b.split("\n");
    const tl = lines.find((l) => l.includes("-->"));
    if (!tl) continue;
    const [s, e] = tl.split("-->");
    const text = lines.slice(lines.indexOf(tl) + 1).join(" ").trim();
    if (!text) continue;
    cues.push({ i: cues.length, start: parseTime(s), end: parseTime(e), text });
  }
  return cues;
}

async function main() {
  const name = process.argv[2];
  const srtArg = process.argv[3];
  const speed = parseFloat(process.argv[4] || "1.0");
  if (!name || !srtArg) { console.error("用法: ts-node scripts/promo-tts.ts <name> <srt路径> [speed=1.0]"); process.exit(1); }
  if (!APPID || !TOKEN) { console.error("缺 TTS_APPID / TTS_ACCESS_TOKEN（见 .env）。"); process.exit(1); }
  if (!VOICE) { console.error("缺 TTS_VOICE_TYPE 音色ID（.env）。"); process.exit(1); }
  const srtPath = path.isAbsolute(srtArg) ? srtArg : path.join(ROOT, srtArg);
  if (!fs.existsSync(srtPath)) { console.error(`缺字幕: ${srtPath}`); process.exit(1); }

  const cues = parseSrt(srtPath);
  if (!cues.length) { console.error("SRT 解析不到字幕。"); process.exit(1); }
  const total = cues[cues.length - 1].end;

  const segDir = path.join(AD_AUDIO, name);
  fs.mkdirSync(segDir, { recursive: true });

  console.log(`▶ 产品宣传配音 ${name}（${cues.length} 句，锁定 SRT 共 ${total.toFixed(1)}s，voice=${VOICE}）...`);
  // 逐句：合成 → 去气口 → 贴合到 [start,end] 时间窗（pad 补静音 / atempo 压缩），输出 fit_NN.mp3 = 精确窗长
  for (const c of cues) {
    const base = `seg_${String(c.i).padStart(2, "0")}`;
    const fit = `fit_${String(c.i).padStart(2, "0")}.mp3`;
    const win = +(c.end - c.start).toFixed(3);
    const rawFile = path.join(segDir, `${base}.raw.mp3`);
    const trimFile = path.join(segDir, `${base}.mp3`);
    try {
      if (!(fs.existsSync(trimFile) && fs.statSync(trimFile).size > 0)) {
        fs.writeFileSync(rawFile, await synth(c.text, speed));
        execFileSync("ffmpeg", ["-y", "-i", `${base}.raw.mp3`, "-af", TRIM_AF, `${base}.mp3`], { cwd: segDir, stdio: "ignore" });
        fs.rmSync(rawFile, { force: true });
      }
      const dur = durationOf(trimFile);
      // 长了→加速到窗内（封顶 1.5x）；短了→末尾补静音。最后统一 -t win 截到精确窗长。
      const af: string[] = [];
      if (dur > win + 0.03) {
        const tempo = Math.min(dur / win, MAX_ATEMPO);
        af.push(`atempo=${tempo.toFixed(4)}`);
      }
      af.push("apad"); // 末尾无限补静音，配合 -t 截成精确窗长
      execFileSync("ffmpeg", ["-y", "-i", `${base}.mp3`, "-af", af.join(","), "-t", String(win), fit], { cwd: segDir, stdio: "ignore" });
      const fitDur = durationOf(path.join(segDir, fit));
      console.log(`  · [${c.i}] 窗${win.toFixed(2)}s 原音${dur.toFixed(2)}s → ${fitDur.toFixed(2)}s${dur > win + 0.03 ? " (加速)" : " (补静音)"}  「${c.text.slice(0, 14)}…」`);
    } catch (e) {
      console.error(`  ✘ 句 [${c.i}] 失败: ${(e as Error).message}`);
      process.exit(1);
    }
  }

  // 拼接所有 fit_NN.mp3（连续时间窗）→ 与 SRT 完全对齐的整轨。concat 滤镜避免中文路径/帧毛刺。
  const inputs: string[] = [];
  const labels: string[] = [];
  cues.forEach((c, i) => { inputs.push("-i", `fit_${String(c.i).padStart(2, "0")}.mp3`); labels.push(`[${i}:a]`); });
  const filter = `${labels.join("")}concat=n=${cues.length}:v=0:a=1[a]`;
  execFileSync("ffmpeg", ["-y", ...inputs, "-filter_complex", filter, "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "2", "_voice.mp3"], { cwd: segDir, stdio: "inherit" });
  const voiceOut = path.join(AD_AUDIO, `${name}.voice.mp3`);
  fs.renameSync(path.join(segDir, "_voice.mp3"), voiceOut);
  console.log(`✔ 配音完成：${durationOf(voiceOut).toFixed(2)}s（应≈${total.toFixed(1)}s）→ ad_audio/${name}.voice.mp3`);
}

main().catch((e) => { console.error("✘ 配音失败:", e?.message || e); process.exit(1); });
