/**
 * 实体信息流广告 · P3 素材编排（墙到墙逐句配片）。
 * 读 <name>.adplan.json（分镜）+ <name>.adtiming.json（句级时间）→ 给每段配一条全屏素材：
 *   1. 本地库优先：递归扫 public/library/（含子目录如「周王庙 空镜」），按中文文件名标签 + 横竖版朝向匹配 query
 *   2. 命中 → 写入 <name>.adrender.json（P4 render-ad 消费）；未命中 → 写入 <name>.pending.json
 *      （格式兼容 fetch-broll/generate-broll，可自动检索/生成后重跑本脚本补齐）
 *
 * 朝向：ratio=9:16 优先「竖版」素材，16:9 优先「横版」；朝向不符的降权（可由 render-ad 居中裁切兜底）。
 *
 * 用法: ts-node scripts/compile-ad.ts <name>
 *   依赖 <name>.adplan.json + <name>.adtiming.json（先跑 plan-ad + tts）
 */
import * as fs from "fs";
import * as path from "path";

const ROOT = path.resolve(__dirname, "..");
const SCRIPTS = path.join(ROOT, "public", "scripts");
const LIBRARY = path.join(ROOT, "public", "library");

const VID_IMG = /\.(png|jpg|jpeg|webp|mp4|mov|mkv|webm|m4v)$/i;

interface AdSeg { text: string; query: string; prompt: string; type: "video" | "image" }
interface Timing { i: number; text: string; start: number; end: number; dur: number }
interface LibItem { tag: string; rel: string; orient: "h" | "v" | null }

function tokens(s: string): Set<string> {
  const out = new Set<string>();
  for (const part of s.split(/[\s,，、/_\-]+/).filter(Boolean)) {
    out.add(part);
    for (const ch of part) if (ch.trim()) out.add(ch);
  }
  return out;
}

function orientOf(name: string): "h" | "v" | null {
  if (/竖版|竖屏|vertical/i.test(name)) return "v";
  if (/横版|横屏|horizontal/i.test(name)) return "h";
  return null;
}

// 递归扫库（含子目录）+ manifest 标签
function loadLibrary(): LibItem[] {
  const out: LibItem[] = [];
  const manifestPath = path.join(LIBRARY, "manifest.json");
  if (fs.existsSync(manifestPath)) {
    try {
      const m = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as Record<string, string>;
      for (const [tag, file] of Object.entries(m)) {
        const full = path.join(LIBRARY, file);
        if (fs.existsSync(full)) out.push({ tag, rel: path.relative(ROOT, full), orient: orientOf(file) });
      }
    } catch (e) { console.warn(`⚠ manifest.json 解析失败: ${(e as Error).message}`); }
  }
  const walk = (dir: string) => {
    for (const f of fs.readdirSync(dir)) {
      const full = path.join(dir, f);
      const st = fs.statSync(full);
      if (st.isDirectory()) { walk(full); continue; }
      if (f === "manifest.json" || !VID_IMG.test(f)) continue;
      const rel = path.relative(ROOT, full);
      if (out.some((e) => e.rel === rel)) continue; // manifest 已含
      out.push({ tag: path.basename(f, path.extname(f)), rel, orient: orientOf(f) });
    }
  };
  if (fs.existsSync(LIBRARY)) walk(LIBRARY);
  return out;
}

// 按 token 重合度 + 朝向偏好打分，返回最佳素材。
// content<2（没有任何双字词命中、只靠单字重合）一律不算命中，杀掉「行车→停车位」这类误配。
// used：已用过的素材降权，逼它在多条同类片间分散，避免整片重复同一画面。
function match(query: string, want: "h" | "v", lib: LibItem[], used: Map<string, number>): LibItem | null {
  const q = tokens(query);
  let best: { it: LibItem; score: number } | null = null;
  for (const it of lib) {
    const t = tokens(it.tag);
    let content = 0;
    for (const tok of q) if (t.has(tok)) content += tok.length >= 2 ? 2 : 1;
    if (content < 2) continue; // 至少一个双字词命中
    let score = content;
    if (it.orient === want) score += 3; // 朝向匹配加分
    else if (it.orient && it.orient !== want) score *= 0.5; // 朝向相反降权（render 可居中裁切兜底）
    score -= (used.get(it.rel) || 0) * 2; // 已用过降权
    if (!best || score > best.score) best = { it, score };
  }
  return best ? best.it : null;
}

