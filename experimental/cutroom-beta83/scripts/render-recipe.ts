import { assertAudioMixPolicy, OUTPUT_AUDIO_RATE } from "../src/audio-mix-policy";
/** Validate and project a frozen RenderRecipe for the existing ad FFmpeg engine. */
import { createHash } from "node:crypto";

interface AdRecipeShot {
  index: number; shot_id: string | null; asset_id: string | null;
  segment_id: string | null; media_kind: "video" | "image"; src: string;
  output_start_us: number; output_end_us: number;
  source_start_us?: number; source_end_us?: number;
  source_window_explicit?: boolean; highlight_terms?: string[];
}
interface AdRecipeCue {
  index: number; text: string; start_us: number; end_us: number;
  highlight_terms?: string[];
}
interface AdRecipeInstance {
  instance_id: string; kind: string; start_us: number; end_us: number;
  render_value: Record<string, unknown>;
}
interface AdRecipe {
  audio_policy: unknown;
  recipe_version: number;
  task_snapshot: { task_id: string; task_type: string; aspect_ratio: string };
  duration_us: number;
  output: { width: number; height: number; fps_num: number; fps_den: number;
    pixel_format: string; audio_rate: number; crf?: number };
  shots: AdRecipeShot[]; subtitle_cues: AdRecipeCue[];
  display_cues?: Array<{text: string; display_text: string; start_us: number; end_us: number}>;
  main_audio: { src: string; preset_id?: string | null };
  subtitle_style: Record<string, unknown>; subtitle_highlight: boolean;
  subtitle_appearance?: import("../src/subtitle-appearance").SubtitleAppearance;
  packaging_instances: AdRecipeInstance[];
  transitions: Array<{ kind: string; duration_us: number }>;
}

const validUs = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

