export interface AdWindowSegment {
  start: number;
  end: number;
  dur: number;
  sourceStart?: number;
  sourceEnd?: number;
}

export interface ProjectedAdWindow<T extends AdWindowSegment> {
  timelineStart: number;
  segments: T[];
}

/** Retain the preceding shot only when the window opens inside its xfade. */
export function projectAdPreviewWindow<T extends AdWindowSegment>(
  segments: T[], start: number, end: number, transitionSeconds: number,
): ProjectedAdWindow<T> {
  if (!segments.length || !Number.isFinite(start) || !Number.isFinite(end)
    || start < 0 || end <= start || end > segments[segments.length - 1].end + 1e-6) {
    throw new Error("AD_PREVIEW_WINDOW_INVALID: 短预览窗口超出锁定时间轴");
  }
  let active = 0;
  for (let index = 1; index < segments.length && segments[index].start <= start; index++) {
    active = index;
  }
  const insideEntrance = active > 0 && start < segments[active].start + transitionSeconds;
  const first = insideEntrance ? active - 1 : active;
  const timelineStart = insideEntrance
    ? Math.max(segments[first].start, start - transitionSeconds)
    : start;
  let last = first;
  for (let index = first + 1; index < segments.length && segments[index].start < end; index++) {
    last = index;
  }
  const projected = segments.slice(first, last + 1).map((segment, offset) => {
    const index = first + offset;
    const skipped = index === first ? timelineStart - segment.start : 0;
    const slotEnd = index === last ? Math.min(segment.end, end) : segment.end;
    const dur = slotEnd - segment.start - skipped;
    if (dur <= 0) throw new Error("AD_PREVIEW_WINDOW_INVALID: 窗口内没有有效镜头帧");
    const sourceStart = (segment.sourceStart ?? 0) + skipped;
    return {
      ...segment,
      start: segment.start + skipped,
      end: slotEnd,
      dur,
      sourceStart,
      sourceEnd: sourceStart + dur,
    };
  });
  return { timelineStart, segments: projected };
}
