/**
 * 带样式渲染：1.25 倍速（视频+音频，保持音高）+ 烧录 ASS 字幕（思源粗宋，重点橘黄）。
 * 落实剪辑规范的「字幕 + 语速」部分。B-roll/贴纸/音效/BGM/转场 需另行加素材。
 *
 * 用法: ts-node scripts/render-styled.ts <name> [speed=1.25]
 *   读 public/digital_human/<name>.mp4 + public/scripts/<name>.ass → out/<name>.mp4
 */
import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { execFileSync } from "child_process";

const ROOT = path.resolve(__dirname, "..");
const CANVAS_W = 1080;
const CANVAS_H = 1920;
const FONT_FILE = path.join(ROOT, "assets", "fonts", "SourceHanSerifSC-Heavy.otf");

function main() {
  const name = process.argv[2];
  const speed = parseFloat(process.argv[3] || "1.25");
  if (!name) { console.error("用法: ts-node scripts/render-styled.ts <name> [speed]"); process.exit(1); }

  const video = path.join(ROOT, "public", "digital_human", `${name}.mp4`);
  const ass = path.join(ROOT, "public", "scripts", `${name}.ass`);
  const outPath = path.join(ROOT, "out", `${name}.mp4`);
  for (const f of [video, ass, FONT_FILE]) {
    if (!fs.existsSync(f)) { console.error(`缺文件: ${f}`); process.exit(1); }
  }
  if (!fs.existsSync(path.dirname(outPath))) fs.mkdirSync(path.dirname(outPath), { recursive: true });

  // 工作目录放系统临时区（纯 ASCII），cwd 设此处 → ass 用相对名、字体放 fonts/ 子目录，规避路径转义
  const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "dhmix-"));
  fs.mkdirSync(path.join(workdir, "fonts"));
  fs.copyFileSync(ass, path.join(workdir, "sub.ass"));
  fs.copyFileSync(FONT_FILE, path.join(workdir, "fonts", path.basename(FONT_FILE)));

  // 视频提速 setpts=PTS/speed；音频 atempo=speed（保持音高）；ass 在提速后烧录，时间轴已对齐
  const vf = [
    `setpts=PTS/${speed}`,
    `scale=${CANVAS_W}:${CANVAS_H}:force_original_aspect_ratio=increase`,
    `crop=${CANVAS_W}:${CANVAS_H}`,
    `setsar=1`,
    `ass=sub.ass:fontsdir=fonts`,
  ].join(",");

  const args = [
    "-y",
    "-i", video,
    "-filter_complex",
    `[0:v]${vf}[outv];[0:a]atempo=${speed}[outa]`,
    "-map", "[outv]",
    "-map", "[outa]",
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "128k",
    "-movflags", "+faststart",
    outPath,
  ];

  console.log(`▶ 渲染 ${name}（${speed}x 提速 + 思源粗宋字幕）...`);
  try {
    execFileSync("ffmpeg", args, { cwd: workdir, stdio: "inherit" });
    console.log(`✔ 完成 ${outPath}`);
  } finally {
    fs.rmSync(workdir, { recursive: true, force: true });
  }
}

main();
