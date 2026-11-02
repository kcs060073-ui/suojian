/**
 * Compatibility launcher only.
 *
 * The old in-memory Node service was intentionally retired. Persistent task
 * state, security checks and the single worker now live in workbench.server.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const bundledPython = path.join(root, ".venv", "Scripts", "python.exe");
const python = fs.existsSync(bundledPython) ? bundledPython : "python";
const forwarded = process.argv.slice(2);
const child = spawn(
  python,
  ["-m", "workbench.server", ...forwarded],
  { cwd: root, env: process.env, stdio: "inherit" },
);

child.on("error", (error) => {
  console.error("无法启动视频制作工作台：", error.message);
  process.exitCode = 1;
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exitCode = code ?? 1;
});
