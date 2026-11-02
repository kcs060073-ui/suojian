import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { formatRenderMixTaskPaths, parseRenderMixOptions, resolveRenderMixAsset, withRenderMixWorkdir } from "../scripts/render-mix-options";

const root = "E:/project";

test("keeps legacy name and speed paths compatible", () => {
  const options = parseRenderMixOptions(["task-a", "1.5"], root);

  assert.equal(options.name, "task-a");
  assert.equal(options.speed, 1.5);
  assert.equal(options.inputVideo, path.resolve(root, "public/digital_human/task-a.mp4"));
  assert.equal(options.planFile, path.resolve(root, "public/scripts/task-a.plan.json"));
  assert.equal(options.assFile, path.resolve(root, "public/scripts/task-a.ass"));
  assert.equal(options.outputVideo, path.resolve(root, "out/task-a.mp4"));
});

test("explicit task paths override legacy name paths", () => {
  const options = parseRenderMixOptions([
    "task-a", "1.25", "--input", "D:/input.mp4", "--plan", "D:/plan.json",
    "--ass", "D:/sub.ass", "--output", "D:/final.mp4", "--asset-root", "D:/assets",
  ], root);

  assert.equal(options.inputVideo, path.resolve("D:/input.mp4"));
  assert.equal(options.planFile, path.resolve("D:/plan.json"));
  assert.equal(options.assFile, path.resolve("D:/sub.ass"));
  assert.equal(options.outputVideo, path.resolve("D:/final.mp4"));
  assert.equal(options.assetRoot, path.resolve("D:/assets"));
});

test("accepts flags in different orders", () => {
  const options = parseRenderMixOptions([
    "--output", "D:/final.mp4", "task-a", "--dry-run", "--input", "D:/input.mp4", "1.75",
  ], root);

  assert.equal(options.name, "task-a");
  assert.equal(options.speed, 1.75);
  assert.equal(options.inputVideo, path.resolve("D:/input.mp4"));
  assert.equal(options.outputVideo, path.resolve("D:/final.mp4"));
  assert.equal(options.dryRun, true);
});

test("preview output flags are bounded", () => {
  const options = parseRenderMixOptions([
    "task-1", "--plan", "plan.json", "--output", "preview.mp4",
    "--max-duration", "10", "--width", "540", "--height", "960",
    "--crf", "28",
  ], root);
  assert.deepEqual(options.outputProfile, {
    maxDuration: 10,
    width: 540,
    height: 960,
    crf: 28,
  });

  for (const argv of [
    ["task-1", "--max-duration", "13"],
    ["task-1", "--width", "541"],
    ["task-1", "--height", "2000"],
    ["task-1", "--crf", "33"],
  ]) {
    assert.throws(() => parseRenderMixOptions(argv, root), /输出|CRF|时长|尺寸/);
  }
});

test("formats dry-run task paths from explicit values", () => {
  const options = parseRenderMixOptions([
    "task-a", "--input", "D:/input.mp4", "--plan", "D:/plan.json", "--output", "D:/output.mp4",
  ], root);

  assert.equal(formatRenderMixTaskPaths(options), [
    `input=${path.resolve("D:/input.mp4")}`,
    `plan=${path.resolve("D:/plan.json")}`,
    `output=${path.resolve("D:/output.mp4")}`,
  ].join("\n"));
});

test("reports a stable error when a path flag has no value", () => {
  assert.throws(
    () => parseRenderMixOptions(["task-a", "--input"], root),
    new Error("缺少 --input 的路径值"),
  );
});

test("resolves absolute assets before asset root and legacy locations", () => {
  const options = parseRenderMixOptions(["task-a", "--asset-root", "D:/assets"], root);

  assert.deepEqual(options.assetSearchRoots, [
    path.resolve("D:/assets"),
    path.resolve(root, "public/inserts"),
    path.resolve(root, "public"),
  ]);
});

test("keeps legacy inserts before public when asset root is omitted", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "render-mix-legacy-assets-"));
  try {
    const insertsRoot = path.join(temp, "public", "inserts");
    const publicRoot = path.join(temp, "public");
    fs.mkdirSync(insertsRoot, { recursive: true });
    fs.writeFileSync(path.join(insertsRoot, "duplicate.mp4"), "inserts");
    fs.writeFileSync(path.join(publicRoot, "duplicate.mp4"), "public");
    const options = parseRenderMixOptions(["task-a"], temp);

    assert.deepEqual(options.assetSearchRoots, [
      path.resolve(temp, "public/inserts"),
      path.resolve(temp, "public"),
    ]);
    assert.equal(resolveRenderMixAsset("duplicate.mp4", options.assetSearchRoots), path.join(insertsRoot, "duplicate.mp4"));
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});

test("rejects unknown flags instead of silently falling back", () => {
  assert.throws(
    () => parseRenderMixOptions(["task-a", "--ouptut", "D:/final.mp4"], root),
    new Error("未知参数: --ouptut"),
  );
});

test("rejects extra positional arguments while accepting sticker passthrough flags", () => {
  assert.throws(
    () => parseRenderMixOptions(["task-a", "1.25", "unexpected"], root),
    new Error("位置参数过多: unexpected"),
  );

  const options = parseRenderMixOptions([
    "task-a", "--stickers", "[]", "--fancy-texts", "[]",
  ], root);
  assert.equal(options.stickersJson, "[]");
  assert.equal(options.fancyTextsJson, "[]");
});

test("cleans a render workdir when preparation throws", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "render-mix-workdir-"));
  try {
    assert.throws(
      () => withRenderMixWorkdir(tempRoot, (workdir) => {
        fs.writeFileSync(path.join(workdir, "partial.ass"), "partial");
        throw new Error("preparation failed");
      }),
      new Error("preparation failed"),
    );
    assert.deepEqual(fs.readdirSync(tempRoot), []);
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
});

test("resolves existing absolute assets before asset root and legacy locations", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "render-mix-options-"));
  try {
    const absolute = path.join(temp, "absolute.mp4");
    const assetRoot = path.join(temp, "asset-root");
    const insertsRoot = path.join(temp, "inserts");
    const publicRoot = path.join(temp, "public");
    fs.mkdirSync(assetRoot);
    fs.mkdirSync(insertsRoot);
    fs.mkdirSync(publicRoot);
    fs.writeFileSync(absolute, "absolute");
    fs.writeFileSync(path.join(assetRoot, "clip.mp4"), "asset-root");
    fs.writeFileSync(path.join(insertsRoot, "clip.mp4"), "inserts");
    fs.writeFileSync(path.join(publicRoot, "clip.mp4"), "public");

    assert.equal(resolveRenderMixAsset(absolute, [assetRoot, insertsRoot, publicRoot]), absolute);
    assert.equal(resolveRenderMixAsset("clip.mp4", [assetRoot, insertsRoot, publicRoot]), path.join(assetRoot, "clip.mp4"));
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});
