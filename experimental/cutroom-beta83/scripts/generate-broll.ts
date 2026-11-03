/**
 * Seedance B-roll generation.
 *
 * Explicit mode is the audited paid-generation path:
 *   ts-node scripts/generate-broll.ts --requests requests.json --run-id run-20260824 --output-dir public/generated --manifest provenance.json
 * It accepts exactly three unique requests and writes only the supplied output directory and provenance manifest.
 *
 * Legacy mode remains available for existing pending files:
 *   ts-node scripts/generate-broll.ts <name>
 */
import "./load-env";
import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { promisify } from "node:util";
import {
  BrollRequest,
  CreateTaskRequest,
  formatProviderText,
  GenerationDependencies,
  GenerationManifest,
  GenerationProvenanceStore,
  runGenerationBatch,
} from "../src/broll-generation";

for (const key of ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]) delete process.env[key];

const execFileAsync = promisify(execFile);
const ROOT = path.resolve(__dirname, "..");
const SCRIPTS = path.join(ROOT, "public", "scripts");
const LIBRARY = path.join(ROOT, "public", "library");
const API_KEY = process.env.SEEDANCE_API_KEY || process.env.ARK_API_KEY || "";
const MODEL = process.env.SEEDANCE_MODEL || "";
const BASE_URL = process.env.SEEDANCE_BASE_URL || "https://ark.cn-beijing.volces.com/api/v3";
const POLL_INTERVAL_MS = 6000;
const POLL_TIMEOUT_MS = 480000;
const SEED = process.env.SEEDANCE_SEED;

interface Pending {
  query: string;
  prompt?: string;
  anchorText: string;
  kind: "broll" | "image";
  at: number;
}

interface ExplicitArgs {
  requests: string;
  runId: string;
  outputDir: string;
  manifest: string;
}

