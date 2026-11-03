import { assertAudioMixPolicy, OUTPUT_AUDIO_RATE } from "../src/audio-mix-policy";
/** Validate and project one frozen digital-human Recipe for render-mix. */
import { createHash } from "node:crypto";
import path from "node:path";

type Shot = {
  index: number; shot_id: string; asset_id: string | null;
  media_kind: "presenter" | "video" | "image"; src: string;
  output_start_us: number; output_end_us: number;
  source_start_us?: number; source_end_us?: number;
  output_start_frame: number; output_end_frame: number;
  render_value: Record<string, unknown> | null;
};
type Instance = { kind: string; start_us: number; end_us: number;
  render_value: Record<string, unknown> };
export type DigitalRecipe = {
  audio_policy: unknown;
  recipe_version: number;
  task_snapshot: { task_id: string; task_type: string; aspect_ratio: string };
  duration_us: number;
  output: { width: number; height: number; fps_num: number; fps_den: number;
    pixel_format: string; audio_rate: number; video_codec: string;
    audio_codec: string; crf: number };
  shots: Shot[];
  main_audio: { src: string; source_start_us: number; source_end_us: number;
    output_start_us: number; output_end_us: number;
    playback_rate_num: number; playback_rate_den: number };
  subtitle_cues: Array<{ text: string; start_us: number; end_us: number }>;
  subtitle_appearance?: import("../src/subtitle-appearance").SubtitleAppearance;
  subtitle_style: { preset_id?: string | null; ass_path?: string | null };
  packaging_instances: Instance[];
  transitions: unknown[];
  asset_manifest: unknown[];
  dependency_digest: string;
};

const validUs = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

