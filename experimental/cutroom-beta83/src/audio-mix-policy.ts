import * as fs from "fs";
import * as path from "path";

// Included in frozen recipes and runtime dependency snapshots.
export const AUDIO_MIX_POLICY = JSON.parse(
  fs.readFileSync(path.join(__dirname, "audio-mix-policy-data.json"), "utf8"),
) as { version: string; sampleRate: number; bitrate: string;
       voiceFilter: string; duckFilter: string; limiterFilter: string };
export const OUTPUT_AUDIO_RATE = AUDIO_MIX_POLICY.sampleRate;
export const BGM_DUCK_FILTER = AUDIO_MIX_POLICY.duckFilter;

export function assertAudioMixPolicy(value: unknown): void {
  if (!value || typeof value !== "object" ||
      Object.entries(AUDIO_MIX_POLICY).some(([key, expected]) =>
        (value as Record<string, unknown>)[key] !== expected)) {
    throw new Error("AUDIO_POLICY_STALE: 声音处理规则已更新，请重新生成预览");
  }
}
