/**
 * 实体信息流广告 · P1 配音（火山豆包语音合成-2.0）。
 * 读 <name>.adplan.json 的 segments，逐句 TTS 合成 → ffmpeg 拼接成整条音轨 →
 * 同时产「句级时间表」<name>.adtiming.json（每段 start/end/dur），给 P4 烧字幕 + 按句切素材用。
 *
 * 为什么逐句合成再拼：每段音频时长 = 该句字幕/素材的展示时长，天然得到精确时间轴，免去 Whisper 对齐。
 *
 * 用法: ts-node scripts/tts.ts <name> [speed=1.0]
 *   产物：public/ad_audio/<name>/seg_NN.mp3（分段，调试用）
 *         public/ad_audio/<name>.voice.mp3（整条配音）
 *         public/scripts/<name>.adtiming.json（句级时间表）
 *
 * 环境变量（见 .env）：TTS_APPID / TTS_ACCESS_TOKEN / TTS_CLUSTER / TTS_VOICE_TYPE / TTS_ENDPOINT
 * 系统死代理会拦截 API，脚本内已清空 *_PROXY。
 */
import "./load-env";
import * as fs from "fs";
import * as path from "path";
import { execFileSync } from "child_process";
import { createHash, randomUUID } from "crypto";
import { requestTtsAudio, executeTtsWithReceipt, TtsExecution } from "./tts-execution";
import { reconcileVoiceTimeline } from "./ad-voice-timing";
import { cachedSegmentMatches, segmentCacheDigest } from "./ad-voice-cache";

for (const k of ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]) delete process.env[k];

const ROOT = path.resolve(__dirname, "..");
const SCRIPTS = path.join(ROOT, "public", "scripts");
const AD_AUDIO = path.join(ROOT, "public", "ad_audio");

// 多音字读音纠正：只改喂给 TTS 的文本，不动字幕（字幕仍用原字）。用同音字替换，逼 TTS 读对。
// 以后遇到读错的多音字往这加一行即可。
const PRON_FIX: [RegExp, string][] = [
  [/行车/g, "航车"], // 行车(行吊/天车)=háng chē，豆包默认读 xíng；航=háng 同音纠正
];
const fixPron = (t: string) => PRON_FIX.reduce((s, [re, rep]) => s.replace(re, rep), t);

// 去气口：掐头去尾的静音/呼吸（leading + 用 areverse 反转去 trailing）
const TRIM_AF =
  "silenceremove=start_periods=1:start_silence=0.03:start_threshold=-40dB:detection=peak,areverse," +
  "silenceremove=start_periods=1:start_silence=0.03:start_threshold=-40dB:detection=peak,areverse";

const VOICE = process.env.TTS_VOICE_TYPE || "";

interface AdSeg { text: string; query: string; prompt: string; type: "video" | "image" }
interface Timing { i: number; text: string; start: number; end: number; dur: number; audio: string }

interface TtsArgs {
  name: string;
  speed: number;
  explicitPlan?: string;
  explicitTiming?: string;
  explicitVoice?: string;
  explicitSegments?: string;
  explicitExecution?: string;
}

function parseArgs(argv: string[]): TtsArgs {
  const explicit = argv.some((value) => value.startsWith("--"));
  if (!explicit) {
    const [name, speedText = "1.0", ...extra] = argv;
    if (!name || extra.length) throw new Error("用法: ts-node scripts/tts.ts <name> [speed=1.0]");
    return { name, speed: Number.parseFloat(speedText) };
  }
  const allowed = new Set(["--plan", "--speed", "--timing-output", "--voice-output", "--segments-dir", "--execution-output"]);
  const values = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith("--") || !value || value.startsWith("--")) throw new Error("显式调用缺少参数值");
    if (!allowed.has(flag)) throw new Error(`未知参数：${flag}`);
    values.set(flag, value);
  }
  const explicitPlan = values.get("--plan");
  const explicitTiming = values.get("--timing-output");
  const explicitVoice = values.get("--voice-output");
  const explicitSegments = values.get("--segments-dir");
  if (!explicitPlan || !explicitTiming || !explicitVoice || !explicitSegments) {
    throw new Error("显式调用必须提供 --plan、--timing-output、--voice-output 和 --segments-dir");
  }
  return {
    name: path.basename(explicitPlan).replace(/\.adplan\.json$/i, "") || "ad-task",
    speed: Number.parseFloat(values.get("--speed") || "1.0"),
    explicitPlan,
    explicitTiming,
    explicitVoice,
    explicitSegments,
    explicitExecution: values.get("--execution-output"),
  };
}