function main() {
  const name = process.argv[2];
  if (!name) { console.error("用法: ts-node scripts/compile-ad.ts <name>"); process.exit(1); }
  const planPath = path.join(SCRIPTS, `${name}.adplan.json`);
  const timingPath = path.join(SCRIPTS, `${name}.adtiming.json`);
  if (!fs.existsSync(planPath)) { console.error(`缺分镜: ${planPath}（先跑 plan-ad）`); process.exit(1); }
  if (!fs.existsSync(timingPath)) { console.error(`缺时间表: ${timingPath}（先跑 tts）`); process.exit(1); }

  const plan = JSON.parse(fs.readFileSync(planPath, "utf8")) as { ratio: string; segments: AdSeg[] };
  const timingDoc = JSON.parse(fs.readFileSync(timingPath, "utf8")) as { total: number; segments: Timing[] };
  const segs = plan.segments;
  const timing = timingDoc.segments;
  const want: "h" | "v" = plan.ratio === "16:9" ? "h" : "v";
  const lib = loadLibrary();
  console.log(`▶ 编排 ${name}（ratio=${plan.ratio} 朝向优先=${want} | 库 ${lib.length} 条）...`);

  const renderSegs: any[] = [];
  const pending: any[] = [];
  const used = new Map<string, number>();
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    const t = timing[i] || { start: 0, end: 0, dur: 0 };
    const hit = match(seg.query, want, lib, used);
    const base = { i, text: seg.text, start: t.start, end: t.end, dur: t.dur, type: seg.type, query: seg.query };
    if (hit) {
      used.set(hit.rel, (used.get(hit.rel) || 0) + 1);
      renderSegs.push({ ...base, src: hit.rel.replace(/\\/g, "/"), matched: true, orient: hit.orient });
    } else {
      renderSegs.push({ ...base, src: null, matched: false });
      pending.push({ query: seg.query, prompt: seg.prompt, anchorText: seg.text, kind: seg.type === "image" ? "image" : "broll", at: t.start });
    }
  }

  const adrender = { name, ratio: plan.ratio, total: timingDoc.total, voice: `public/ad_audio/${name}.voice.mp3`, segments: renderSegs };
  fs.writeFileSync(path.join(SCRIPTS, `${name}.adrender.json`), JSON.stringify(adrender, null, 2), "utf8");

  const pendingPath = path.join(SCRIPTS, `${name}.pending.json`);
  if (pending.length) fs.writeFileSync(pendingPath, JSON.stringify(pending, null, 2), "utf8");
  else if (fs.existsSync(pendingPath)) fs.writeFileSync(pendingPath, "[]", "utf8"); // 覆写空数组替代删除（safe-delete 守卫拦截 rmSync）

  const matched = renderSegs.filter((s) => s.matched).length;
  console.log(`✔ adrender: ${segs.length} 段 | 本地命中 ${matched} | 待补 ${pending.length} → ${name}.adrender.json`);
  if (pending.length) {
    console.log(`  待补（写入 ${name}.pending.json，跑 gen-broll 生成 / fetch-broll 检索 / 人工放库后重跑本脚本）:`);
    for (const p of pending) console.log(`    [${p.at}s] «${p.query}» ← ${p.anchorText}`);
  }
  for (const s of renderSegs.filter((x) => x.matched)) console.log(`    ✓ [${s.start}s] «${s.query}» → ${path.basename(s.src)}`);
}

main();
