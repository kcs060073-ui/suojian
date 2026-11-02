/**
 * P3 编排器：把 brief.json 的 B-roll 建议解析成可渲染的 plan.json。
 *
 * 对每条 brief.broll：
 *   1. 用词级时间戳把 anchorText 定位到最终成片时间轴（src/timeline.ts，与字幕同源）
 *   2. 用 query 在本地素材库匹配文件（混合源：本地库优先）
 *   3. 命中 → 写成 plan.inserts；未命中 → 列入「待补」清单（交给 fetch-broll.ts 自动检索或人工补）
 * 标题卡（brief.title）作为文字标题卡直接并入（无需素材）。
 *
 * 本地素材库：public/library/ 下的图片/视频；匹配依据 public/library/manifest.json
 *   { "数据中心 机房 服务器": "datacenter.mp4", "货运卡车 物流": "trucks.png", ... }
 *   无 manifest 时回退用文件名（去扩展名）当标签。匹配=query 与标签按字/词重合度打分取最高。
 *
 * 用法: ts-node scripts/compile-plan.ts <name> [speed=1.25]
 *   读 public/scripts/<name>.{brief.json,script.txt,words.json} → 写 public/scripts/<name>.plan.json
 */
import * as fs from "fs";
import * as path from "path";
import { buildAnchorTimer, Word } from "../src/timeline";
import type { MixPlan, MixInsert, InsertPosition } from "../src/types";

const ROOT = path.resolve(__dirname, "..");
const SCRIPTS = path.join(ROOT, "public", "scripts");
const LIBRARY = path.join(ROOT, "public", "library");
const INSERTS = path.join(ROOT, "public", "inserts");

// 画中画默认位置：上 1/3 区，避开数字人脸
const PIP_POS: InsertPosition = { x: 0.1, y: 0.08, width: 0.8, height: 0.34 };

interface BrollItem {
  anchorText: string;
  query: string;
  prompt?: string; // 给视频生成模型的电影级描述（plan-brief 产出；老 brief 可能没有）
  kind: "broll" | "image";
  durationSec: number;
}
interface Brief {
  keywords: string[];
  punchLines: string[];
  title: { text: string; durationSec: number };
  broll: BrollItem[];
}

// 把一段中文/词组拆成可比对的 token（按空格切，再按字补充），用于库标签与 query 的重合度
function tokens(s: string): Set<string> {
  const out = new Set<string>();
  for (const part of s.split(/[\s,，、/]+/).filter(Boolean)) {
    out.add(part);
    for (const ch of part) if (ch.trim()) out.add(ch);
  }
  return out;
}

// 加载本地素材库标签 → 文件名
function loadLibrary(): { tag: string; file: string }[] {
  const entries: { tag: string; file: string }[] = [];
  const manifestPath = path.join(LIBRARY, "manifest.json");
  if (fs.existsSync(manifestPath)) {
    try {
      const m = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as Record<string, string>;
      for (const [tag, file] of Object.entries(m)) entries.push({ tag, file });
    } catch (e) {
      console.warn(`⚠ manifest.json 解析失败: ${(e as Error).message}`);
    }
  }
  if (fs.existsSync(LIBRARY)) {
    for (const f of fs.readdirSync(LIBRARY)) {
      if (f === "manifest.json") continue;
      if (!/\.(png|jpg|jpeg|webp|mp4|mov|mkv|webm|m4v)$/i.test(f)) continue;
      if (entries.some((e) => e.file === f)) continue; // manifest 已覆盖
      entries.push({ tag: path.basename(f, path.extname(f)), file: f });
    }
  }
  return entries;
}

// 在库里给 query 找最匹配的素材（token 重合度），无重合返回 null
function matchMaterial(query: string, lib: { tag: string; file: string }[]): string | null {
  const q = tokens(query);
  let best: { file: string; score: number } | null = null;
  for (const e of lib) {
    const t = tokens(e.tag);
    let score = 0;
    for (const tok of q) if (t.has(tok)) score += tok.length >= 2 ? 2 : 1; // 词比单字权重高
    if (score > 0 && (!best || score > best.score)) best = { file: e.file, score };
  }
  return best ? best.file : null;
}