// 火山 TTS HTTP：Authorization 头是 "Bearer;TOKEN"（分号，非空格，火山特有），data 字段是 base64 音频
async function synth(text: string, speed: number, record?: (event: TtsExecution) => void): Promise<Buffer> {
  if (!record) return requestTtsAudio(text, speed, process.env);
  return (await executeTtsWithReceipt(text, speed, process.env, {
    profileConfigDigest: process.env.CUTROOM_TTS_PROFILE_DIGEST || undefined,
    compiledConfigFingerprint: process.env.CUTROOM_TTS_CONFIG_FINGERPRINT || undefined,
    record,
  })).audio;
}

// Resolve existing ancestors to detect aliases, including Windows junctions.
function canonicalOutputPath(value: string): string {
  let current = path.resolve(value);
  const tail: string[] = [];
  while (!fs.existsSync(current)) {
    tail.unshift(path.basename(current));
    const parent = path.dirname(current);
    if (parent === current) throw new Error("TTS_EXECUTION_PATH_INVALID");
    current = parent;
  }
  const resolved = path.join(fs.realpathSync(current), ...tail);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function validateExecutionOutput(output: string, reserved: string[], segmentDirectory: string) {
  const target = canonicalOutputPath(output);
  const relative = path.relative(canonicalOutputPath(segmentDirectory), target);
  const inSegments = relative === "" || (!path.isAbsolute(relative) && relative !== ".."
    && !relative.startsWith(".." + path.sep));
  if (inSegments || reserved.some(value => canonicalOutputPath(value) === target)) {
    throw new Error("TTS_EXECUTION_PATH_COLLISION");
  }
}

function executionReporter(output: string) {
  const target = path.resolve(output);
  for (let current = path.dirname(target);; current = path.dirname(current)) {
    if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) throw new Error("TTS_EXECUTION_PATH_INVALID");
    if (path.dirname(current) === current) break;
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const report = { schema_version: 1, run_id: randomUUID(), events: [] as any[], cached_sources: [] as any[] };
  // Every controlled run needs its own output. Never erase a previous intent.
  fs.writeFileSync(target, JSON.stringify(report), { flag: "wx" });
  const save = () => {
    const temporary = target + "." + randomUUID() + ".tmp";
    try {
      fs.writeFileSync(temporary, JSON.stringify(report), { flag: "wx" });
      fs.renameSync(temporary, target);
    } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  };
  return {
    event: (index: number, event: TtsExecution) => { report.events.push({ segment_index: index, ...event }); save(); },
    cache: (index: number, sourceSha: string) => {
      report.cached_sources.push({ segment_index: index, sha256: sourceSha, status: "cached_unverified" }); save();
    },
  };
}

function durationOf(file: string): number {
  const out = execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", file], { encoding: "utf8" });
  const duration = Number.parseFloat(out.trim());
  if (!Number.isFinite(duration) || duration <= 0) throw new Error("AD_VOICE_DURATION_INVALID: 音频文件时长无效");
  return duration;
}

function audioSha256(file: string): string {
  return createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

async function main() {
  let args: TtsArgs;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error((error as Error).message);
    process.exit(1);
    return;
  }
  const { name, speed, explicitPlan, explicitTiming, explicitVoice, explicitSegments, explicitExecution } = args;
  if (!Number.isFinite(speed) || speed <= 0) { console.error("speed 必须是正数"); process.exit(1); }
  const protocol = (process.env.TTS_PROTOCOL || "volcengine").toLowerCase();
  if (protocol === "volcengine_v3") {
    if (!process.env.TTS_API_KEY || !process.env.TTS_RESOURCE_ID) {
      console.error("缺 TTS_API_KEY 或 TTS_RESOURCE_ID（请在设置中保存新版豆包 API Key）。");
      process.exit(1);
    }
  } else {
    if (!process.env.TTS_ACCESS_TOKEN) { console.error("缺 TTS_ACCESS_TOKEN（见设置或 .env）。"); process.exit(1); }
    if (protocol !== "openai" && !process.env.TTS_APPID) { console.error("缺 TTS_APPID（见 .env）。"); process.exit(1); }
  }
  if (protocol === "openai" && !process.env.TTS_MODEL) { console.error("缺 TTS_MODEL（请检查任务冻结配置）。"); process.exit(1); }
  if (!VOICE) { console.error("缺 TTS_VOICE_TYPE 音色ID（.env 里填，火山控制台 语音合成 音色列表获取）。"); process.exit(1); }

  const planPath = explicitPlan ? path.resolve(explicitPlan) : path.join(SCRIPTS, name + ".adplan.json");
  if (!fs.existsSync(planPath)) { console.error(`缺分镜: ${planPath}（先跑 plan-ad）`); process.exit(1); }
  const segs: AdSeg[] = JSON.parse(fs.readFileSync(planPath, "utf8")).segments;

  const segDir = explicitSegments ? path.resolve(explicitSegments) : path.join(AD_AUDIO, name);
  fs.mkdirSync(segDir, { recursive: true });

  const voiceOut = explicitVoice ? path.resolve(explicitVoice) : path.join(AD_AUDIO, name + ".voice.mp3");
  const timingOut = explicitTiming ? path.resolve(explicitTiming) : path.join(SCRIPTS, name + ".adtiming.json");
  if (explicitExecution) validateExecutionOutput(explicitExecution, [planPath, voiceOut, timingOut], segDir);

  console.log(`▶ 配音 ${name}（${segs.length} 段, voice=${VOICE}, speed=${speed}）...`);
  const report = explicitExecution ? executionReporter(explicitExecution) : null;
  const timing: Timing[] = [];
  let cursor = 0;
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    const base = `seg_${String(i).padStart(2, "0")}`;
    const file = path.join(segDir, `${base}.mp3`);
    const cacheFile = path.join(segDir, `${base}.cache.json`);
    try {
      const spokenText = fixPron(seg.text);
      const inputDigest = segmentCacheDigest({
        text: seg.text, spokenText, speed, voice: VOICE, protocol,
        model: process.env.TTS_MODEL || "",
        resourceId: process.env.TTS_RESOURCE_ID || "",
        endpoint: process.env.TTS_ENDPOINT || "",
        cluster: process.env.TTS_CLUSTER || "",
        appId: process.env.TTS_APPID || "",
        credentialFingerprint: createHash("sha256").update(
          protocol === "volcengine_v3" ? process.env.TTS_API_KEY || "" : process.env.TTS_ACCESS_TOKEN || "",
        ).digest("hex"),
        trimFilter: TRIM_AF,
      });
      let cached = false;
      if (fs.existsSync(file) && fs.statSync(file).size > 0 && fs.existsSync(cacheFile)) {
        try {
          cached = cachedSegmentMatches(
            JSON.parse(fs.readFileSync(cacheFile, "utf8")), inputDigest, audioSha256(file),
          );
        } catch {
          cached = false;
        }
      }
      if (!cached) {
        const buf = await synth(spokenText, speed, report ? event => report.event(i, event) : undefined);
        fs.writeFileSync(path.join(segDir, `${base}.raw.mp3`), buf);
        // 去气口（cwd 相对名规避中文路径）
        execFileSync("ffmpeg", ["-y", "-i", `${base}.raw.mp3`, "-af", TRIM_AF, `${base}.mp3`], { cwd: segDir, stdio: "ignore" });
        // 不删 raw.mp3 —— safe-delete 守卫会 abort 进程，try-catch 拦不住。下次 synth 会覆盖。
      }
      if (cached && report) report.cache(i, audioSha256(file));
      const dur = durationOf(file);
      if (!cached) {
        fs.writeFileSync(cacheFile, JSON.stringify({
          inputDigest, audioSha256: audioSha256(file),
        }), "utf8");
      }
      timing.push({ i, text: seg.text, start: +cursor.toFixed(3), end: +(cursor + dur).toFixed(3), dur: +dur.toFixed(3), audio: path.relative(ROOT, file) });
      cursor += dur;
      console.log(`  · [${i}] ${dur.toFixed(2)}s${cached ? " (缓存)" : ""}  「${seg.text.slice(0, 16)}…」`);
    } catch (e) {
      console.error(`  ✘ 段 [${i}] 合成失败: ${(e as Error).message}`);
      process.exit(1);
    }
  }

  // 拼接整条音轨（concat demuxer + 重编码，避免 mp3 直拷的帧边界毛刺）
  // 用 concat 滤镜（多 -i 输入），不写 list 文件，规避中文路径 + list 解析坑。
  // cwd 设到分段目录、相对 ASCII 名（seg_NN.mp3 / _voice.mp3），ffmpeg 不接触中文路径。
  const inputs: string[] = [];
  const labels: string[] = [];
  timing.forEach((t, i) => { inputs.push("-i", path.basename(t.audio)); labels.push(`[${i}:a]`); });
  const filter = `${labels.join("")}concat=n=${timing.length}:v=0:a=1[a]`;
  execFileSync("ffmpeg", ["-y", ...inputs, "-filter_complex", filter, "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "2", "_voice.mp3"], { cwd: segDir, stdio: "inherit" });
  fs.mkdirSync(path.dirname(voiceOut), { recursive: true });
  fs.renameSync(path.join(segDir, "_voice.mp3"), voiceOut);

  const measuredVoiceSeconds = durationOf(voiceOut);
  const finalTimeline = reconcileVoiceTimeline(timing, measuredVoiceSeconds);

  fs.mkdirSync(path.dirname(timingOut), { recursive: true });
  fs.writeFileSync(timingOut, JSON.stringify({ name, speed, ...finalTimeline }, null, 2), "utf8");
  console.log(`✔ 配音完成：整条音轨实测 ${finalTimeline.total.toFixed(2)}s → ad_audio/${name}.voice.mp3 + ${name}.adtiming.json`);
}

main().catch((e) => { console.error("✘ 配音失败:", e?.message || e); process.exit(1); });
