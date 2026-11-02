import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

import { buildSfxFilters } from "../src/mix-sfx";

const ROOT = path.resolve(__dirname, "..");

function dryRun(plan: object) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "render-mix-packaging-"));
  const input = path.join(root, "digital.mp4");
  const sfx = path.join(root, "hit.wav");
  const planPath = path.join(root, "plan.json");
  fs.writeFileSync(input, "not-a-real-video");
  fs.writeFileSync(sfx, "not-a-real-wave");
  fs.writeFileSync(planPath, JSON.stringify(plan));
  const result = spawnSync(
    process.execPath,
    [
      "-r", "ts-node/register", "scripts/render-mix.ts", "packaging",
      "--input", input, "--plan", planPath,
      "--output", path.join(root, "out.mp4"), "--asset-root", root,
      "--dry-run",
    ],
    { cwd: ROOT, encoding: "utf8" },
  );
  fs.rmSync(root, { recursive: true, force: true });
  return result;
}


test("buildSfxFilters delays and mixes one bounded effect", () => {
  const result = buildSfxFilters(
    [{ src: "hit.wav", start: 2.5, gain: 0.35 }],
    3,
  );

  assert.deepEqual(result.inputs, ["-i", "hit.wav"]);
  assert.match(result.filters.join(";"), /adelay=2500\|2500/);
  assert.match(result.filters.join(";"), /volume=0.35/);
  assert.equal(result.labels[0], "[sfx0]");
});

test("buildSfxFilters rejects unsafe timing and gain values", () => {
  assert.throws(
    () => buildSfxFilters([{ src: "hit.wav", start: -1 }], 1),
    /MixSfx start must be a finite non-negative number/,
  );
  assert.throws(
    () => buildSfxFilters([{ src: "hit.wav", start: 0, gain: 1.5 }], 1),
    /MixSfx gain must be between 0 and 1/,
  );
  assert.throws(
    () => buildSfxFilters([{ src: "", start: 0 }], 1),
    /MixSfx src is required/,
  );
});

test("render-mix uses the selected title preset and delayed SFX", () => {
  const result = dryRun({
    titles: [{
      text: "核心数据",
      start: 0,
      end: 2.5,
      presetId: "card-data-emphasis",
    }],
    sfx: [{ src: "hit.wav", start: 1.25, gain: 0.4 }],
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /adelay=1250\|1250/);
  assert.match(result.stdout, /音效1/);
});

test("render-mix rejects an unknown selected title preset", () => {
  const result = dryRun({
    titles: [{ text: "标题", start: 0, end: 1, presetId: "missing-card" }],
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /PACKAGING_TITLE_PRESET_UNKNOWN/);
});

test("render-mix applies the bounded preview canvas, CRF and duration", () => {
  const result = dryRun({
    output: { maxDuration: 10, width: 540, height: 960, crf: 28 },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /scale=540:960/);
  assert.match(result.stdout, /-crf 28/);
  assert.match(result.stdout, /-t 10/);
});
