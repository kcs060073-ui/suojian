import type { MixSfx } from "./types";


export interface BuiltSfxFilters {
  inputs: string[];
  filters: string[];
  labels: string[];
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
    inputs.push("-i", effect.src);
    filters.push(
      `[${inputIndex}:a]adelay=${delayMs}|${delayMs},volume=${gain},` +
      `aformat=channel_layouts=stereo[sfx${index}]`,
    );
    labels.push(`[sfx${index}]`);
  });
  return { inputs, filters, labels };
}
