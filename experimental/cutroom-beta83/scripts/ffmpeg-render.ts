import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { execFileSync } from "child_process";
import type { Storyboard, InsertItem } from "../src/types";

const ROOT = path.resolve(__dirname, "..");
const DH_DIR = path.join(ROOT, "public", "digital_human");
const INSERTS_DIR = path.join(ROOT, "public", "inserts");

const CANVAS_W = 1080;
const CANVAS_H = 1920;

// 把 storyboard.subtitles 写成 SRT，供 FFmpeg subtitles 滤镜烧录。
// 放在系统临时目录（纯 ASCII 路径），规避 subtitles 滤镜对中文/冒号路径的转义难题。
function writeSrt(sb: Storyboard, workdir: string): string {
  const pad = (n: number, w = 2) => String(Math.floor(n)).padStart(w, "0");
  const fmt = (t: number) => {
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    const s = Math.floor(t % 60);
    const ms = Math.round((t - Math.floor(t)) * 1000);
    return `${pad(h)}:${pad(m)}:${pad(s)},${pad(ms, 3)}`;
  };
  const body = sb.subtitles
    .map((s, i) => `${i + 1}\n${fmt(s.start)} --> ${fmt(s.end)}\n${s.text}\n`)
    .join("\n");
  const file = path.join(workdir, "sub.srt");
  fs.writeFileSync(file, body, "utf8");
  return file;
}

// 构建单个插入素材的滤镜片段；返回 [filterChain, labelIn->labelOut]。
function insertFilter(item: InsertItem, idx: number, inLabel: string): {
  chain: string;
  outLabel: string;
} {
  const W = Math.round(item.position.width * CANVAS_W);
  const H = Math.round(item.position.height * CANVAS_H);
  const X = Math.round(item.position.x * CANVAS_W);
  const Y = Math.round(item.position.y * CANVAS_H);
  const start = item.time;
  const end = item.time + item.duration;
  const enable = `between(t,${start},${end})`;
  const radius = item.style?.borderRadius ?? 0; // 圆角暂以普通矩形处理，FFmpeg 圆角成本高，先省略
  const ovk = `ov${idx}`;
  const out = `t${idx}`;

  // 输入流标签：图片/视频都是第 (idx+1) 个 -i 输入
  const inputV = `${idx + 1}:v`;

  let prep: string;
  if (item.type === "video") {
    // 视频插入：裁到 duration，时间轴平移到 start 出现
    prep = `[${inputV}]trim=0:${item.duration},setpts=PTS-STARTPTS+${start}/TB,scale=${W}:${H},setsar=1[${ovk}]`;
  } else {
    // 图片插入：单帧，overlay 默认 eof_action=repeat 会保持显示，由 enable 控制时间窗
    prep = `[${inputV}]scale=${W}:${H},setsar=1[${ovk}]`;
  }
  void radius;
  const overlay = `[${inLabel}][${ovk}]overlay=${X}:${Y}:enable='${enable}'[${out}]`;
  return { chain: `${prep};${overlay}`, outLabel: out };
}

export function renderStoryboard(sb: Storyboard, outPath: string): void {
  const dhPath = path.join(DH_DIR, sb.digitalHuman.src);
  if (!fs.existsSync(dhPath)) {
    throw new Error(`数字人视频缺失: ${dhPath}`);
  }

  // 工作目录放系统临时区（ASCII），cwd 设到这里后字幕用相对名 sub.srt，免去路径转义
  const workdir = fs.mkdtempSync(path.join(os.tmpdir(), "dhmix-"));
  const subRel = path.basename(writeSrt(sb, workdir));

  const inputs: string[] = ["-i", dhPath];
  for (const ins of sb.inserts) {
    inputs.push("-i", path.join(INSERTS_DIR, ins.src));
  }

  // 底层：缩放铺满 9:16 后中心裁切
  const parts: string[] = [
    `[0:v]scale=${CANVAS_W}:${CANVAS_H}:force_original_aspect_ratio=increase,crop=${CANVAS_W}:${CANVAS_H},setsar=1[base]`,
  ];
  let last = "base";
  sb.inserts.forEach((ins, i) => {
    const { chain, outLabel } = insertFilter(ins, i, last);
    parts.push(chain);
    last = outLabel;
  });

  // 字幕烧录（相对路径，cwd=workdir）。字号按 9:16 调；描边模拟黑边白字。
  const style =
    "FontName=Microsoft YaHei,Fontsize=14,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=90";
  parts.push(`[${last}]subtitles=${subRel}:force_style='${style}'[outv]`);

  const filter = parts.join(";");

  const args = [
    "-y",
    ...inputs,
    "-filter_complex",
    filter,
    "-map",
    "[outv]",
    "-map",
    "0:a?",
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    "-movflags",
    "+faststart",
    "-shortest",
    outPath,
  ];

  try {
    execFileSync("ffmpeg", args, { cwd: workdir, stdio: "inherit" });
  } finally {
    fs.rmSync(workdir, { recursive: true, force: true });
  }
}
