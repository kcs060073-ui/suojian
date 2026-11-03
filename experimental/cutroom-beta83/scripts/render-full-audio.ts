/**
 * Render the complete timeline BEFORE any preview seeking. Stateful normalization,
 * ducking and limiting must have the same history in previews and final videos.
 */
import * as fs from "fs";
import * as path from "path";
import { execFileSync } from "child_process";
import { AUDIO_MIX_POLICY as POLICY } from "../src/audio-mix-policy";
import { buildSfxFilters } from "../src/mix-sfx";
import type { MixInsert, MixSfx } from "../src/types";

interface FullAudio {
  voice: string;
  speed: number;
  duration?: number;
  bgm?: { src: string; gain?: number; duck?: boolean };
  defaultGain: number;
  inserts?: MixInsert[];
  sfx?: MixSfx[];
  reviewOutput?: string;
  dryRun?: boolean;
}

export function renderFullAudio(spec: FullAudio, work: string): string {
  if ((!(spec.dryRun && spec.duration === undefined) &&
       (!Number.isFinite(spec.duration) || spec.duration! <= 0)) ||
      !Number.isFinite(spec.speed) || spec.speed < .5 || spec.speed > 2) {
    throw new Error("AUDIO_TIMELINE_INVALID: 声音时长或倍速无效");
  }
  const output = path.join(work, "full-mix.wav");
  const trim = spec.duration === undefined ? "" : `atrim=duration=${spec.duration + .25},`;
  const durationArgs = spec.duration === undefined ? [] : ["-t", String(spec.duration)];
  const inputs = ["-i", spec.voice];
  let nextInput = 1;
  const parts = [
    // Leave a short silent tail through tempo, loudness and limiter processing.
    // Trimming before these buffered filters loses their final audio at EOF;
    // the output sink trims to the authoritative duration after all DSP.
    `[0:a]asetpts=PTS-STARTPTS,${spec.duration !== undefined ? "apad=pad_dur=0.5," : ""}` +
      `atempo=${spec.speed},${trim}${POLICY.voiceFilter},asetpts=N/SR/TB[voiceSource]`,
  ];
  const detectors: string[] = [];
  const detector = () => {
    const label = `voiceDetector${detectors.length}`;
    detectors.push(`[${label}]`);
    return `[${label}]`;
  };
  const ambient: string[] = [];
  for (const [i, ins] of (spec.inserts ?? []).entries()) {
    if (!["keep", "duck", "clean"].includes(ins.audio ?? "mute")) continue;
    const hasAudio = spec.dryRun || execFileSync("ffprobe", ["-v", "error",
      "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", ins.src],
      { encoding: "utf8", timeout: 30000, maxBuffer: 65536 }).trim().length > 0;
    if (!hasAudio) continue;
    const duration = ins.end - ins.start;
    const sourceStart = ins.sourceStart ?? 0;
    if (![ins.start, duration, sourceStart].every(Number.isFinite) ||
        ins.start < 0 || duration <= 0 || sourceStart < 0) {
      throw new Error("AUDIO_INSERT_TIME_INVALID: 同期声时间无效");
    }
    inputs.push("-i", ins.src);
    parts.push(`[${nextInput++}:a]atrim=start=${sourceStart}:duration=${duration},asetpts=PTS-STARTPTS,` +
      `aresample=${POLICY.sampleRate},aformat=channel_layouts=stereo,volume=0.7,` +
      (ins.audio === "clean" ? "highpass=f=80,afftdn=nr=12," : "") +
      `adelay=${Math.round(ins.start * 1000)}:all=1[ambientRaw${i}]`);
    parts.push(`[ambientRaw${i}]${detector()}sidechaincompress=threshold=0.015:ratio=8:attack=15:release=250[ambient${i}]`);
    ambient.push(`[ambient${i}]`);
  }
  const music = spec.bgm;
  const review = Boolean(spec.reviewOutput && music);
  let musicLabel = "";
  if (music) {
    const gain = music.gain ?? spec.defaultGain;
    if (!Number.isFinite(gain) || gain < 0 || gain > 1) throw new Error("BGM_GAIN_INVALID");
    inputs.push("-stream_loop", "-1", "-i", music.src);
    parts.push(`[${nextInput++}:a]asetpts=PTS-STARTPTS,${trim}` +
      `aresample=${POLICY.sampleRate},aformat=channel_layouts=stereo,volume=${gain}[musicRaw]`);
    if (music.duck !== false) {
      parts.push(`[musicRaw]${detector()}${POLICY.duckFilter}[musicDucked]`);
      musicLabel = "[musicDucked]";
    } else musicLabel = "[musicRaw]";
    if (review) {
      parts.push(`${musicLabel}asplit=2[musicMix][musicReview]`);
      musicLabel = "[musicMix]";
    }
  }
  const voiceOutputs = ["[voiceMix]", ...detectors, ...(review ? ["[voiceReview]"] : [])];
  parts.push(voiceOutputs.length > 1
    ? `[voiceSource]asplit=${voiceOutputs.length}${voiceOutputs.join("")}`
    : "[voiceSource]anull[voiceMix]");
  const sfx = buildSfxFilters(spec.sfx ?? [], nextInput);
  inputs.push(...sfx.inputs);
  parts.push(...sfx.filters);
  const mix = ["[voiceMix]", ...ambient, ...sfx.labels, ...(musicLabel ? [musicLabel] : [])];
  parts.push(mix.length > 1
    ? `${mix.join("")}amix=inputs=${mix.length}:duration=first:normalize=0,${POLICY.limiterFilter}[mixed]`
    : `[voiceMix]${POLICY.limiterFilter}[mixed]`);
  if (review) {
    // Four PCM channels: voice L/R, ducked music L/R. No second normalization.
    parts.push("[voiceReview][musicReview]join=inputs=2:channel_layout=4.0:" +
      "map=0.0-FL|0.1-FR|1.0-FC|1.1-BC[review]");
  }
  // Buffered DSP can retain samples but shift packet PTS. Use the actual
  // sample clock before the sink enforces the confirmed timeline.
  parts.push("[mixed]asetpts=N/SR/TB[outputAudio]");
  const args = ["-nostdin", "-y", "-v", "error", ...inputs, "-filter_complex", parts.join(";"),
    "-map", "[outputAudio]", ...durationArgs, "-ar", String(POLICY.sampleRate),
    "-ac", "2", "-c:a", "pcm_s24le", output,
    ...(review ? ["-map", "[review]", ...durationArgs, "-ar", String(POLICY.sampleRate),
      "-c:a", "pcm_f32le", path.join(work, "review-stems.wav")] : [])];
  if (spec.dryRun) {
    console.log("Full timeline audio:", JSON.stringify(args));
    return output;
  }
  execFileSync("ffmpeg", args, { cwd: work, stdio: "pipe", timeout: 600000, maxBuffer: 1024 * 1024 });
  if (review) {
    const destination = path.resolve(spec.reviewOutput!);
    if (fs.existsSync(destination)) throw new Error("AUDIO_REVIEW_EXISTS: 不覆盖既有测量分轨");
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.copyFileSync(path.join(work, "review-stems.wav"), destination, fs.constants.COPYFILE_EXCL);
  }
  return output;
}
