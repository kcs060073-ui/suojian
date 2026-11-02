/**
 * 文案锚点 → 最终成片时间轴（秒）映射。
 * 镜像 make-ass.ts 的字幕时序公式（loadCharTimeline + 发音字比例映射 + /speed），
 * 保证 compile-plan 算出的 B-roll/插入时间与烧录的字幕落在同一条时间轴上。
 *
 * 说明：make-ass 内部保留同一套逻辑（其字幕时序与本模块独立但公式一致）。改公式时两处需同步。
 */

export interface Word {
  w: string;
  start: number;
  end: number;
}

const PUNC = /[\s，。、；：？！…""''（）()《》【】\-—~·.]/;

function loadCharTimeline(words: Word[]): { start: number; end: number }[] {
  const out: { start: number; end: number }[] = [];
  for (const wd of words) {
    const chars = [...wd.w].filter((c) => c.trim() !== "");
    if (chars.length === 0) continue;
    const span = (wd.end - wd.start) / chars.length;
    chars.forEach((_, i) => {
      out.push({ start: wd.start + span * i, end: wd.start + span * (i + 1) });
    });
  }
  return out;
}

export interface AnchorTimer {
  /** 给定文案中的原文子串，返回它在最终成片时间轴上的 [start,end]（秒）；找不到返回 null。 */
  timeOf(anchor: string): { start: number; end: number } | null;
}

export function buildAnchorTimer(scriptText: string, words: Word[], speed: number): AnchorTimer {
  const timeline = loadCharTimeline(words);
  const M = timeline.length;

  // 文案去标点后的「发音字」序列：原文 index -> 发音序号
  const spokenRank = new Map<number, number>();
  let rank = 0;
  let N = 0;
  for (let i = 0; i < scriptText.length; i++) {
    if (!PUNC.test(scriptText[i])) {
      spokenRank.set(i, rank++);
      N++;
    }
  }

  const tAt = (k: number, which: "start" | "end") => {
    const j = Math.min(Math.max(Math.round((k / Math.max(1, N)) * M), 0), M - 1);
    if (M === 0) return 0;
    return timeline[j][which] / speed;
  };

  return {
    timeOf(anchor: string) {
      const idx = scriptText.indexOf(anchor);
      if (idx < 0) return null;
      let firstRank = -1;
      let lastRank = -1;
      for (let i = idx; i < idx + anchor.length; i++) {
        if (spokenRank.has(i)) {
          if (firstRank < 0) firstRank = spokenRank.get(i)!;
          lastRank = spokenRank.get(i)!;
        }
      }
      if (firstRank < 0) return null;
      const start = Math.max(0, tAt(firstRank, "start"));
      const end = Math.max(start + 0.5, tAt(lastRank + 1, "end"));
      return { start, end };
    },
  };
}
