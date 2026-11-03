/**
 * 信息流广告一键出片
 * 文案 → AI分镜 → TTS → 配片 → 渲染，4步串联，输入txt输出mp4。
 *
 * 用法: npx ts-node scripts/ad-once.ts <name> [ratio=9:16] [bgm路径] [--fetch]
 *   读  public/scripts/<name>.script.txt
 *   产  out/<name>.mp4
 *
 * 参数:
 *   name    文案名（public/scripts/<name>.script.txt）
 *   ratio   画幅 9:16(竖·默认) | 16:9(横)
 *   bgm     背景音乐路径（可选）
 *   --fetch 有缺素材时自动走Pexels检索补齐再重跑配片（不加则用占位屏兜底出片）
 */
import { execSync } from "child_process";
import * as fs from "fs";
import * as path from "path";

const ROOT = path.resolve(__dirname, "..");
const SCRIPTS = path.join(ROOT, "public", "scripts");

const name = process.argv[2];
const ratio = process.argv[3] || "9:16";
const bgm = process.argv[4] || "";
const doFetch = process.argv.includes("--fetch");

if (!name) {
  console.error("用法: npx ts-node scripts/ad-once.ts <name> [ratio=9:16] [bgm] [--fetch]");
  console.error("  读 public/scripts/<name>.script.txt → 产 out/<name>.mp4");
  process.exit(1);
}

const t0 = Date.now();
const run = (desc: string, cmd: string) => {
  const s = Date.now();
  console.log(`\n▶ ${desc}`);
  execSync(cmd, { cwd: ROOT, stdio: "inherit" });
  console.log(`  ✔ ${((Date.now() - s) / 1000) | 0}s`);
};

try {
  if (!fs.existsSync(path.join(SCRIPTS, `${name}.script.txt`))) {
    throw new Error(`缺文案: public/scripts/${name}.script.txt`);
  }

  run("① AI分镜（plan-ad）", `npx ts-node scripts/plan-ad.ts "${name}" "${ratio}"`);
  run("② TTS配音", `npx ts-node scripts/tts.ts "${name}"`);
  run("③ 配片（compile-ad）", `npx ts-node scripts/compile-ad.ts "${name}"`);

  // 待补素材：--fetch 时自动 Pexels 检索补齐再重跑配片；否则 render-ad 用占位屏兜底出片
  const pending = path.join(SCRIPTS, `${name}.pending.json`);
  if (fs.existsSync(pending)) {
    const cnt = JSON.parse(fs.readFileSync(pending, "utf8")).length;
    if (doFetch) {
      run("③b Pexels补素材", `npx ts-node scripts/fetch-broll.ts "${name}"`);
      run("③c 重跑配片", `npx ts-node scripts/compile-ad.ts "${name}"`);
    } else {
      console.log(`\n⚠ ${cnt}段缺素材，用占位屏兜底出片（加 --fetch 自动Pexels检索补，或补素材后重跑）`);
    }
  }

  run("④ 渲染出片（render-ad）", `npx ts-node scripts/render-ad.ts "${name}"${bgm ? ` "${bgm}"` : ""}`);

  const total = ((Date.now() - t0) / 1000) | 0;
  console.log(`\n========== ✔ 出片完成 ==========`);
  console.log(`  总耗时 ${Math.floor(total / 60)}分${total % 60}秒`);
  console.log(`  → out/${name}.mp4`);
} catch (e) {
  console.error(`\n✘ 出片失败: ${(e as Error).message}`);
  process.exit(1);
}