export function resolveDigitalRecipePayload(
  envelope: any, expectedInput: string, expectedAss: string,
): { plan: any; recipe: DigitalRecipe | null } {
  if (envelope.renderRecipeJson === undefined) return { plan: envelope, recipe: null };
  if (typeof envelope.renderRecipeJson !== "string" ||
      typeof envelope.recipe_digest !== "string") {
    throw new Error("RECIPE_INVALID: 缺少冻结配方正文或摘要");
  }
  const digest = createHash("sha256").update(envelope.renderRecipeJson, "utf8").digest("hex");
  if (digest !== envelope.recipe_digest) {
    throw new Error("RECIPE_DIGEST_MISMATCH: 数字人配方内容已变化");
  }
  let recipe: DigitalRecipe;
  try { recipe = JSON.parse(envelope.renderRecipeJson) as DigitalRecipe; }
  catch { throw new Error("RECIPE_INVALID: 冻结配方 JSON 无效"); }
  if (recipe.recipe_version !== 1 || recipe.task_snapshot?.task_type !== "digital_human" ||
      recipe.task_snapshot.aspect_ratio !== "9:16" ||
      !validUs(recipe.duration_us) || recipe.duration_us === 0 ||
      !Array.isArray(recipe.shots) || recipe.shots.length === 0 ||
      !Array.isArray(recipe.subtitle_cues) ||
      !Array.isArray(recipe.packaging_instances) ||
      !Array.isArray(recipe.transitions) || recipe.transitions.length !== 0 ||
      !Array.isArray(recipe.asset_manifest) ||
      typeof recipe.dependency_digest !== "string") {
    throw new Error("RECIPE_INVALID: 数字人配方结构无效");
  }
  assertAudioMixPolicy(recipe.audio_policy);
  if (recipe.output?.width !== 1080 || recipe.output.height !== 1920 ||
      !Number.isSafeInteger(recipe.output.fps_num) || recipe.output.fps_num <= 0 ||
      !Number.isSafeInteger(recipe.output.fps_den) || recipe.output.fps_den <= 0 ||
      recipe.output.audio_rate !== OUTPUT_AUDIO_RATE ||
      recipe.output.pixel_format !== "yuv420p" ||
      recipe.output.video_codec !== "libx264" ||
      recipe.output.audio_codec !== "aac" || recipe.output.crf !== 20) {
    throw new Error("RECIPE_OUTPUT_UNSUPPORTED: 当前数字人渲染器输出配置不匹配");
  }
  if (path.resolve(recipe.main_audio?.src || "") !== path.resolve(expectedInput) ||
      recipe.main_audio.playback_rate_num !== 5 ||
      recipe.main_audio.playback_rate_den !== 4 ||
      recipe.main_audio.output_start_us !== 0 ||
      recipe.main_audio.output_end_us !== recipe.duration_us ||
      recipe.main_audio.source_start_us !== 0 ||
      recipe.main_audio.source_end_us < recipe.duration_us * 5 / 4) {
    throw new Error("RECIPE_MAIN_AUDIO_MISMATCH: 配方口播主轨与输入不一致");
  }
  if (recipe.subtitle_style?.ass_path &&
      path.resolve(recipe.subtitle_style.ass_path) !== path.resolve(expectedAss)) {
    throw new Error("RECIPE_SUBTITLE_MISMATCH: 冻结 ASS 与执行输入不一致");
  }
  const inserts: Array<Record<string, unknown>> = [];
  let cursor = 0;
  for (let index = 0; index < recipe.shots.length; index++) {
    const shot = recipe.shots[index];
    if (shot.index !== index || !validUs(shot.output_start_us) ||
        !validUs(shot.output_end_us) || shot.output_start_us !== cursor ||
        shot.output_end_us <= cursor || shot.output_end_us > recipe.duration_us ||
        !validUs(shot.output_start_frame) || !validUs(shot.output_end_frame) ||
        shot.output_start_frame > shot.output_end_frame) {
      throw new Error("RECIPE_TIMELINE_MISMATCH: 数字人镜头时间轴不连续");
    }
    if (shot.media_kind === "image") {
      if (shot.source_start_us !== undefined || shot.source_end_us !== undefined) {
        throw new Error("IMAGE_HAS_SOURCE_WINDOW: 图片不能携带视频入出点");
      }
    } else if (!validUs(shot.source_start_us) || !validUs(shot.source_end_us) ||
               shot.source_end_us - shot.source_start_us <
                 shot.output_end_us - shot.output_start_us) {
      throw new Error("SOURCE_WINDOW_TOO_SHORT: 数字人镜头源窗口不足");
    }
    if (shot.media_kind === "presenter") {
      if (path.resolve(shot.src) !== path.resolve(expectedInput) || shot.render_value !== null) {
        throw new Error("RECIPE_PRESENTER_MISMATCH: 口播主画面来源不一致");
      }
    } else if (shot.media_kind === "video" || shot.media_kind === "image") {
      const value = shot.render_value;
      if (!value || path.resolve(String(value.src || "")) !== path.resolve(shot.src) ||
          Math.round(Number(value.start) * 1_000_000) !== shot.output_start_us ||
          Math.round(Number(value.end) * 1_000_000) !== shot.output_end_us) {
        throw new Error("RECIPE_SHOT_MISMATCH: 插入画面与冻结镜头不一致");
      }
      inserts.push(value);
    } else {
      throw new Error("RECIPE_MEDIA_KIND_INVALID: 镜头媒体类型无效");
    }
    cursor = shot.output_end_us;
  }
  if (cursor !== recipe.duration_us ||
      recipe.subtitle_cues.some((cue) => !validUs(cue.start_us) ||
        !validUs(cue.end_us) || cue.start_us >= cue.end_us ||
        cue.end_us > recipe.duration_us || typeof cue.text !== "string")) {
    throw new Error("RECIPE_TIMELINE_MISMATCH: 数字人字幕或镜头超出成片");
  }
  const plan: any = {
    speed: 1.25, inserts, subtitleAppearance: recipe.subtitle_appearance, subtitlePresetId: recipe.subtitle_style?.preset_id,
    output: envelope.output ?? { width: 1080, height: 1920, crf: 20 },
  };
  for (const instance of recipe.packaging_instances) {
    if (!validUs(instance.start_us) || !validUs(instance.end_us) ||
        instance.start_us >= instance.end_us || instance.end_us > recipe.duration_us ||
        !instance.render_value || typeof instance.render_value !== "object") {
      throw new Error("RECIPE_PACKAGING_INVALID: 包装实例超出成片");
    }
    if (instance.kind === "bgm" || instance.kind === "watermark") {
      if (plan[instance.kind]) throw new Error("RECIPE_PACKAGING_DUPLICATE: 单实例包装层重复");
      plan[instance.kind] = instance.render_value;
    } else if (["sfx", "titles", "stickers", "fancyTexts"].includes(instance.kind)) {
      (plan[instance.kind] ??= []).push(instance.render_value);
    } else {
      throw new Error("RECIPE_PACKAGING_INVALID: 未知包装层");
    }
  }
  return { plan, recipe };
}
