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
import { randomUUID } from "crypto";
import { buildTtsHttpRequest, parseVolcengineSseAudio } from "./tts-provider";

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
}

function parseArgs(argv: string[]): TtsArgs {
  const explicit = argv.some((value) => value.startsWith("--"));
  if (!explicit) {
    const [name, speedText = "1.0", ...extra] = argv;
    if (!name || extra.length) throw new Error("用法: ts-node scripts/tts.ts <name> [speed=1.0]");
    return { name, speed: Number.parseFloat(speedText) };
  }
  const allowed = new Set(["--plan", "--speed", "--timing-output", "--voice-output", "--segments-dir"]);
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
  };
}

// 火山 TTS HTTP：Authorization 头是 "Bearer;TOKEN"（分号，非空格，火山特有），data 字段是 base64 音频
async function synth(text: string, speed: number): Promise<Buffer> {
  const request = buildTtsHttpRequest(text, speed, process.env, randomUUID());
  const r = await fetch(request.endpoint, {
    method: "POST",
    headers: request.headers,
    body: JSON.stringify(request.body),
  });
  if (request.responseKind === "audio") {
    if (!r.ok) {
      const detail = (await r.text().catch(() => "")).slice(0, 200);
      throw new Error(`TTS ${r.status} ${detail}`);
    }
    return Buffer.from(await r.arrayBuffer());
  }
  if (request.responseKind === "volcengine-sse") {
    const payload = await r.text();
    if (!r.ok) {
      if (payload.toLowerCase().includes("resource not granted")) {
        throw new Error("当前 API Key 未开通所选配音服务版本，请在火山控制台开通后重试");
      }
      if (r.status === 401 || r.status === 403) {
        throw new Error("豆包 API Key 无效或没有访问权限");
      }
      throw new Error(`豆包 TTS 请求失败（HTTP ${r.status}）`);
    }
    return parseVolcengineSseAudio(payload);
  }
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

async function main() {
  let args: TtsArgs;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error((error as Error).message);
    process.exit(1);
    return;
  }
  const { name, speed, explicitPlan, explicitTiming, explicitVoice, explicitSegments } = args;
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

  console.log(`▶ 配音 ${name}（${segs.length} 段, voice=${VOICE}, speed=${speed}）...`);
  const timing: Timing[] = [];
  let cursor = 0;
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    const base = `seg_${String(i).padStart(2, "0")}`;
    const file = path.join(segDir, `${base}.mp3`);
    try {
      const cached = fs.existsSync(file) && fs.statSync(file).size > 0;
      if (!cached) {
        const buf = await synth(fixPron(seg.text), speed);
        fs.writeFileSync(path.join(segDir, `${base}.raw.mp3`), buf);
        // 去气口（cwd 相对名规避中文路径）
        execFileSync("ffmpeg", ["-y", "-i", `${base}.raw.mp3`, "-af", TRIM_AF, `${base}.mp3`], { cwd: segDir, stdio: "ignore" });
        // 不删 raw.mp3 —— safe-delete 守卫会 abort 进程，try-catch 拦不住。下次 synth 会覆盖。
      }
      const dur = durationOf(file);
      timing.push({ i, text: seg.text, start: +cursor.toFixed(3), end: +(cursor + dur).toFixed(3), dur: +dur.toFixed(3), audio: path.relative(ROOT, file) });
      cursor += dur;
      console.log(`  · [${i}] ${dur.toFixed(2)}s${cached ? " (缓存)" : ""}  「${seg.text.slice(0, 16)}…」`);
    } catch (e) {
      console.error(`  ✘ 段 [${i}] 合成失败: ${(e as Error).message}`);
      process.exit(1);
    }
  }

  // 拼接整条音轨（concat demuxer + 重编码，避免 mp3 直拷的帧边界毛刺）
  const voiceOut = explicitVoice ? path.resolve(explicitVoice) : path.join(AD_AUDIO, name + ".voice.mp3");
  // 用 concat 滤镜（多 -i 输入），不写 list 文件，规避中文路径 + list 解析坑。
  // cwd 设到分段目录、相对 ASCII 名（seg_NN.mp3 / _voice.mp3），ffmpeg 不接触中文路径。
  const inputs: string[] = [];
  const labels: string[] = [];
  timing.forEach((t, i) => { inputs.push("-i", path.basename(t.audio)); labels.push(`[${i}:a]`); });
  const filter = `${labels.join("")}concat=n=${timing.length}:v=0:a=1[a]`;
  execFileSync("ffmpeg", ["-y", ...inputs, "-filter_complex", filter, "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "2", "_voice.mp3"], { cwd: segDir, stdio: "inherit" });
  fs.mkdirSync(path.dirname(voiceOut), { recursive: true });
  fs.renameSync(path.join(segDir, "_voice.mp3"), voiceOut);

  const timingOut = explicitTiming
    ? path.resolve(explicitTiming)
    : path.join(SCRIPTS, name + ".adtiming.json");
  fs.mkdirSync(path.dirname(timingOut), { recursive: true });
  fs.writeFileSync(timingOut, JSON.stringify({ name, speed, total: +cursor.toFixed(3), segments: timing }, null, 2), "utf8");
  console.log(`✔ 配音完成：总时长 ${cursor.toFixed(2)}s → ad_audio/${name}.voice.mp3 + ${name}.adtiming.json`);
}

main().catch((e) => { console.error("✘ 配音失败:", e?.message || e); process.exit(1); });
