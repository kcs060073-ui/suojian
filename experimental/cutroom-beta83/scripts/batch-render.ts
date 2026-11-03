import * as fs from "fs";
import * as path from "path";
import { renderStoryboard } from "./ffmpeg-render";
import type { Storyboard } from "../src/types";

const ROOT = path.resolve(__dirname, "..");
const SCRIPTS_DIR = path.join(ROOT, "public", "scripts");
const OUT_DIR = path.join(ROOT, "out");

// 遍历 public/scripts/*.storyboard.json，逐个用 FFmpeg 渲染；失败跳过不中断。
function main() {
  if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });

  const boards = fs
    .readdirSync(SCRIPTS_DIR)
    .filter((f) => f.endsWith(".storyboard.json"));

  if (boards.length === 0) {
    console.warn("没有找到 storyboard，请先运行 npm run build-storyboard。");
    return;
  }

  const failed: string[] = [];
  let done = 0;

  for (const file of boards) {
    const sb = JSON.parse(
      fs.readFileSync(path.join(SCRIPTS_DIR, file), "utf8")
    ) as Storyboard;
    const outName = sb.outputName || path.basename(file, ".storyboard.json");
    const outPath = path.join(OUT_DIR, `${outName}.mp4`);
    console.log(`\n▶ 渲染 ${outName} ...`);

    try {
      renderStoryboard(sb, outPath);
      console.log(`✔ 完成 ${outPath}`);
      done++;
    } catch (e) {
      console.error(`✘ 渲染失败：${outName}`);
      console.error(e instanceof Error ? e.message : e);
      failed.push(outName);
    }
  }

  console.log(`\n=== 批量渲染结束：成功 ${done}，失败 ${failed.length} ===`);
  if (failed.length) console.log("失败项：" + failed.join(", "));
}

main();
