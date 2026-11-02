import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import test from "node:test";
import ts from "typescript";

test("legacy generation reads manifest under its lock and preserves prior owner entries", async () => {
  const root = path.resolve(__dirname, "..");
  const dir = path.join(root, "runtime", "fixes-20260920", `legacy-lock-${process.pid}-${Date.now()}`);
  const library = path.join(dir, "public/library"), scripts = path.join(dir, "public/scripts");
  fs.mkdirSync(library, { recursive: true }); fs.mkdirSync(scripts, { recursive: true });
  execFileSync("ffmpeg", ["-v", "error", "-f", "lavfi", "-i", "color=blue:s=32x32:d=1", "-c:v", "libx264", path.join(dir, "sample.mp4")], { timeout: 30000 });
  const bytes = fs.readFileSync(path.join(dir, "sample.mp4"));
  fs.writeFileSync(path.join(library, "manifest.json"), "{}");
  fs.writeFileSync(path.join(scripts, "case.pending.json"), JSON.stringify([{ query: "new", at: 0, kind: "broll", anchorText: "new" }]));
  let interleaved = false;
  const customFs = { ...fs, openSync(file: fs.PathLike, flags: string | number, mode?: fs.Mode) {
    if (String(file).endsWith(".seedance-legacy.lock") && !interleaved) {
      interleaved = true;
      // A second owner finishes immediately before this caller acquires the lock.
      // All reads, writes, exclusive locking and persistence remain real.
      fs.writeFileSync(path.join(library, "seed_other.mp4"), bytes);
      fs.writeFileSync(path.join(library, "manifest.json"), JSON.stringify({ other: "seed_other.mp4" }));
      fs.writeFileSync(path.join(library, "seedance-jobs.json"), JSON.stringify({ other: { file: "seed_other.mp4", prompt: "other", status: "complete", taskId: "prior-task" } }));
    }
    return fs.openSync(file, flags, mode);
  } };
  const filename = path.join(root, "scripts/generate-broll.ts"), nativeRequire = createRequire(filename);
  const context: any = { exports: {}, module: {}, __dirname: path.join(dir, "scripts"), console, Buffer, setTimeout,
    process: { env: { SEEDANCE_API_KEY: "offline", SEEDANCE_MODEL: "offline" }, pid: process.pid },
    require: (id: string) => id === "./load-env" ? {} : id === "node:fs" ? customFs : nativeRequire(id),
    fetch: async (url: string, request?: { method?: string }) => {
      if (request?.method === "POST") return { ok: true, json: async () => ({ id: "new-task" }) };
      if (String(url).endsWith("/new-task")) return { ok: true, json: async () => ({ status: "succeeded", content: { video_url: "https://offline.invalid/file" } }) };
      return { ok: true, arrayBuffer: async () => bytes };
    } };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true, target: ts.ScriptTarget.ES2022 },
  }).outputText, context);
  await context.runLegacyMode("case");
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(library, "manifest.json"), "utf8")), {
    other: "seed_other.mp4", new: "seed_new.mp4",
  });
  assert.equal(JSON.parse(fs.readFileSync(path.join(library, "seedance-jobs.json"), "utf8")).other.taskId, "prior-task");
});