/** Legacy hand-authored ad plans remain readable; workbench plans use recipe bytes. */
export function resolveAdRecipePayload(envelope: any): any {
  if (envelope.renderRecipeJson === undefined) return envelope;
  if (typeof envelope.renderRecipeJson !== "string" ||
      typeof envelope.recipe_digest !== "string") {
    throw new Error("RECIPE_INVALID: 缺少冻结配方正文或摘要");
  }
  const actualDigest = createHash("sha256")
    .update(envelope.renderRecipeJson, "utf8").digest("hex");
  if (actualDigest !== envelope.recipe_digest) {
    throw new Error("RECIPE_DIGEST_MISMATCH: 渲染配方内容已变化");
  }
  let recipe: AdRecipe;
  try { recipe = JSON.parse(envelope.renderRecipeJson) as AdRecipe; }
  catch { throw new Error("RECIPE_INVALID: 冻结配方 JSON 无效"); }
  if (recipe.recipe_version !== 1 || recipe.task_snapshot?.task_type !== "ad_mix" ||
      !["9:16", "16:9"].includes(recipe.task_snapshot?.aspect_ratio) ||
      !validUs(recipe.duration_us) || recipe.duration_us === 0 ||
      !Array.isArray(recipe.shots) || recipe.shots.length === 0 ||
      !Array.isArray(recipe.subtitle_cues) ||
      recipe.subtitle_cues.length === 0 ||
      !Array.isArray(recipe.packaging_instances) ||
      !Array.isArray(recipe.transitions)) {
    throw new Error("RECIPE_INVALID: 广告配方结构无效");
  }
  const expectedSize = recipe.task_snapshot.aspect_ratio === "9:16"
    ? [1080, 1920] : [1920, 1080];
  assertAudioMixPolicy(recipe.audio_policy);
  if (recipe.output?.width !== expectedSize[0] ||
      recipe.output?.height !== expectedSize[1] ||
      recipe.output?.fps_num !== 30 || recipe.output?.fps_den !== 1 ||
      recipe.output?.pixel_format !== "yuv420p" ||
      recipe.output?.audio_rate !== OUTPUT_AUDIO_RATE) {
    throw new Error("RECIPE_OUTPUT_UNSUPPORTED: 当前广告渲染器输出配置不匹配");
  }
  let cueCursor = 0;
  for (const [index, cue] of recipe.subtitle_cues.entries()) {
    if (cue.index !== index || typeof cue.text !== "string" ||
        !validUs(cue.start_us) || !validUs(cue.end_us) ||
        cue.start_us !== cueCursor || cue.end_us <= cueCursor || cue.end_us > recipe.duration_us) {
      throw new Error("RECIPE_CUE_WINDOW_MISMATCH: 字幕配音时间轴不连续");
    }
    cueCursor = cue.end_us;
  }
  if (cueCursor !== recipe.duration_us) throw new Error("RECIPE_CUE_WINDOW_MISMATCH: 字幕未覆盖配音");
  if (recipe.display_cues !== undefined) {
    if (!Array.isArray(recipe.display_cues)) throw new Error("RECIPE_DISPLAY_CUE_INVALID: 字幕格式无效");
    let end = 0;
    for (const cue of recipe.display_cues) {
      if (typeof cue.text !== 'string' || typeof cue.display_text !== 'string' ||
          !validUs(cue.start_us) || !validUs(cue.end_us) || cue.start_us < end ||
          cue.end_us <= cue.start_us || cue.end_us > recipe.duration_us)
        throw new Error("RECIPE_DISPLAY_CUE_INVALID: 字幕时间区间无效或重叠");
      end = cue.end_us;
    }
  }
  const segments: Array<Record<string, unknown>> = [];
  let cursor = 0;
  for (let index = 0; index < recipe.shots.length; index++) {
    const shot = recipe.shots[index];
    const matches = recipe.subtitle_cues.filter(cue => cue.start_us <= shot.output_start_us && shot.output_end_us <= cue.end_us);
    if (matches.length !== 1) throw new Error("RECIPE_CUE_WINDOW_MISMATCH: 镜头无法对应唯一配音段");
    const cue = matches[0];
    if (shot.index !== index ||
        !validUs(shot.output_start_us) || !validUs(shot.output_end_us) ||
        shot.output_start_us !== cursor || shot.output_end_us <= cursor ||
        shot.output_end_us > recipe.duration_us) {
      throw new Error("RECIPE_CUE_WINDOW_MISMATCH: 字幕与镜头时间轴不一致");
    }
    if (typeof shot.src !== "string" || !shot.src || typeof cue.text !== "string") {
      throw new Error("RECIPE_INVALID: 镜头素材或字幕正文缺失");
    }
    const segment: Record<string, unknown> = {
      i: shot.index, cardId: shot.shot_id, assetId: shot.asset_id,
      mediaKind: shot.media_kind, text: cue.text,
      start: shot.output_start_us / 1_000_000,
      end: shot.output_end_us / 1_000_000,
      dur: (shot.output_end_us - shot.output_start_us) / 1_000_000,
      src: shot.src, matched: true, segmentId: shot.segment_id,
    };
    if (shot.media_kind === "image") {
      if (shot.source_start_us !== undefined || shot.source_end_us !== undefined) {
        throw new Error("IMAGE_HAS_SOURCE_WINDOW: 图片不能携带视频入出点");
      }
    } else if (shot.media_kind === "video") {
      if (!validUs(shot.source_start_us) || !validUs(shot.source_end_us) ||
          shot.source_end_us - shot.source_start_us < shot.output_end_us - shot.output_start_us) {
        throw new Error("SOURCE_WINDOW_TOO_SHORT: 视频源窗口不足");
      }
      if (shot.source_window_explicit) {
        segment.sourceStart = shot.source_start_us / 1_000_000;
        segment.sourceEnd = shot.source_end_us / 1_000_000;
      }
    } else {
      throw new Error("RECIPE_MEDIA_KIND_INVALID: 镜头媒体类型无效");
    }
    if (cue.highlight_terms?.length) segment.highlightTerms = cue.highlight_terms;
    segments.push(segment);
    cursor = shot.output_end_us;
  }
  if (cursor !== recipe.duration_us ||
      recipe.transitions.length !== Math.max(0, recipe.shots.length - 1) ||
      recipe.transitions.some((item) => item.kind !== "fade" || item.duration_us !== 400000)) {
    throw new Error("RECIPE_TIMELINE_MISMATCH: 主时间轴或转场与渲染器不一致");
  }
  if (typeof recipe.main_audio?.src !== "string" || !recipe.main_audio.src) {
    throw new Error("RECIPE_MAIN_AUDIO_MISSING: 配方缺少主配音");
  }
  const result: any = {
    name: recipe.task_snapshot.task_id,
    ratio: recipe.task_snapshot.aspect_ratio,
    total: recipe.duration_us / 1_000_000,
    voice: recipe.main_audio.preset_id
      ? { src: recipe.main_audio.src, presetId: recipe.main_audio.preset_id }
      : recipe.main_audio.src,
    segments,
    subtitleSegments: recipe.display_cues ? recipe.display_cues.map(cue => ({text: cue.text, displayText: cue.display_text,
      start: cue.start_us / 1_000_000, end: cue.end_us / 1_000_000})) : recipe.subtitle_cues.map(cue => ({i: cue.index, text: cue.text,
      start: cue.start_us / 1_000_000, end: cue.end_us / 1_000_000,
      highlightTerms: cue.highlight_terms ?? []})),
    subtitlePreset: recipe.subtitle_style,
    subtitleHighlight: recipe.subtitle_highlight,
      subtitleAppearance: recipe.subtitle_appearance,
    output: envelope.output ?? {
      width: recipe.output.width, height: recipe.output.height,
      crf: recipe.output.crf ?? 23,
    },
  };
  for (const instance of recipe.packaging_instances) {
    if (!validUs(instance.start_us) || !validUs(instance.end_us) ||
        instance.start_us >= instance.end_us || instance.end_us > recipe.duration_us ||
        !instance.render_value || typeof instance.render_value !== "object") {
      throw new Error("RECIPE_PACKAGING_INVALID: 包装实例时间或内容无效");
    }
    if (instance.kind === "bgm" || instance.kind === "watermark") {
      if (result[instance.kind]) throw new Error("RECIPE_PACKAGING_DUPLICATE: 单实例包装层重复");
      result[instance.kind] = instance.render_value;
    } else if (["sfx", "titles", "stickers", "fancyTexts"].includes(instance.kind)) {
      (result[instance.kind] ??= []).push(instance.render_value);
    } else {
      throw new Error("RECIPE_PACKAGING_INVALID: 未知包装层");
    }
  }
  return result;
}
