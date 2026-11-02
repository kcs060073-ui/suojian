import * as assert from "node:assert/strict";
import { test } from "node:test";
import * as path from "node:path";

import { parsePlanBriefOptions } from "../scripts/plan-brief";


test("explicit workbench paths do not fall back to shared public script outputs", () => {
  const root = path.resolve("E:/task-root");
  const options = parsePlanBriefOptions([
    "--script", "E:/task-root/inputs/final-script.txt",
    "--brief-output", "E:/task-root/artifacts/brief.json",
    "--plan-output", "E:/task-root/artifacts/plan.json",
    "--speed", "1.25",
    "--profile", "zhaoshang",
  ], root);
  assert.equal(options.name, null);
  assert.equal(options.scriptPath, path.resolve("E:/task-root/inputs/final-script.txt"));
  assert.equal(options.briefPath, path.resolve("E:/task-root/artifacts/brief.json"));
  assert.equal(options.planPath, path.resolve("E:/task-root/artifacts/plan.json"));
  assert.equal(options.speed, 1.25);
  assert.equal(options.profile, "zhaoshang");
});

test("legacy name speed and profile remain compatible", () => {
  const root = path.resolve("E:/project");
  const options = parsePlanBriefOptions(["xinjiang", "1.3", "caishui"], root);
  assert.equal(options.name, "xinjiang");
  assert.equal(options.scriptPath, path.join(root, "public", "scripts", "xinjiang.script.txt"));
  assert.equal(options.briefPath, path.join(root, "public", "scripts", "xinjiang.brief.json"));
  assert.equal(options.planPath, path.join(root, "public", "scripts", "xinjiang.plan.json"));
  assert.equal(options.speed, 1.3);
  assert.equal(options.profile, "caishui");
});

test("partial or mixed explicit invocation fails closed", () => {
  const root = path.resolve("E:/project");
  assert.throws(
    () => parsePlanBriefOptions(["--script", "a.txt", "--brief-output", "b.json"], root),
    /显式模式必须同时提供/,
  );
  assert.throws(
    () => parsePlanBriefOptions(["xinjiang", "--script", "a.txt", "--brief-output", "b.json", "--plan-output", "c.json"], root),
    /不能混用/,
  );
  assert.throws(
    () => parsePlanBriefOptions(["--script", "a.txt", "--brief-output", "b.json", "--plan-output", "c.json", "stray"], root),
    /意外参数/,
  );
});
