/**
 * Pexels 空镜检索（fetch-broll）
 * 读 <name>.pending.json（compile-ad 产出的缺素材列表），把中文 query 翻译成英文 →
 * Pexels Video API 检索 → 下载最佳匹配 → 存入 public/library/pexels/。
 * 下载完后重跑 compile-ad 即可命中新素材。
 *
 * 用法: npx ts-node scripts/fetch-broll.ts <name>
 *   读  public/scripts/<name>.pending.json
 *   写  public/library/pexels/<query>.mp4（每段一个文件）
 *
 * 环境变量（见 .env）：PEXELS_API_KEY / ARK_API_KEY / ARK_BASE_URL / ARK_MODEL
 */
import "./load-env";
import * as fs from "fs";
import * as path from "path";
import { execFileSync } from "child_process";

for (const k of ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]) delete process.env[k];

const ROOT = path.resolve(__dirname, "..");
const SCRIPTS = path.join(ROOT, "public", "scripts");
const LIB_PEXELS = path.join(ROOT, "public", "library", "pexels");

const PEXELS_KEY = process.env.PEXELS_API_KEY || "";
const ARK_KEY = process.env.ARK_API_KEY || "";
const ARK_URL = process.env.ARK_BASE_URL || "";
const ARK_MODEL = process.env.ARK_MODEL || "";

interface Pending { query: string; prompt: string; anchorText: string; at: number }

/** 用 DeepSeek 批量翻译中文 query → 英文关键词（一次 API 调用） */
async function translateQueries(queries: string[]): Promise<string[]> {
  const list = queries.map((q, i) => `${i + 1}. ${q}`).join("\n");
  const body = {
    model: ARK_MODEL,
    messages: [
      { role: "system", content: "你是翻译助手。把中文视频素材搜索关键词翻译成英文，用于 Pexels 视频搜索。只输出英文关键词，每行一个，带编号。简洁准确，适合搜索。" },
      { role: "user", content: `翻译以下关键词为英文（用于视频素材搜索）：\n${list}` },
    ],
    temperature: 0.2,
  };
  const r = await fetch(`${ARK_URL}/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${ARK_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j: any = await r.json();
  const text: string = j.choices?.[0]?.message?.content || "";
  // 解析编号列表
  return text.split("\n").map((l) => l.replace(/^\d+\.\s*/, "").trim()).filter(Boolean);
}

interface PexelsVideoFile { link: string; quality: number; width: number; height: number; file_type: string }
interface PexelsVideo { id: number; duration: number; width: number; height: number; video_files: PexelsVideoFile[] }

/** Pexels 视频搜索：返回最佳匹配（优先竖版、时长够、HD） */
async function searchPexels(query: string, orientation: "portrait" | "landscape"): Promise<PexelsVideoFile | null> {
  const url = `https://api.pexels.com/videos/search?query=${encodeURIComponent(query)}&orientation=${orientation}&per_page=5&size=medium`;
  const r = await fetch(url, { headers: { Authorization: PEXELS_KEY } });
  if (!r.ok) { console.warn(`  Pexels搜索失败 ${r.status}: ${query}`); return null; }
  const j: any = await r.json();
  const videos: PexelsVideo[] = j.videos || [];
  if (!videos.length) return null;

  // 选最佳：时长 >= 3s，优先 720p-1080p 的 mp4
  for (const v of videos) {
    const file = v.video_files
      .filter((f) => f.file_type === "video/mp4" && f.quality >= 720 && f.quality <= 1080)
      .sort((a, b) => b.quality - a.quality)[0];
    if (file) return file;
  }
  // 降级：任意 mp4
  for (const v of videos) {
    const file = v.video_files.find((f) => f.file_type === "video/mp4");
    if (file) return file;
  }
  return null;
}

/** 下载文件 */
function download(url: string, dest: string): boolean {
  try {
    execFileSync("curl", ["-sL", "-o", dest, url], { stdio: "ignore", timeout: 60000 });
    return fs.existsSync(dest) && fs.statSync(dest).size > 1000;
  } catch { return false; }
}

async function main() {
  const name = process.argv[2];
  if (!name) { console.error("用法: ts-node scripts/fetch-broll.ts <name>"); process.exit(1); }
  if (!PEXELS_KEY) { console.error("缺 PEXELS_API_KEY（.env）"); process.exit(1); }

  const pendingPath = path.join(SCRIPTS, `${name}.pending.json`);
  if (!fs.existsSync(pendingPath)) { console.log("✔ 无缺素材（pending.json 不存在）"); return; }
  const pending: Pending[] = JSON.parse(fs.readFileSync(pendingPath, "utf8"));
  if (!pending.length) { console.log("✔ 无缺素材"); return; }

  console.log(`▶ Pexels补素材 ${name}（${pending.length}段缺）...`);

  // 1. 批量翻译中文 query → 英文
  console.log("  翻译关键词...");
  const enQueries = await translateQueries(pending.map((p) => p.query));
  if (enQueries.length !== pending.length) {
    console.warn(`  ⚠ 翻译数量不匹配(${enQueries.length}/${pending.length})，按原顺序取前 N 个`);
  }
  pending.forEach((p, i) => {
    const en = enQueries[i] || p.query;
    console.log(`    [${i}] ${p.query} → ${en}`);
    (p as any).en = en;
  });

  // 2. 逐个搜索 + 下载
  fs.mkdirSync(LIB_PEXELS, { recursive: true });
  let ok = 0, fail = 0;
  for (let i = 0; i < pending.length; i++) {
    const p = pending[i];
    const en = (p as any).en || p.query;
    const safeName = p.query.replace(/[\/\\:*?"<>|]/g, "_");
    const dest = path.join(LIB_PEXELS, `${safeName}.mp4`);

    // 已下载过则跳过
    if (fs.existsSync(dest) && fs.statSync(dest).size > 1000) {
      console.log(`  ✓ [${i}] ${p.query} (缓存)`);
      ok++;
      continue;
    }

    const file = await searchPexels(en, "portrait");
    if (!file) {
      console.log(`  ✗ [${i}] ${p.query} → 无结果`);
      fail++;
      continue;
    }
    const success = download(file.link, dest);
    if (success) {
      console.log(`  ✓ [${i}] ${p.query} → ${safeName}.mp4 (${(fs.statSync(dest).size / 1024 / 1024).toFixed(1)}MB)`);
      ok++;
    } else {
      console.log(`  ✗ [${i}] ${p.query} → 下载失败`);
      fail++;
    }
    // Pexels 限速：200次/小时，加 500ms 延迟
    await new Promise((r) => setTimeout(r, 500));
  }

  console.log(`\n✔ 完成: ${ok} 成功 / ${fail} 失败 / ${pending.length} 总计`);
  console.log(`  素材存入 public/library/pexels/`);
  console.log(`  下一步: 重跑 compile-ad ${name} 即可命中新素材`);
}

main().catch((e) => { console.error("✘ fetch-broll 失败:", e?.message || e); process.exit(1); });
