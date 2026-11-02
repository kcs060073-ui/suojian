import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import test from "node:test";
import ts from "typescript";

const ROOT = path.resolve(__dirname, "..");
const HERE = path.join(ROOT, "runtime", "fixes-20260920", `render-${process.pid}`);
fs.mkdirSync(HERE, { recursive: true });
function fixture(name: string) {
  const dir = path.join(HERE, name); fs.mkdirSync(dir, { recursive: true }); return dir;
}
function ff(args: string[], cwd = HERE): Buffer {
  return execFileSync("ffmpeg", ["-v", "error", "-y", ...args], { cwd, timeout: 90000, maxBuffer: 32 * 1024 * 1024 });
}
function script(name: string, overrides: Record<string, unknown>, globals: Record<string, unknown> = {}) {
  const filename = path.join(ROOT, "scripts", name);
  const realRequire = createRequire(filename);
  const context: any = { exports: {}, module: {}, __dirname: path.dirname(filename), Buffer, setTimeout, console,
    require: (id: string) => id === "./load-env" ? {} : overrides[id] ?? realRequire(id), ...globals };
  if (globals.runAsMain) context.require.main = context.module;
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true, target: ts.ScriptTarget.ES2022 },
  }).outputText, context);
  return context;
}
function captureRender(name: string, argv: string[], dir: string) {
  let command: string[] = []; let work = "";
  script(name, {
    child_process: { execFileSync: (exe: string, args: string[], options: any) => {
      if (exe === "ffmpeg") {
        command = args; work = options.cwd;
        fs.writeFileSync(path.resolve(work, args[args.length - 1]), "captured"); return Buffer.alloc(0);
      }
      if (args[0]?.endsWith("gen-fancy-text.py")) {
        fs.copyFileSync(path.join(dir, "red.png"), args[args.indexOf("--out") + 1]); return Buffer.alloc(0);
      }
      return execFileSync(exe, args, options);
    } },
    os: { tmpdir: () => dir },
    "./render-mix-options": {
      ...require("../scripts/render-mix-options"),
      withRenderMixWorkdir: (_base: string, action: (dir: string) => void) => {
        const work = path.join(dir, "work"); fs.mkdirSync(work, { recursive: true }); return action(work);
      },
    },
  }, { process: { argv: ["node", name, ...argv], env: {}, exit: (code: number) => { throw new Error(`exit ${code}`); } } });
  assert.ok(command.length, "renderer must produce ffmpeg invocation");
  return { command, work };
}
function sample(command: string[], cwd: string, seconds: number) {
  // Exercise the renderer's actual input and filter graph at a small test canvas.
  const args = command.slice(1, command.indexOf("-map"));
  const fi = args.indexOf("-filter_complex") + 1;
  args[fi] = args[fi].replace(/1080/g, "108").replace(/1920/g, "192");
  const label = args[fi].includes("[vout]") ? "[vout]" : "[outv]";
  // Audio outputs must still be consumed even though only pixels are asserted.
  if (args[fi].includes("[aout]")) args[fi] += ";[aout]anullsink";
  else if (label === "[outv]") args[fi] += ";[outa]anullsink";
  return ff([...args, "-map", label, "-ss", String(seconds), "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], cwd);
}

test("ad render honors selected source window, including transition padding", () => {
  const dir = fixture("ad-source");
  ff(["-f", "lavfi", "-i", "color=red:s=32x32:r=30:d=5", "-f", "lavfi", "-i", "color=blue:s=32x32:r=30:d=1", "-f", "lavfi", "-i", "color=lime:s=32x32:r=30:d=1", "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "source.mp4"], dir);
  ff(["-f", "lavfi", "-i", "sine=duration=1.5", "voice.mp3"], dir);
  const plan = path.join(dir, "plan.json");
  fs.writeFileSync(plan, JSON.stringify({ ratio: "9:16", voice: path.join(dir, "voice.mp3"), segments: [{ i: 0, text: "", start: 0, end: 1, dur: 1, src: path.join(dir, "source.mp4"), sourceStart: 5, sourceEnd: 6 }] }));
  const { command, work } = captureRender("render-ad.ts", ["--plan", plan, "--output", path.join(dir, "out.mp4")], dir);
  for (const time of [0.1, 0.9, 1.2]) {
    const pixels = sample(command, work, time);
    assert.ok(pixels.length > 0, `frame ${time} exists`);
    assert.ok(pixels[2] > 200 && pixels[0] < 20 && pixels[1] < 20, `selected blue window at ${time}, got ${[...pixels.subarray(0, 3)]}`);
  }
});

test("ad render applies each confirmed subtitle preset to the generated ASS", () => {
  const presets = JSON.parse(fs.readFileSync(path.join(ROOT, "assets/manifests/subtitle-presets.json"), "utf8")).items;
  for (const [id, font, size, color, margin] of [
    ["sub-heavy-orange", "Source Han Serif SC", "46", "&H0000B3FF", "60"],
    ["sub-ad-smiley-punch", "Smiley Sans Oblique", "72", "&H00FFFFFF", "390"],
    ["sub-keyword-hl", "Alimama ShuHeiTi Bold", "48", "&H00000000", "60"],
  ]) {
    const dir = fixture(String(id)); fs.writeFileSync(path.join(dir, "voice.mp3"), "synthetic");
    const plan = path.join(dir, "plan.json");
    fs.writeFileSync(plan, JSON.stringify({ ratio: "9:16", voice: path.join(dir, "voice.mp3"), subtitlePreset: presets.find((p: any) => p.id === id), segments: [{ i: 0, text: "test", start: 0, end: 1, dur: 1, src: null }] }));
    const { work } = captureRender("render-ad.ts", ["--plan", plan, "--output", path.join(dir, "out.mp4")], dir);
    const ass = fs.readFileSync(path.join(work, "sub.ass"), "utf8");
    const fields = ass.split("\n").find(line => line.startsWith("Style: Default,"))!.split(",");
    assert.deepEqual([fields[1], fields[2], fields[3], fields[21]], [font, size, color, margin]);
    if (id === "sub-keyword-hl") assert.deepEqual([fields[6], fields[15], fields[17]], ["&H0000B3FF", "4", "0"]);

  }
});

test("ad render bounds preview duration, canvas and CRF before building transitions", () => {
  const dir = fixture("ad-preview-profile");
  ff(["-f", "lavfi", "-i", "color=blue:s=32x32:r=30:d=12", "source.mp4"], dir);
  ff(["-f", "lavfi", "-i", "sine=duration=12", "voice.mp3"], dir);
  const plan = path.join(dir, "plan.json");
  fs.writeFileSync(plan, JSON.stringify({
    ratio: "9:16",
    voice: path.join(dir, "voice.mp3"),
    segments: [{ i: 0, text: "preview", start: 0, end: 12, dur: 12, src: path.join(dir, "source.mp4"), sourceStart: 0, sourceEnd: 12 }],
  }));
  const { command } = captureRender("render-ad.ts", [
    "--plan", plan,
    "--output", path.join(dir, "out.mp4"),
    "--max-duration", "8",
    "--width", "540",
    "--height", "960",
    "--crf", "28",
  ], dir);
  const graph = command[command.indexOf("-filter_complex") + 1];
  assert.match(graph, /scale=540:960/);
  assert.match(graph, /trim=duration=8\.400/);
  assert.equal(command[command.indexOf("-crf") + 1], "28");
  assert.equal(command[command.indexOf("-t") + 1], "8");
});

test("ad render normalizes the final mixed audio to the QA loudness target", () => {
  const dir = fixture("ad-loudness");
  ff(["-f", "lavfi", "-i", "color=blue:s=32x32:r=30:d=2", "source.mp4"], dir);
  ff(["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "voice.mp3"], dir);
  const plan = path.join(dir, "plan.json");
  fs.writeFileSync(plan, JSON.stringify({
    ratio: "9:16",
    voice: path.join(dir, "voice.mp3"),
    segments: [{
      i: 0, text: "normalized", start: 0, end: 2, dur: 2,
      src: path.join(dir, "source.mp4"),
    }],
  }));

  const { command } = captureRender(
    "render-ad.ts",
    ["--plan", plan, "--output", path.join(dir, "out.mp4")],
    dir,
  );
  const graph = command[command.indexOf("-filter_complex") + 1];
  assert.match(graph, /loudnorm=I=-16:TP=-1\.5:LRA=11\[aout\]/);
  assert.equal(command[command.indexOf("-map") + 3], "[aout]");
});

test("ad render consumes compiled SFX and visual packaging fields", () => {
  const dir = fixture("ad-packaging-fields");
  ff(["-f", "lavfi", "-i", "color=blue:s=32x32:r=30:d=2", "source.mp4"], dir);
  ff(["-f", "lavfi", "-i", "sine=duration=2", "voice.mp3"], dir);
  ff(["-f", "lavfi", "-i", "sine=frequency=880:duration=0.2", "sfx.wav"], dir);
  ff(["-f", "lavfi", "-i", "color=red:s=16x16", "-frames:v", "1", "brand.png"], dir);
  const plan = path.join(dir, "plan.json");
  fs.writeFileSync(plan, JSON.stringify({
    ratio: "9:16",
    voice: path.join(dir, "voice.mp3"),
    segments: [{ i: 0, text: "subtitle", start: 0, end: 1.5, dur: 1.5, src: path.join(dir, "source.mp4") }],
    sfx: [{ src: path.join(dir, "sfx.wav"), start: 0.25, gain: 0.3 }],
    watermark: { src: path.join(dir, "brand.png"), opacity: 0.8, position: { x: 0.05, y: 0.05, width: 0.2 } },
    titles: [{ text: "TITLE-CARD", start: 0, end: 0.8, style: { fontFamily: "Title Fixture", primaryHex: "#123456", backgroundHex: "#654321" } }],
    fancyTexts: [{ text: "FANCY-TEXT", start: 0.6, end: 1.2, style: { fontFamily: "Fancy Fixture", primaryHex: "#FEDCBA", outlineHex: "#112233" } }],
  }));
  const { command, work } = captureRender("render-ad.ts", ["--plan", plan, "--output", path.join(dir, "out.mp4")], dir);
  const graph = command[command.indexOf("-filter_complex") + 1];
  assert.match(graph, /adelay=250\|250/);
  assert.match(graph, /overlay=/);
  const ass = fs.readFileSync(path.join(work, "sub.ass"), "utf8");
  assert.match(ass, /TITLE-CARD/);
  assert.match(ass, /FANCY-TEXT/);
  assert.match(ass, /Style: PackTitle,Title Fixture/);
  assert.match(ass, /&H00563412/);
  assert.match(ass, /Style: PackFancy,Fancy Fixture/);
  assert.match(ass, /&H00BADCFE/);
});

test("bounded ad preview is a decodable H264 AAC portrait MP4", () => {
  const dir = fixture("ad-preview-real-output");
  ff(["-f", "lavfi", "-i", "color=blue:s=180x320:r=24:d=2", "source.mp4"], dir);
  ff(["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "voice.mp3"], dir);
  const plan = path.join(dir, "plan.json");
  const output = path.join(dir, "preview.mp4");
  fs.writeFileSync(plan, JSON.stringify({
    ratio: "9:16",
    total: 2,
    voice: path.join(dir, "voice.mp3"),
    segments: [{ i: 0, text: "真实预览", start: 0, end: 2, dur: 2, src: path.join(dir, "source.mp4") }],
  }));
  execFileSync(process.execPath, [
    "-r", "ts-node/register", path.join(ROOT, "scripts/render-ad.ts"),
    "--plan", plan, "--output", output,
    "--max-duration", "1.2", "--width", "540", "--height", "960", "--crf", "28",
  ], { cwd: ROOT, timeout: 90000, stdio: "pipe" });
  const probe = JSON.parse(execFileSync("ffprobe", [
    "-v", "error", "-show_streams", "-show_format", "-of", "json", output,
  ], { encoding: "utf8" }));
  const video = probe.streams.find((stream: any) => stream.codec_type === "video");
  const audio = probe.streams.find((stream: any) => stream.codec_type === "audio");
  assert.deepEqual([video.codec_name, video.width, video.height], ["h264", 540, 960]);
  assert.equal(audio.codec_name, "aac");
  assert.ok(Number(probe.format.duration) <= 1.3);
});

test("mix default fade emits a visible image midway through its window", () => {
  const dir = fixture("mix-fade");
  ff(["-f", "lavfi", "-i", "color=red:s=32x32", "-frames:v", "1", "red.png"], dir);
  ff(["-f", "lavfi", "-i", "color=black:s=108x192:r=30:d=1", "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "base.mp4"], dir);
  const plan = path.join(dir, "plan.json");
  fs.writeFileSync(plan, JSON.stringify({ speed: 1, fancyTexts: [{ text: "test", presetId: "test", start: 0.2, end: 0.8, scale: 0.25, x: 0, y: 0 }] }));
  const { command, work } = captureRender("render-mix.ts", ["fade", "--input", path.join(dir, "base.mp4"), "--plan", plan, "--output", path.join(dir, "out.mp4")], dir);
  const pixels = sample(command, work, 0.5);
  assert.ok(pixels[0] > 200 && pixels[1] < 20, `expected red fade midpoint, got ${[...pixels.subarray(0, 3)]}`);
});

type LegacyCase = { queries: string[]; manifest?: Record<string, string>; existing?: string; pollFails?: boolean; cli?: boolean };
function legacyCase(name: string, options: LegacyCase) {
  const dir = fixture(name), library = path.join(dir, "public/library"), scripts = path.join(dir, "public/scripts");
  fs.mkdirSync(library, { recursive: true }); fs.mkdirSync(scripts, { recursive: true });
  if (options.existing) ff(["-f", "lavfi", "-i", "color=blue:s=32x32:d=1", options.existing], library);
  fs.writeFileSync(path.join(library, "manifest.json"), JSON.stringify(options.manifest ?? {}));
  fs.writeFileSync(path.join(scripts, "case.pending.json"), JSON.stringify(options.queries.map(query => ({query, kind: "broll", at: 0, anchorText: query}))));
  let creates = 0;
  let exitResolve!: (code: number) => void;
  const exited = new Promise<number>(resolve => { exitResolve = resolve; });
  const context = script("generate-broll.ts", {}, {
    __dirname: path.join(dir, "scripts"),
    process: { env: { SEEDANCE_API_KEY: "offline", SEEDANCE_MODEL: "offline" }, pid: process.pid,
      argv: ["node", "generate-broll.ts", "case"], exit: exitResolve },
    runAsMain: options.cli,
    fetch: async (_url: string, request: any) => {
      if (request?.method === "POST") { creates++; return { ok: true, json: async () => ({ id: "paid-1" }) }; }
      if (options.pollFails) throw new Error("offline poll failure");
      return { ok: true, json: async () => ({status: "succeeded", content: {video_url: "https://offline.invalid/file"}}), arrayBuffer: async () => Buffer.from("offline-video") };
    },
  });
  return { run: () => context.runLegacyMode("case"), creates: () => creates, library, exited };
}

test("legacy valid query cache is reused without creating a paid task", async () => {
  const f = legacyCase("legacy-cache", {queries: ["existing"], manifest: {existing: "seed_existing.mp4"}, existing: "seed_existing.mp4"});
  await f.run();
  assert.equal(f.creates(), 0);
});
test("legacy unrelated existing file is rejected before a paid task", async () => {
  const f = legacyCase("legacy-conflict", {queries: ["a/b"], manifest: {"a b": "seed_a_b.mp4"}, existing: "seed_a_b.mp4"});
  await assert.rejects(f.run());
  assert.equal(f.creates(), 0);
});
test("legacy conflicting pending query slugs are rejected before any paid task", async () => {
  const f = legacyCase("legacy-pending-conflict", {queries: ["a/b", "a b"]});
  await assert.rejects(f.run());
  assert.equal(f.creates(), 0);
});
test("legacy poll failure returns failure and preserves provider task id", async () => {
  const f = legacyCase("legacy-failure", {queries: ["new"], pollFails: true});
  await assert.rejects(f.run());
  assert.equal(f.creates(), 1);
  const jobs = JSON.parse(fs.readFileSync(path.join(f.library, "seedance-jobs.json"), "utf8"));
  assert.equal(jobs.new.taskId, "paid-1");
  assert.equal(jobs.new.status, "failed");
  await assert.rejects(f.run());
  assert.equal(f.creates(), 1, "failed provider job must not silently incur a second charge");
});


test("legacy CLI exits nonzero when output preflight fails", async () => {
  const f = legacyCase("legacy-cli-conflict", { queries: ["a/b"], manifest: {"a b": "seed_a_b.mp4"}, existing: "seed_a_b.mp4", cli: true });
  assert.equal(await f.exited, 1);
  assert.equal(f.creates(), 0);
});
