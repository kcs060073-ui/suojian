import type { MixSfx } from "./types";


export interface BuiltSfxFilters {
  inputs: string[];
  filters: string[];
  labels: string[];
}


export function projectSfxToWindow(effects: MixSfx[], start: number, end: number): MixSfx[] {
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) {
    throw new Error("SFX_PREVIEW_WINDOW_INVALID: 音效预览窗口无效");
  }
  return effects.flatMap((effect) => {
    const clippedStart = Math.max(effect.start, start);
    const clippedEnd = Math.min(effect.end ?? Infinity, end);
    if (clippedEnd <= clippedStart) return [];
    const skipped = clippedStart - effect.start;
    const originalDuration = effect.end === undefined ? undefined : effect.end - effect.start;
    const originalFadeOut = effect.fadeOut ?? (originalDuration === undefined ? 0 : Math.min(.08, originalDuration));
    const projectedDuration = clippedEnd - clippedStart;
    const fadeIn = Math.min(projectedDuration, Math.max(0, (effect.fadeIn ?? 0) - skipped));
    const fadeOut = effect.end !== undefined && clippedEnd === effect.end
      ? Math.min(projectedDuration - fadeIn, originalFadeOut) : 0;
    return [{ ...effect, start: clippedStart - start, end: clippedEnd - start,
      sourceStart: (effect.sourceStart ?? 0) + skipped, fadeIn, fadeOut }];
  });
}

export function buildSfxFilters(
  effects: MixSfx[],
  firstInputIndex: number,
): BuiltSfxFilters {
  if (!Number.isInteger(firstInputIndex) || firstInputIndex < 0) {
    throw new Error("firstInputIndex must be a non-negative integer");
  }
  const inputs: string[] = [];
  const filters: string[] = [];
  const labels: string[] = [];
  effects.forEach((effect, index) => {
    if (!effect.src?.trim()) {
      throw new Error("MixSfx src is required");
    }
    if (!Number.isFinite(effect.start) || effect.start < 0) {
      throw new Error("MixSfx start must be a finite non-negative number");
    }
    const gain = effect.gain ?? 0.35;
    if (!Number.isFinite(gain) || gain < 0 || gain > 1) {
      throw new Error("MixSfx gain must be between 0 and 1");
    }
    const inputIndex = firstInputIndex + index;
    const delayMs = Math.round(effect.start * 1000);
    const duration = effect.end === undefined ? undefined : effect.end - effect.start;
    if (duration !== undefined && (!Number.isFinite(duration) || duration <= 0)) {
      throw new Error("MixSfx end must be after start");
    }
    const sourceStart = effect.sourceStart ?? 0;
    if (!Number.isFinite(sourceStart) || sourceStart < 0) {
      throw new Error("MixSfx sourceStart must be a finite non-negative number");
    }
    const fadeIn = effect.fadeIn ?? 0;
    const fadeOut = effect.fadeOut ?? (duration === undefined ? 0 : Math.min(.08, duration));
    if (![fadeIn, fadeOut].every((value) => Number.isFinite(value) && value >= 0)
        || (duration !== undefined && fadeIn + fadeOut > duration + 1e-9)
        || (duration === undefined && fadeOut > 0)) {
      throw new Error("MixSfx fades exceed clip duration or are invalid");
    }
    const n = (value: number) => Number(value.toFixed(6));
    let trim = "";
    if (duration !== undefined || sourceStart > 0) {
      trim = `atrim=${sourceStart > 0 ? `start=${n(sourceStart)}${duration === undefined ? "" : ":"}` : ""}`
        + `${duration === undefined ? "" : `duration=${n(duration)}`},asetpts=PTS-STARTPTS,`;
    }
    if (fadeIn > 0) trim += `afade=t=in:st=0:d=${n(fadeIn)},`;
    if (fadeOut > 0 && duration !== undefined) {
      trim += `afade=t=out:st=${n(duration - fadeOut)}:d=${n(fadeOut)},`;
    }
    inputs.push("-i", effect.src);
    filters.push(
      `[${inputIndex}:a]${trim}adelay=${delayMs}|${delayMs},volume=${gain},` +
      `aformat=channel_layouts=stereo[sfx${index}]`,
    );
    labels.push(`[sfx${index}]`);
  });
  return { inputs, filters, labels };
}
