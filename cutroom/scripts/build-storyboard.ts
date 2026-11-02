import * as fs from "fs";
import * as path from "path";
import { execFileSync } from "child_process";
import { parseSrt } from "./srt-to-subtitles";
import {
  DEFAULT_INSERT_POSITION,
  type InsertItem,
  type Storyboard,
} from "../src/types";

const ROOT = path.resolve(__dirname, "..");
const DH_DIR = path.join(ROOT, "public", "digital_human");
const SCRIPTS_DIR = path.join(ROOT, "public", "scripts");
const INSERTS_DIR = path.join(ROOT, "public", "inserts");
const KEYWORDS_FILE = path.join(ROOT, "keywords.json");
const DEFAULT_FPS = 30;

// keywords.json 里每个关键词的配置
interface KeywordConfig {
  src: string;
  type: "image" | "video";
  duration: number;
  position?: InsertItem["position"];
}
type KeywordMap = Record<string, KeywordConfig>;

// 用 ffprobe 读取视频时长（秒）；失败则回退到字幕最后结束时间。
function probeDuration(file: string, fallback: number): number {
  try {
    const out = execFileSync(
      "ffprobe",
      [
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        file,
      ],
      { encoding: "utf8" }
    );
    const d = parseFloat(out.trim());
    return Number.isFinite(d) && d > 0 ? d : fallback;
  } catch {
    return fallback;
  }
}

// 找到与 srt 同名的数字人视频（任意常见扩展名）
function findDigitalHuman(base: string): string | null {
  for (const ext of [".mp4", ".mov", ".webm", ".mkv"]) {
    if (fs.existsSync(path.join(DH_DIR, base + ext))) return base + ext;
  }
  return null;
}

function loadKeywords(): KeywordMap {
  if (!fs.existsSync(KEYWORDS_FILE)) return {};
  return JSON.parse(fs.readFileSync(KEYWORDS_FILE, "utf8")) as KeywordMap;
}

// 关键词自动匹配：扫描字幕段，命中关键词则在该段 start 插入素材；每个关键词每条视频只插一次。
function buildStoryboard(srtPath: string, keywords: KeywordMap): Storyboard | null {
  const base = path.basename(srtPath, ".srt");
  const dhFile = findDigitalHuman(base);
  if (!dhFile) {
    console.warn(`[skip] 找不到 ${base} 对应的数字人视频（public/digital_human/${base}.mp4）`);
    return null;
  }

  const subtitles = parseSrt(fs.readFileSync(srtPath, "utf8"));
  const subEnd = subtitles.length ? subtitles[subtitles.length - 1].end : 0;
  const duration = probeDuration(path.join(DH_DIR, dhFile), subEnd || 6);

  const used = new Set<string>();
  const inserts: InsertItem[] = [];
  for (const seg of subtitles) {
    for (const [kw, cfg] of Object.entries(keywords)) {
      if (used.has(kw)) continue;
      if (!seg.text.includes(kw)) continue;
      if (!fs.existsSync(path.join(INSERTS_DIR, cfg.src))) {
        console.warn(`[warn] 关键词「${kw}」命中，但素材缺失：public/inserts/${cfg.src}`);
        continue;
      }
      inserts.push({
        time: seg.start,
        duration: cfg.duration,
        type: cfg.type,
        src: cfg.src,
        position: cfg.position ?? DEFAULT_INSERT_POSITION,
      });
      used.add(kw);
    }
  }

  return {
    id: base,
    outputName: base,
    fps: DEFAULT_FPS,
    duration,
    digitalHuman: { src: dhFile },
    inserts: inserts.sort((a, b) => a.time - b.time),
    subtitles,
  };
}

function main() {
  if (!fs.existsSync(SCRIPTS_DIR)) {
    console.error(`找不到 ${SCRIPTS_DIR}`);
    process.exit(1);
  }
  const keywords = loadKeywords();
  const srts = fs
    .readdirSync(SCRIPTS_DIR)
    .filter((f) => f.toLowerCase().endsWith(".srt"));

  if (srts.length === 0) {
    console.warn("public/scripts/ 下没有 .srt 文件，无事可做。");
    return;
  }

  let ok = 0;
  for (const srt of srts) {
    const sb = buildStoryboard(path.join(SCRIPTS_DIR, srt), keywords);
    if (!sb) continue;
    const outPath = path.join(SCRIPTS_DIR, `${sb.id}.storyboard.json`);
    fs.writeFileSync(outPath, JSON.stringify(sb, null, 2), "utf8");
    console.log(
      `[ok] ${srt} → ${path.basename(outPath)}  (时长 ${sb.duration.toFixed(1)}s, 插入 ${sb.inserts.length} 个, 字幕 ${sb.subtitles.length} 段)`
    );
    ok++;
  }
  console.log(`\n完成：生成 ${ok}/${srts.length} 个 storyboard。`);
}

main();