// 素材文件最终放在 public/inserts/（render-mix 从那里取）。库文件按需软链/复制过去。
function ensureInsert(file: string): string {
  const dst = path.join(INSERTS, file);
  if (fs.existsSync(dst)) return file;
  const srcLib = path.join(LIBRARY, file);
  if (fs.existsSync(srcLib)) {
    fs.copyFileSync(srcLib, dst);
    return file;
  }
  return file; // 既不在 inserts 也不在 library：交由调用方判断（多半是待补）
}

function main() {
  const name = process.argv[2];
  const speed = parseFloat(process.argv[3] || "1.25");
  if (!name) {
    console.error("用法: ts-node scripts/compile-plan.ts <name> [speed]");
    process.exit(1);
  }
  const briefPath = path.join(SCRIPTS, `${name}.brief.json`);
  if (!fs.existsSync(briefPath)) {
    console.error(`缺 brief: ${briefPath}（先跑 plan-brief.ts 生成）`);
    process.exit(1);
  }
  const brief: Brief = JSON.parse(fs.readFileSync(briefPath, "utf8"));
  const scriptText = fs.readFileSync(path.join(SCRIPTS, `${name}.script.txt`), "utf8").replace(/\r/g, "");
  const words: Word[] = JSON.parse(fs.readFileSync(path.join(SCRIPTS, `${name}.words.json`), "utf8"));
  const timer = buildAnchorTimer(scriptText, words, speed);
  const lib = loadLibrary();
  if (!fs.existsSync(INSERTS)) fs.mkdirSync(INSERTS, { recursive: true });

  const inserts: MixInsert[] = [];
  const pending: { query: string; prompt?: string; anchorText: string; kind: string; at: number }[] = [];

  for (const b of brief.broll) {
    const t = timer.timeOf(b.anchorText);
    if (!t) {
      console.warn(`  ⚠ 锚点对不上文案，跳过: "${b.anchorText}"`);
      continue;
    }
    const start = t.start;
    const end = start + (b.durationSec || 3);
    const file = matchMaterial(b.query, lib);
    if (!file) {
      pending.push({ query: b.query, prompt: b.prompt, anchorText: b.anchorText, kind: b.kind, at: +start.toFixed(1) });
      continue;
    }
    ensureInsert(file);
    if (b.kind === "broll") {
      inserts.push({ type: "broll", src: file, start: +start.toFixed(2), end: +end.toFixed(2) });
    } else {
      inserts.push({ type: "image", src: file, start: +start.toFixed(2), end: +end.toFixed(2), position: PIP_POS });
    }
  }

  const plan: MixPlan = {
    speed,
    titles: brief.title?.text ? [{ text: brief.title.text, start: 0, end: brief.title.durationSec || 3 }] : [],
    inserts,
  };

  // 品牌全局配置（设一次全片通用）：public/brand.json { watermark?, bgm? } → 注入 plan
  const brandPath = path.join(ROOT, "public", "brand.json");
  if (fs.existsSync(brandPath)) {
    try {
      const brand = JSON.parse(fs.readFileSync(brandPath, "utf8"));
      if (brand.watermark) plan.watermark = brand.watermark;
      if (brand.bgm) plan.bgm = brand.bgm;
    } catch (e) {
      console.warn(`⚠ brand.json 解析失败: ${(e as Error).message}`);
    }
  }

  fs.writeFileSync(path.join(SCRIPTS, `${name}.plan.json`), JSON.stringify(plan, null, 2), "utf8");

  // 待补清单 → 给 fetch-broll.ts（自动检索）或人工补素材
  const pendingPath = path.join(SCRIPTS, `${name}.pending.json`);
  if (pending.length) {
    fs.writeFileSync(pendingPath, JSON.stringify(pending, null, 2), "utf8");
  } else if (fs.existsSync(pendingPath)) {
    fs.rmSync(pendingPath);
  }

  console.log(`✔ plan: 标题卡 ${plan.titles!.length} | 已配 B-roll ${inserts.length} | 待补 ${pending.length}`);
  if (pending.length) {
    console.log(`  待补素材（写入 ${name}.pending.json，跑 fetch-broll.ts 自动检索或人工放 public/library/）:`);
    for (const p of pending) console.log(`    [${p.at}s] ${p.kind}  «${p.query}»  ← ${p.anchorText}`);
  }
}

main();
