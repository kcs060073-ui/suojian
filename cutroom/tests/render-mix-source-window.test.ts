import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const ROOT = path.resolve(__dirname, "..");

function wavBytes(seconds: number): Buffer {
  const sampleRate = 8000;
  const dataSize = sampleRate * seconds * 2;
  const buffer = Buffer.alloc(44 + dataSize);
  buffer.write("RIFF", 0);
  buffer.writeUInt32LE(36 + dataSize, 4);
  buffer.write("WAVEfmt ", 8);
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(sampleRate, 24);
  buffer.writeUInt32LE(sampleRate * 2, 28);
  buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36);
  buffer.writeUInt32LE(dataSize, 40);
  return buffer;
}

function runDryPlan(plan: object) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "render-mix-source-window-"));
  const input = path.join(root, "digital.mp4");
  const video = path.join(root, "visual.mp4");
  const audio = path.join(root, "visual.wav");
  const planPath = path.join(root, "plan.json");
  fs.writeFileSync(input, "not-a-real-video");
  fs.writeFileSync(video, "not-a-real-video");
  fs.writeFileSync(audio, wavBytes(3));
  fs.writeFileSync(planPath, JSON.stringify(plan));
  const result = spawnSync(
    process.execPath,
    [
      "-r", "ts-node/register", "scripts/render-mix.ts", "source-window",
      "--input", input, "--plan", planPath,
      "--output", path.join(root, "out.mp4"), "--asset-root", root, "--dry-run",
    ],
    { cwd: ROOT, encoding: "utf8" },
  );
  fs.rmSync(root, { recursive: true, force: true });
  return result;
}

test("render-mix trims video and retained insert audio from explicit source windows", () => {
  const result = runDryPlan({
    inserts: [
      {
        type: "broll", src: "visual.mp4",
        start: 6, end: 9, sourceStart: 30, sourceEnd: 33, audio: "mute",
      },
      {
        type: "video", src: "visual.wav",
        start: 10, end: 12.5, sourceStart: 4, sourceEnd: 8, audio: "keep",
        position: { x: 0.1, y: 0.1, width: 0.8, height: 0.3 },
      },
    ],
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /trim=start=30\.000:end=33\.000/);
  assert.match(result.stdout, /trim=start=4\.000:end=8\.000/);
  assert.match(result.stdout, /atrim=start=4\.000:end=8\.000,atrim=duration=2\.500/);
});

test("render-mix rejects a reversed source window", () => {
  const result = runDryPlan({
    inserts: [{
      type: "broll", src: "visual.mp4",
      start: 0, end: 3, sourceStart: 4, sourceEnd: 3, audio: "mute",
    }],
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /source.*window|源时间窗|sourceStart/i);
});