function requireProviderConfiguration(): void {
  if (!API_KEY) throw new Error("缺 SEEDANCE_API_KEY（或 ARK_API_KEY）。");
  if (!MODEL) throw new Error("缺 SEEDANCE_MODEL：填 Seedance 模型名或推理接入点ID（doubao-seedance-… / ep-…）。");
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function createProviderDependencies(): GenerationDependencies {
  return {
    async createTask(request: CreateTaskRequest): Promise<string> {
      const response = await fetch(`${BASE_URL}/contents/generations/tasks`, {
        method: "POST",
        headers: { Authorization: `Bearer ${API_KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          model: request.model,
          content: [{ type: "text", text: request.providerText }],
        }),
      });
      if (!response.ok) throw new Error(`建任务 ${response.status}: ${(await response.text()).slice(0, 200)}`);
      const body: { id?: string } = await response.json();
      if (!body.id) throw new Error("建任务无 id");
      return body.id;
    },

    async pollTask(taskId: string): Promise<string> {
      const deadline = Date.now() + POLL_TIMEOUT_MS;
      while (Date.now() < deadline) {
        const response = await fetch(`${BASE_URL}/contents/generations/tasks/${taskId}`, {
          headers: { Authorization: `Bearer ${API_KEY}` },
        });
        if (!response.ok) throw new Error(`查任务 ${response.status}`);
        const body: {
          status?: string;
          content?: { video_url?: string };
          error?: { message?: string };
        } = await response.json();
        if (body.status === "succeeded") {
          if (!body.content?.video_url) throw new Error("成功但无 video_url");
          return body.content.video_url;
        }
        if (body.status === "failed" || body.status === "cancelled") {
          throw new Error(`任务${body.status}: ${body.error?.message || ""}`);
        }
        await sleep(POLL_INTERVAL_MS);
      }
      throw new Error("轮询超时");
    },

    async download(url: string, destination: string): Promise<void> {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`下载 ${response.status}`);
      const handle = await fs.promises.open(destination, "wx");
      try {
        await handle.writeFile(Buffer.from(await response.arrayBuffer()));
      } finally {
        await handle.close();
      }
    },

    async probe(file: string) {
      const { stdout } = await execFileAsync("ffprobe", [
        "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", file,
      ]);
      const parsed: { streams?: Array<{ width?: number; height?: number }>; format?: { duration?: string } } = JSON.parse(stdout);
      const stream = parsed.streams?.[0];
      const duration = Number(parsed.format?.duration);
      if (!stream?.width || !stream.height || !Number.isFinite(duration)) throw new Error(`ffprobe 输出不完整: ${file}`);
      return { width: stream.width, height: stream.height, duration };
    },

    async sha256(file: string): Promise<string> {
      return new Promise((resolve, reject) => {
        const hash = createHash("sha256");
        const input = fs.createReadStream(file);
        input.on("error", reject);
        input.on("data", (chunk) => hash.update(chunk));
        input.on("end", () => resolve(hash.digest("hex")));
      });
    },
  };
}

function parseExplicitArgs(argv: string[]): ExplicitArgs | undefined {
  if (!argv.includes("--requests")) return undefined;
  const values: Partial<ExplicitArgs> = {};
  const flagMap: Record<string, keyof ExplicitArgs> = {
    "--requests": "requests",
    "--run-id": "runId",
    "--output-dir": "outputDir",
    "--manifest": "manifest",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    const property = flagMap[flag];
    if (!property) throw new Error(`显式模式未知参数: ${flag}`);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`缺少 ${flag} 的值`);
    if (values[property]) throw new Error(`重复参数: ${flag}`);
    values[property] = value;
    index += 1;
  }
  if (!values.requests || !values.runId || !values.outputDir || !values.manifest) {
    throw new Error("显式模式必须提供 --requests --run-id --output-dir --manifest");
  }
  return values as ExplicitArgs;
}

function manifestJson(manifest: GenerationManifest): string {
  return `${JSON.stringify(manifest, null, 2)}\n`;
}

export function createFileProvenanceStore(manifestPath: string): GenerationProvenanceStore {
  const absolute = path.resolve(manifestPath);

  async function persistAtomically(manifest: GenerationManifest): Promise<void> {
    const directory = path.dirname(absolute);
    const temporary = path.join(directory, `.${path.basename(absolute)}.${process.pid}.${randomUUID()}.tmp`);
    await fs.promises.mkdir(directory, { recursive: true });
    try {
      await fs.promises.writeFile(temporary, manifestJson(manifest), { encoding: "utf8", flag: "wx" });
      await fs.promises.rename(temporary, absolute);
    } catch (error) {
      await fs.promises.rm(temporary, { force: true }).catch(() => undefined);
      throw error;
    }
  }

  return {
    async reserve(manifest: GenerationManifest): Promise<void> {
      await fs.promises.mkdir(path.dirname(absolute), { recursive: true });
      await fs.promises.writeFile(absolute, manifestJson(manifest), { encoding: "utf8", flag: "wx" });
    },
    persist: persistAtomically,
  };
}

async function runExplicitMode(args: ExplicitArgs): Promise<void> {
  requireProviderConfiguration();
  const requests: BrollRequest[] = JSON.parse(await fs.promises.readFile(path.resolve(args.requests), "utf8"));
  const manifestPath = path.resolve(args.manifest);
  const result = await runGenerationBatch(requests, {
    runId: args.runId,
    outputDir: args.outputDir,
    model: MODEL,
    dependencies: createProviderDependencies(),
    provenanceStore: createFileProvenanceStore(manifestPath),
  });
  console.log(`✔ 已生成受控批次 ${result.runId}，provenance: ${manifestPath}`);
}

export function formatLegacyProviderText(prompt: string, seed = SEED): string {
  return `${formatProviderText(prompt)}${seed ? ` --seed ${seed}` : ""}`;
}

function legacySlug(query: string): string {
  return "seed_" + query.replace(/[\s,，、/]+/g, "_").replace(/[^\w一-龥_]/g, "").slice(0, 36);
}

async function runLegacyMode(name: string): Promise<void> {
  requireProviderConfiguration();
  const pendingPath = path.join(SCRIPTS, `${name}.pending.json`);
  if (!fs.existsSync(pendingPath)) {
    console.log(`无待补清单 ${name}.pending.json（先跑 compile-plan.ts）`);
    return;
  }
  const pending: Pending[] = JSON.parse(fs.readFileSync(pendingPath, "utf8"));
  fs.mkdirSync(LIBRARY, { recursive: true });
  const legacyManifestPath = path.join(LIBRARY, "manifest.json");
  const provider = createProviderDependencies();

  // One local owner performs preflight, submission and persistence together.
  const lockPath = path.join(LIBRARY, ".seedance-legacy.lock");
  const lock = fs.openSync(lockPath, "wx");
  const jobsPath = path.join(LIBRARY, "seedance-jobs.json");
  type LegacyJob = { taskId?: string; file: string; prompt: string; status: string; error?: string };
  const writeJson = (file: string, value: unknown) => {
    const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(value, null, 2), "utf8");
    fs.renameSync(temporary, file);
  };
  try {
    const legacyManifest: Record<string, string> = fs.existsSync(legacyManifestPath)
      ? JSON.parse(fs.readFileSync(legacyManifestPath, "utf8"))
      : {};
    const jobs: Record<string, LegacyJob> = fs.existsSync(jobsPath)
      ? JSON.parse(fs.readFileSync(jobsPath, "utf8")) : {};
    const planned = new Map<string, { item: Pending; file: string; prompt: string }>();
    const outputs = new Map<string, string>();
    for (const item of pending) {
      if (!item.query?.trim()) throw new Error("LEGACY_QUERY_INVALID: query 不能为空");
      const prompt = item.prompt || item.query;
      const file = legacyManifest[item.query] || `${legacySlug(item.query)}.mp4`;
      if (path.basename(file) !== file || !file.endsWith(".mp4")) throw new Error("LEGACY_OUTPUT_INVALID: manifest 输出必须是本目录 MP4");
      const outputKey = file.toLowerCase();
      const owner = outputs.get(outputKey);
      if (owner && owner !== item.query) throw new Error(`LEGACY_OUTPUT_CONFLICT: ${owner} 与 ${item.query} 使用同名输出`);
      if (planned.has(item.query) && planned.get(item.query)!.prompt !== prompt) throw new Error(`LEGACY_QUERY_CONFLICT: ${item.query} 的 prompt 不一致`);
      for (const [query, saved] of Object.entries(legacyManifest)) {
        if (query !== item.query && saved.toLowerCase() === outputKey) throw new Error(`LEGACY_OUTPUT_CONFLICT: ${file} 已属于 ${query}`);
      }
      outputs.set(outputKey, item.query);
      planned.set(item.query, { item, file, prompt });
    }
    const work: Array<{ item: Pending; file: string; prompt: string }> = [];
    for (const entry of planned.values()) {
      const { item, file, prompt } = entry;
      const destination = path.join(LIBRARY, file);
      if (fs.existsSync(destination)) {
        if (legacyManifest[item.query] !== file) throw new Error(`LEGACY_OUTPUT_CONFLICT: ${file} 已存在且没有匹配 query`);
        if (jobs[item.query] && jobs[item.query].prompt !== prompt) throw new Error(`LEGACY_QUERY_CONFLICT: ${item.query} 的 prompt 已改变`);
        const probe = await provider.probe(destination);
        if (!(probe.duration > 0 && probe.width > 0 && probe.height > 0)) throw new Error(`LEGACY_CACHE_INVALID: ${file}`);
        console.log(`  · 复用已核验缓存 «${item.query}» → ${file}`);
        continue;
      }
      if (jobs[item.query]) throw new Error(`LEGACY_JOB_REQUIRES_REVIEW: ${item.query} 已有 ${jobs[item.query].status} 记录，请先核对供应商任务 ${jobs[item.query].taskId || "提交状态未知"}`);
      work.push(entry);
    }
    let failed = 0;
    console.log(`▶ Seedance 生成 ${work.length} 条（${MODEL} @ ${BASE_URL}）...`);
    for (const { item, file, prompt } of work) {
      try {
        // Persist intent before the paid request; uncertain submissions cannot be silently retried.
        jobs[item.query] = { file, prompt, status: "submitting" };
        writeJson(jobsPath, jobs);
        const taskId = await provider.createTask({ key: item.query, prompt, model: MODEL, providerText: formatLegacyProviderText(prompt) });
        jobs[item.query] = { file, prompt, taskId, status: "submitted" };
        writeJson(jobsPath, jobs);
        const videoUrl = await provider.pollTask(taskId);
        await provider.download(videoUrl, path.join(LIBRARY, file));
        const probe = await provider.probe(path.join(LIBRARY, file));
        if (!(probe.duration > 0)) throw new Error("LEGACY_OUTPUT_INVALID: 下载视频时长无效");
        legacyManifest[item.query] = file;
        writeJson(legacyManifestPath, legacyManifest);
        jobs[item.query].status = "complete";
        writeJson(jobsPath, jobs);
        console.log(`    ✔ → library/${file}`);
      } catch (error) {
        failed++;
        jobs[item.query].status = "failed";
        jobs[item.query].error = (error as Error).message;
        writeJson(jobsPath, jobs);
        console.warn(`    ✘ «${item.query}»: ${(error as Error).message}`);
      }
    }
    if (failed) throw new Error(`LEGACY_GENERATION_FAILED: ${failed} 条未完成，任务记录见 ${jobsPath}`);
    console.log(`✔ 完成。重跑: npx ts-node scripts/compile-plan.ts ${name}`);
  } finally {
    fs.closeSync(lock);
    fs.unlinkSync(lockPath);
  }
}

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  const explicit = parseExplicitArgs(argv);
  if (explicit) return runExplicitMode(explicit);
  if (argv.length !== 1 || argv[0].startsWith("--")) {
    throw new Error("用法: ts-node scripts/generate-broll.ts <name> | --requests <json> --run-id <id> --output-dir <dir> --manifest <json>");
  }
  return runLegacyMode(argv[0]);
}

if (require.main === module) {
  main().catch((error) => {
    console.error("✘ 生成失败:", error?.message || error);
    process.exit(1);
  });
}

export { parseExplicitArgs };
