import type { SubtitleItem } from "../src/types";

// 把 SRT 时间戳 "HH:MM:SS,mmm" 解析成秒。
function parseTimestamp(ts: string): number {
  const m = ts.trim().match(/(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})/);
  if (!m) return 0;
  const [, h, min, s, ms] = m;
  return (
    Number(h) * 3600 +
    Number(min) * 60 +
    Number(s) +
    Number(ms.padEnd(3, "0")) / 1000
  );
}

// 解析 SRT 文本为 SubtitleItem[]。容忍 BOM、CRLF、多行文本块。
export function parseSrt(content: string): SubtitleItem[] {
  const text = content.replace(/^﻿/, "").replace(/\r\n/g, "\n");
  const blocks = text.split(/\n{2,}/);
  const items: SubtitleItem[] = [];

  for (const block of blocks) {
    const lines = block.split("\n").filter((l) => l.trim() !== "");
    if (lines.length === 0) continue;

    // 找到含 "-->" 的时间行（序号行可有可无）
    const timeLineIdx = lines.findIndex((l) => l.includes("-->"));
    if (timeLineIdx === -1) continue;

    const [startRaw, endRaw] = lines[timeLineIdx].split("-->");
    const start = parseTimestamp(startRaw);
    const end = parseTimestamp(endRaw);
    const textLines = lines.slice(timeLineIdx + 1);
    const subText = textLines.join(" ").trim();
    if (subText) items.push({ start, end, text: subText });
  }

  return items.sort((a, b) => a.start - b.start);
}
