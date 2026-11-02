import * as fs from "fs";
import * as path from "path";
import type { MixOutputProfile } from "../src/types";

export type RenderMixOptions = {
  name: string;
  speed?: number;
  inputVideo: string;
  planFile: string;
  assFile: string;
  outputVideo: string;
  assetRoot: string;
  assetSearchRoots: string[];
  dryRun: boolean;
  stickersJson?: string;
  fancyTextsJson?: string;
  outputProfile: MixOutputProfile;
};

const pathFlags = new Set(["--input", "--plan", "--ass", "--output", "--asset-root"]);
const numericFlags = new Set(["--max-duration", "--width", "--height", "--crf"]);
const valueFlags = new Set([
  ...pathFlags,
  "--stickers",
  "--fancy-texts",
  ...numericFlags,
]);

function optionalNumber(values: Map<string, string>, flag: string): number | undefined {
  const raw = values.get(flag);
  if (raw === undefined) return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`输出参数 ${flag} 必须是数字`);
  return value;
}

function resolveTaskPath(root: string, value: string | undefined, fallback: string): string {
  return value ? path.resolve(root, value) : path.resolve(fallback);
}

export function resolveRenderMixAsset(src: string, assetSearchRoots: string[]): string {
  if (path.isAbsolute(src) && fs.existsSync(src)) return src;
  for (const root of assetSearchRoots) {
    const candidate = path.join(root, src);
    if (fs.existsSync(candidate)) return candidate;
  }
  return path.join(assetSearchRoots[0], src);
}

export function formatRenderMixTaskPaths(options: RenderMixOptions): string {
  return [
    `input=${options.inputVideo}`,
    `plan=${options.planFile}`,
    `output=${options.outputVideo}`,
  ].join("\n");
}

export function withRenderMixWorkdir<T>(tempRoot: string, run: (workdir: string) => T): T {
  const workdir = fs.mkdtempSync(path.join(tempRoot, "dhmix-"));
  try {
    return run(workdir);
  } finally {
    fs.rmSync(workdir, { recursive: true, force: true });
  }
}

export function parseRenderMixOptions(argv: string[], root: string): RenderMixOptions {
  const values = new Map<string, string>();
  const positional: string[] = [];
  let dryRun = false;

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--dry-run") {
      dryRun = true;
      continue;
    }
    if (valueFlags.has(arg)) {
      const value = argv[i + 1];
      if (!value || value.startsWith("--")) throw new Error(`缺少 ${arg} 的路径值`);
      values.set(arg, value);
      i++;
      continue;
    }
    if (arg.startsWith("--")) throw new Error(`未知参数: ${arg}`);
    positional.push(arg);
  }

  const name = positional[0];
  if (!name) throw new Error("缺少任务名称");
  if (positional.length > 2) throw new Error(`位置参数过多: ${positional.slice(2).join(" ")}`);
  const speed = positional[1] ? parseFloat(positional[1]) : undefined;
  const resolvedRoot = path.resolve(root);
  const hasExplicitAssetRoot = values.has("--asset-root");
  const assetRoot = resolveTaskPath(resolvedRoot, values.get("--asset-root"), path.join(resolvedRoot, "public"));
  const legacySearchRoots = [path.join(resolvedRoot, "public", "inserts"), path.join(resolvedRoot, "public")];
  const maxDuration = optionalNumber(values, "--max-duration");
  const width = optionalNumber(values, "--width");
  const height = optionalNumber(values, "--height");
  const crf = optionalNumber(values, "--crf");
  if (maxDuration !== undefined && (maxDuration <= 0 || maxDuration > 12)) {
    throw new Error("输出时长必须大于 0 且不超过 12 秒");
  }
  for (const [label, value, limit] of [
    ["宽度", width, 1080],
    ["高度", height, 1920],
  ] as const) {
    if (value !== undefined && (!Number.isInteger(value) || value <= 0 || value > limit || value % 2 !== 0)) {
      throw new Error(`输出${label}必须是正偶数且不超过 ${limit}`);
    }
  }
  if (crf !== undefined && (!Number.isInteger(crf) || crf < 18 || crf > 32)) {
    throw new Error("输出 CRF 必须是 18—32 的整数");
  }

  return {
    name,
    speed: speed && Number.isFinite(speed) ? speed : undefined,
    inputVideo: resolveTaskPath(resolvedRoot, values.get("--input"), path.join(resolvedRoot, "public", "digital_human", `${name}.mp4`)),
    planFile: resolveTaskPath(resolvedRoot, values.get("--plan"), path.join(resolvedRoot, "public", "scripts", `${name}.plan.json`)),
    assFile: resolveTaskPath(resolvedRoot, values.get("--ass"), path.join(resolvedRoot, "public", "scripts", `${name}.ass`)),
    outputVideo: resolveTaskPath(resolvedRoot, values.get("--output"), path.join(resolvedRoot, "out", `${name}.mp4`)),
    assetRoot,
    assetSearchRoots: hasExplicitAssetRoot ? [assetRoot, ...legacySearchRoots] : legacySearchRoots,
    dryRun,
    stickersJson: values.get("--stickers"),
    fancyTextsJson: values.get("--fancy-texts"),
    outputProfile: {
      ...(maxDuration === undefined ? {} : { maxDuration }),
      ...(width === undefined ? {} : { width }),
      ...(height === undefined ? {} : { height }),
      ...(crf === undefined ? {} : { crf }),
    },
  };
}
