/**
 * Generate replace-only ASS subtitles from a locked script and Whisper word timings.
 * Legacy: ts-node scripts/make-ass.ts <name> [speed]
 * Explicit: --script --words --semantics --output --metadata --preset --speed --lead --duration
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { buildAssDocument } from "../src/subtitle-ass";
import { TimedWord } from "../src/subtitle-alignment";

const ROOT = path.resolve(__dirname, "..");
const SCRIPTS = path.join(ROOT, "public", "scripts");
const EXPLICIT_CONTRACT_FLAGS = new Set([
  "--script", "--words", "--semantics", "--output", "--metadata", "--duration",
]);

interface MakeAssOptions {
  script: string;
  words: string;
  output: string;
  metadata: string;
  semantics?: string;
  legacyBrief?: string;
  preset: string;
  speed: number;
  lead: number;
  duration?: number;
  highlightEnabled: boolean;
}

function valueAfter(args: string[], index: number, flag: string): string {
  const value = args[index + 1];
  if (!value || value.startsWith("--")) throw new Error(`缺少 ${flag} 的值`);
  return value;
}

function numberValue(value: string, flag: string): number {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(`${flag} 必须是有限数字`);
  return number;
}

export function parseMakeAssOptions(args: string[], root = ROOT): MakeAssOptions {
  let name: string | undefined;
  let speed: number | undefined;
  let script: string | undefined;
  let words: string | undefined;
  let output: string | undefined;
  let metadata: string | undefined;
  let semantics: string | undefined;
  let preset: string | undefined;
  let lead = 0.08;
  let duration: number | undefined;
  let highlightEnabled = true;

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (!arg.startsWith("--")) {
      if (!name) name = arg;
      else if (speed === undefined) speed = numberValue(arg, "speed");
      else throw new Error(`位置参数过多: ${arg}`);
      continue;
    }
    if (arg === "--script") { script = path.resolve(valueAfter(args, index, arg)); index += 1; continue; }
    if (arg === "--words") { words = path.resolve(valueAfter(args, index, arg)); index += 1; continue; }
    if (arg === "--output") { output = path.resolve(valueAfter(args, index, arg)); index += 1; continue; }
    if (arg === "--metadata") { metadata = path.resolve(valueAfter(args, index, arg)); index += 1; continue; }
    if (arg === "--semantics") { semantics = path.resolve(valueAfter(args, index, arg)); index += 1; continue; }
    if (arg === "--preset") { preset = valueAfter(args, index, arg); index += 1; continue; }
    if (arg === "--speed") { speed = numberValue(valueAfter(args, index, arg), arg); index += 1; continue; }
    if (arg === "--lead") { lead = numberValue(valueAfter(args, index, arg), arg); index += 1; continue; }
    if (arg === "--duration") { duration = numberValue(valueAfter(args, index, arg), arg); index += 1; continue; }
    if (arg === "--highlight") {
      const value = valueAfter(args, index, arg);
      if (value !== "0" && value !== "1") throw new Error("--highlight 必须是 0 或 1");
      highlightEnabled = value === "1";
      index += 1;
      continue;
    }
    if (arg === "--mode") {
      const mode = valueAfter(args, index, arg);
      if (mode !== "replace") throw new Error("现代字幕仅支持 --mode replace");
      index += 1;
      continue;
    }
    throw new Error(`未知参数: ${arg}`);
  }

  if (name && args.some((arg) => EXPLICIT_CONTRACT_FLAGS.has(arg))) {
    throw new Error("位置参数 legacy 调用不能与显式参数混用");
  }
  if (!name && (!script || !words)) throw new Error("用法: make-ass <name> [speed] 或 --script <path> --words <path>");
  if (!name && !semantics) throw new Error("显式 make-ass 必须提供 --semantics <path>");
  const base = name ?? path.basename(script!, path.extname(script!));
  const defaults = path.join(root, "public", "scripts");
  return {
    script: script ?? path.join(defaults, `${base}.script.txt`),
    words: words ?? path.join(defaults, `${base}.words.json`),
    output: output ?? path.join(defaults, `${base}.ass`),
    metadata: metadata ?? path.join(defaults, `${base}.subtitle.json`),
    semantics,
    legacyBrief: name ? path.join(defaults, `${base}.brief.json`) : undefined,
    preset: preset ?? (name ? "sub-legacy-heavy-v1" : "sub-modern-clean-v1"),
    speed: speed ?? 1.25,
    lead,
    duration,
    highlightEnabled,
  };
}

interface SubtitleSemantics {
  keywords: string[];
  punchLines: string[];
}

function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string" && item.trim().length > 0);
}

function readExplicitSemantics(semanticsPath: string): SubtitleSemantics {
  let value: unknown;
  try {
    value = JSON.parse(fs.readFileSync(semanticsPath, "utf8"));
  } catch (error) {
    throw new Error(`semantics JSON 无效: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (
    typeof value !== "object" || value === null || Array.isArray(value)
    || new Set(Object.keys(value)).size !== 2
    || !Object.prototype.hasOwnProperty.call(value, "keywords")
    || !Object.prototype.hasOwnProperty.call(value, "punchLines")
  ) {
    throw new Error("semantics 必须是且只能包含 keywords 与 punchLines 的 JSON object");
  }
  const candidate = value as Record<string, unknown>;
  if (!stringArray(candidate.keywords) || !stringArray(candidate.punchLines)) {
    throw new Error("semantics 的 keywords 与 punchLines 必须是非空字符串数组");
  }
  return { keywords: candidate.keywords, punchLines: candidate.punchLines };
}

function readLegacyBrief(briefPath: string | undefined): SubtitleSemantics {
  if (!briefPath || !fs.existsSync(briefPath)) return { keywords: [], punchLines: [] };
  try {
    const value = JSON.parse(fs.readFileSync(briefPath, "utf8")) as Record<string, unknown>;
    return {
      keywords: stringArray(value.keywords) ? value.keywords : [],
      punchLines: stringArray(value.punchLines) ? value.punchLines : [],
    };
  } catch (error) {
    console.warn(`brief.json 无法读取，语义按空集合处理: ${error instanceof Error ? error.message : String(error)}`);
    return { keywords: [], punchLines: [] };
  }
}

function main(): void {
  const options = parseMakeAssOptions(process.argv.slice(2));
  const scriptText = fs.readFileSync(options.script, "utf8").replace(/\r/g, "");
  const words = JSON.parse(fs.readFileSync(options.words, "utf8")) as TimedWord[];
  const semantics = options.semantics
    ? readExplicitSemantics(options.semantics)
    : readLegacyBrief(options.legacyBrief);
  const result = buildAssDocument({
    scriptText,
    words,
    preset: options.preset,
    speed: options.speed,
    lead: options.lead,
    duration: options.duration,
    keywords: semantics.keywords,
    punchLines: semantics.punchLines,
    highlightEnabled: options.highlightEnabled,
  });
  fs.mkdirSync(path.dirname(options.output), { recursive: true });
  fs.mkdirSync(path.dirname(options.metadata), { recursive: true });
  fs.writeFileSync(options.output, result.ass, "utf8");
  fs.writeFileSync(options.metadata, `${JSON.stringify(result.metadata, null, 2)}\n`, "utf8");
  console.log(`完成：${result.metadata.cues.length} 条替换字幕 → ${options.output}`);
  console.log(`metadata=${options.metadata} coverage=${result.metadata.coverage.toFixed(3)} speed=${result.metadata.speed}`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
