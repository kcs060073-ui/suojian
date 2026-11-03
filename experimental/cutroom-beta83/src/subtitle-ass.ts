import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { alignScriptToWords, AlignmentError, TimedWord } from "./subtitle-alignment";

export interface SubtitleCue {
  text: string;
  start: number;
  end: number;
  style?: "Default" | "Punch";
}

export interface SubtitleMetadata {
  schema: 1;
  preset: string;
  coverage: number;
  speed: number;
  lead: number;
  cues: SubtitleCue[];
  assSha256: string;
  highlightEnabled?: boolean;
}

export interface BuildAssDocumentInput {
  scriptText: string;
  words: TimedWord[];
  preset?: string;
  speed?: number;
  lead?: number;
  duration?: number;
  keywords?: string[];
  punchLines?: string[];
  highlightEnabled?: boolean;
}

interface SubtitlePreset {
  id: string;
  fontname_guess: string;
  size: number;
  punch_size?: number;
  primary_hex: string;
  highlight_hex?: string;
  outline_hex: string;
  shadow_hex: string;
  bold: boolean;
  alignment: number;
  margin_v: number;
  outline: number;
  shadow: number;
  spacing?: number;
  punch_spacing?: number;
  punch_outline?: number;
  punch_shadow?: number;
  back_alpha_hex?: string;
  border_style: number;
  max_chars_per_line?: number;
  max_lines?: number;
  fade_in_ms?: number;
  fade_out_ms?: number;
  tail_pad_seconds?: number;
  license: string;
}

export class SubtitleAssError extends Error {
  constructor(public readonly code: string, message: string) {
    super(`${code}: ${message}`);
    this.name = "SubtitleAssError";
  }
}

const ROOT = path.resolve(__dirname, "..");
const PRESET_PATH = path.join(ROOT, "assets", "manifests", "subtitle-presets.json");
const CANVAS_W = 1080;
const CANVAS_H = 1920;
const MARGIN_LR = 40;
const DEFAULT_PRESET = "sub-modern-clean-v1";
const SOFT_BREAK = /[，、；：,;:]/u;
const HARD_BREAK = /[。！？!?\r\n]/u;
const SPEAKABLE = /[\p{L}\p{N}%]/u;

function assColor(hex: string, alpha = "00"): string {
  const normalized = hex.replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(normalized)) throw new SubtitleAssError("INVALID_PRESET", `invalid colour ${hex}`);
  if (!/^[0-9a-fA-F]{2}$/.test(alpha)) throw new SubtitleAssError("INVALID_PRESET", `invalid alpha ${alpha}`);
  return `&H${alpha.toUpperCase()}${normalized.slice(4, 6)}${normalized.slice(2, 4)}${normalized.slice(0, 2)}&`;
}

function loadPreset(id: string): SubtitlePreset {
  const manifest = JSON.parse(fs.readFileSync(PRESET_PATH, "utf8")) as { items?: SubtitlePreset[] };
  const preset = manifest.items?.find((item) => item.id === id);
  if (!preset) throw new SubtitleAssError("PRESET_NOT_FOUND", `subtitle preset not found: ${id}`);
  if (preset.license !== "commercial-approved") throw new SubtitleAssError("INVALID_PRESET", `subtitle preset is not commercial-approved: ${id}`);
  return preset;
}

interface TextCue { rawText: string; scriptIndices: number[]; startIndex: number; endIndex: number; punch: boolean; }

const NUMERIC_TOKEN = /[0-9０-９]+(?:[.．][0-9０-９]+)?(?:多)?(?:亿|万|千|百)?(?:平方米|平米|千瓦时|千瓦|兆瓦|公斤|千克|公里|千米|万元|亿元|元|块|毛|角|吨|克|平|亩|瓦|度|个|家|户|人|倍|分|米|%|[PpGgMmTt])?/gu;

interface ProtectedSpan { start: number; end: number; }

export function protectedSpans(text: string, keywords: string[]): ProtectedSpan[] {
  const candidates: ProtectedSpan[] = [];
  const numeric = new RegExp(NUMERIC_TOKEN.source, "gu");
  for (let match = numeric.exec(text); match; match = numeric.exec(text)) {
    candidates.push({ start: match.index, end: match.index + match[0].length });
  }
  for (const keyword of [...new Set(keywords.filter((value) => value.length > 0))]) {
    const escaped = keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(escaped, "gu");
    for (let match = pattern.exec(text); match; match = pattern.exec(text)) {
      candidates.push({ start: match.index, end: match.index + match[0].length });
    }
  }
  candidates.sort((left, right) => left.start - right.start || (right.end - right.start) - (left.end - left.start));
  const selected: ProtectedSpan[] = [];
  let cursor = 0;
  for (const candidate of candidates) {
    if (candidate.start < cursor) continue;
    selected.push(candidate);
    cursor = candidate.end;
  }
  return selected;
}

function cueText(text: string, maxChars: number, maxLines: number, keywords: string[]): string {
  const normalized = text.trim();
  const glyphs = Array.from(normalized);
  const trailing = glyphs[glyphs.length - 1];
  const trailingBoundaryPunctuation = trailing !== undefined
    && (SOFT_BREAK.test(trailing) || HARD_BREAK.test(trailing));
  const accountedGlyphs = glyphs.length - (trailingBoundaryPunctuation ? 1 : 0);
  const maxTotal = maxChars * maxLines;
  if (accountedGlyphs <= maxChars || maxLines === 1) return glyphs.join("");
  if (accountedGlyphs > maxTotal) throw new SubtitleAssError("CUE_OUT_OF_RANGE", `cue exceeds ${maxLines} lines of ${maxChars} characters`);

  const spans = protectedSpans(normalized, keywords);
  const candidates = Array.from({ length: glyphs.length - 1 }, (_, index) => index + 1)
    .filter((index) => index <= maxChars && accountedGlyphs - index <= maxChars)
    .filter((index) => {
      const boundary = glyphs.slice(0, index).join("").length;
      return !spans.some((span) => span.start < boundary && boundary < span.end);
    });
  if (!candidates.length) throw new SubtitleAssError("CUE_OUT_OF_RANGE", "cue cannot wrap without splitting a protected token");
  const punctuation = candidates.filter((index) => SOFT_BREAK.test(glyphs[index - 1]));
  const breakAt = (punctuation.length ? punctuation : candidates).reduce((best, index) => index > best ? index : best);
  return `${glyphs.slice(0, breakAt).join("")}\\N${glyphs.slice(breakAt).join("")}`;
}

export function escapeAssText(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/\{/g, "\\{").replace(/\}/g, "\\}");
}

interface HighlightToken { start: number; end: number; }

function highlightedSegment(text: string, keywords: string[], orange: string, white: string): string {
  const spans = protectedSpans(text, keywords);
  let cursor = 0;
  let output = "";
  for (const span of spans) {
    output += escapeAssText(text.slice(cursor, span.start));
    output += `{\\c${orange}}${escapeAssText(text.slice(span.start, span.end))}{\\c${white}}`;
    cursor = span.end;
  }
  return output + escapeAssText(text.slice(cursor));
}

function highlightedAssText(text: string, keywords: string[], orange: string, white: string): string {
  return text.split("\\N").map((line) => highlightedSegment(line, keywords, orange, white)).join("\\N");
}

const SURROUNDING_PUNCH_PUNCTUATION = /^[\s，。、；：？！!?…“”"'（）()《》【】\-—~·.]+|[\s，。、；：？！!?…“”"'（）()《》【】\-—~·.]+$/gu;

function normalizedPunchText(value: string): string {
  return value.replace(SURROUNDING_PUNCH_PUNCTUATION, "");
}

function punchRanges(scriptText: string, punchLines: string[]): ProtectedSpan[] {
  const normalized = new Set(punchLines.map(normalizedPunchText).filter(Boolean));
  if (!normalized.size) return [];
  const ranges: ProtectedSpan[] = [];
  let start = 0;
  for (let index = 0; index < scriptText.length;) {
    const point = scriptText.codePointAt(index);
    if (point === undefined) break;
    const character = String.fromCodePoint(point);
    const end = index + character.length;
    if (HARD_BREAK.test(character)) {
      if (normalized.has(normalizedPunchText(scriptText.slice(start, end)))) ranges.push({ start, end });
      start = end;
    }
    index = end;
  }
  if (start < scriptText.length && normalized.has(normalizedPunchText(scriptText.slice(start)))) {
    ranges.push({ start, end: scriptText.length });
  }
  return ranges;
}

function punchCueText(text: string, maxChars: number, keywords: string[]): string {
  const normalized = text.trim();
  const glyphs = Array.from(normalized);
  if (glyphs.length <= maxChars) return normalized;
  const spans = protectedSpans(normalized, keywords);
  const candidates = Array.from({ length: glyphs.length - 1 }, (_, index) => index + 1)
    .filter((index) => {
      const boundary = glyphs.slice(0, index).join("").length;
      return !spans.some((span) => span.start < boundary && boundary < span.end);
    });
  if (!candidates.length) return normalized;
  const punctuation = candidates.filter((index) => SOFT_BREAK.test(glyphs[index - 1]));
  const preferred = punctuation.length ? punctuation : candidates;
  const midpoint = glyphs.length / 2;
  const breakAt = preferred.reduce((best, index) => {
    const distance = Math.abs(index - midpoint);
    const bestDistance = Math.abs(best - midpoint);
    return distance < bestDistance || (distance === bestDistance && index < best) ? index : best;
  });
  return `${glyphs.slice(0, breakAt).join("")}\\N${glyphs.slice(breakAt).join("")}`;
}

function segmentScript(scriptText: string, maxChars: number, maxLines: number, keywords: string[], punchLines: string[]): TextCue[] {
  const cues: TextCue[] = [];
  const maxTotal = maxChars * maxLines;
  const spans = protectedSpans(scriptText, keywords);
  const safeBoundary = (index: number) => !spans.some((span) => span.start < index && index < span.end);
  const punches = punchRanges(scriptText, punchLines);
  const pushRange = (startIndex: number, endIndex: number, punch = false) => {
    const indices: number[] = [];
    for (let index = startIndex; index < endIndex;) {
      const point = scriptText.codePointAt(index);
      if (point === undefined) break;
      const character = String.fromCodePoint(point);
      if (SPEAKABLE.test(character)) indices.push(index);
      index += character.length;
    }
    if (indices.length) {
      cues.push({ rawText: scriptText.slice(startIndex, endIndex), scriptIndices: indices, startIndex, endIndex, punch });
    }
  };

  let startIndex = 0;
  while (startIndex < scriptText.length) {
    while (startIndex < scriptText.length) {
      const point = scriptText.codePointAt(startIndex);
      if (point === undefined) break;
      const character = String.fromCodePoint(point);
      if (!/\s/u.test(character)) break;
      startIndex += character.length;
    }
    if (startIndex >= scriptText.length) break;

    const punch = punches.find((range) => range.start === startIndex);
    if (punch) {
      pushRange(punch.start, punch.end, true);
      startIndex = punch.end;
      continue;
    }
    let index = startIndex;
    let displayGlyphs = 0;
    let latestSafe: number | undefined;
    let emitted = false;
    while (index < scriptText.length) {
      const point = scriptText.codePointAt(index);
      if (point === undefined) break;
      const character = String.fromCodePoint(point);
      const nextIndex = index + character.length;
      if (!/\s/u.test(character)) displayGlyphs += 1;
      const boundaryIsSafe = safeBoundary(nextIndex);
      if (displayGlyphs > maxTotal) {
        if (boundaryIsSafe && (HARD_BREAK.test(character) || SOFT_BREAK.test(character))) {
          pushRange(startIndex, nextIndex);
          startIndex = nextIndex;
          emitted = true;
          break;
        }
        if (latestSafe !== undefined && latestSafe > startIndex) {
          pushRange(startIndex, latestSafe);
          startIndex = latestSafe;
          emitted = true;
          break;
        }
        if (boundaryIsSafe) {
          pushRange(startIndex, nextIndex);
          startIndex = nextIndex;
          emitted = true;
          break;
        }
      }
      if (boundaryIsSafe && displayGlyphs > 0) {
        latestSafe = nextIndex;
        if (HARD_BREAK.test(character) || SOFT_BREAK.test(character)) {
          pushRange(startIndex, nextIndex);
          startIndex = nextIndex;
          emitted = true;
          break;
        }
      }
      index = nextIndex;
    }
    if (emitted) continue;
    if (index >= scriptText.length) {
      pushRange(startIndex, scriptText.length);
      break;
    }
  }
  return cues;
}
function splitTemporalGaps(cue: TextCue, timestamps: Map<number, { start: number; end: number }>): TextCue[] {
  if (cue.punch) return [cue];
  const pieces: TextCue[] = [];
  let startOffset = 0;
  let firstSpoken = 0;
  for (let index = 1; index < cue.scriptIndices.length; index += 1) {
    const previous = timestamps.get(cue.scriptIndices[index - 1]);
    const current = timestamps.get(cue.scriptIndices[index]);
    if (!previous || !current) continue;
    if (current.start - previous.end > 0.5) {
      const splitAt = cue.scriptIndices[index] - cue.startIndex;
      pieces.push({
        rawText: cue.rawText.slice(startOffset, splitAt),
        scriptIndices: cue.scriptIndices.slice(firstSpoken, index),
        startIndex: cue.startIndex + startOffset,
        endIndex: cue.startIndex + splitAt,
        punch: false,
      });
      startOffset = splitAt;
      firstSpoken = index;
    }
  }
  pieces.push({
    rawText: cue.rawText.slice(startOffset),
    scriptIndices: cue.scriptIndices.slice(firstSpoken),
    startIndex: cue.startIndex + startOffset,
    endIndex: cue.endIndex,
    punch: false,
  });
  return pieces;
}

function fmtTime(value: number): string {
  const centiseconds = Math.max(0, Math.round(value * 100));
  const hours = Math.floor(centiseconds / 360000);
  const minutes = Math.floor((centiseconds % 360000) / 6000);
  const seconds = Math.floor((centiseconds % 6000) / 100);
  return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(centiseconds % 100).padStart(2, "0")}`;
}

function styleLine(name: string, size: number, preset: SubtitlePreset): string {
  const punch = name === "Punch";
  const spacing = punch ? (preset.punch_spacing ?? preset.spacing ?? 0) : (preset.spacing ?? 0);
  const outline = punch ? (preset.punch_outline ?? preset.outline) : preset.outline;
  const shadow = punch ? (preset.punch_shadow ?? preset.shadow) : preset.shadow;
  return [
    `Style: ${name}`,
    preset.fontname_guess,
    size,
    assColor(preset.primary_hex),
    assColor(preset.highlight_hex ?? "#FD9437"),
    assColor(preset.outline_hex),
    assColor(preset.shadow_hex, preset.back_alpha_hex ?? "00"),
    preset.bold ? "1" : "0", "0", "0", "0", "100", "100", String(spacing), "0",
    String(preset.border_style), String(outline), String(shadow), String(preset.alignment),
    String(MARGIN_LR), String(MARGIN_LR), String(preset.margin_v), "1",
  ].join(",");
}

function stableError(error: unknown): never {
  if (error instanceof SubtitleAssError) throw error;
  if (error instanceof AlignmentError && error.code === "EMPTY_SCRIPT") {
    throw new SubtitleAssError("CUE_ANCHOR_MISSING", error.message);
  }
  if (error instanceof AlignmentError && error.code === "TIMELINE_REGRESSION") {
    throw new SubtitleAssError("TIMELINE_REGRESSION", error.message);
  }
  throw error;
}

export function buildAssDocument(input: BuildAssDocumentInput): { ass: string; metadata: SubtitleMetadata } {
  const presetId = input.preset ?? DEFAULT_PRESET;
  const preset = loadPreset(presetId);
  const speed = input.speed ?? 1.25;
  const lead = input.lead ?? 0.08;
  const maxChars = preset.max_chars_per_line ?? 12;
  const maxLines = preset.max_lines ?? 2;
  const tailPad = preset.tail_pad_seconds ?? 0.12;
  const keywords = input.keywords ?? [];
  const punchLines = input.punchLines ?? [];
  if (!Number.isFinite(input.duration ?? 0) || (input.duration !== undefined && input.duration <= 0)) {
    throw new SubtitleAssError("CUE_OUT_OF_RANGE", "duration must be a positive finite number");
  }
  if (!Number.isFinite(tailPad) || tailPad < 0 || tailPad > 0.12) {
    throw new SubtitleAssError("INVALID_PRESET", "tail pad must be between 0 and 0.12 seconds");
  }

  let alignment;
  try {
    alignment = alignScriptToWords(input.scriptText, input.words, speed, lead);
  } catch (error) {
    return stableError(error);
  }
  const byIndex = new Map(alignment.chars.map((character) => [character.scriptIndex, character]));
  const duration = input.duration ?? Math.max(...alignment.chars.map((character) => character.end + tailPad));
  const cues = segmentScript(input.scriptText, maxChars, maxLines, keywords, punchLines)
    .flatMap((cue) => splitTemporalGaps(cue, byIndex))
    .map(({ rawText, scriptIndices, punch }) => {
      const text = punch
        ? punchCueText(rawText, maxChars, keywords)
        : cueText(rawText, maxChars, maxLines, keywords);
      const anchored = scriptIndices.map((index) => byIndex.get(index));
      if (anchored.length === 0 || anchored.some((character) => character === undefined) || !anchored.some((character) => character !== undefined && character.confidence > 0)) {
        throw new SubtitleAssError("CUE_ANCHOR_MISSING", `cue has no complete alignment anchor: ${text}`);
      }
      const first = anchored[0]!;
      const last = anchored[anchored.length - 1]!;
      if (first.start > last.end) throw new SubtitleAssError("TIMELINE_REGRESSION", `cue timestamps regress: ${text}`);
      if (first.start >= duration || last.end < 0 || last.end > duration) {
        throw new SubtitleAssError("CUE_OUT_OF_RANGE", `cue is outside duration: ${text}`);
      }
      return {
        text,
        start: Math.max(0, first.start),
        end: Math.min(duration, last.end + tailPad),
        style: punch ? "Punch" as const : "Default" as const,
      };
    });

  for (let index = 1; index < cues.length; index += 1) {
    const previous = cues[index - 1];
    const current = cues[index];
    if (current.start < previous.start) throw new SubtitleAssError("TIMELINE_REGRESSION", "cue starts regress");
    if (current.start < previous.end) {
      const midpoint = (current.start + previous.end) / 2;
      previous.end = midpoint;
      current.start = midpoint;
    }
    if (current.end < current.start) throw new SubtitleAssError("TIMELINE_REGRESSION", "cue ends before its start");
  }

  const header = `[Script Info]\nScriptType: v4.00+\nPlayResX: ${CANVAS_W}\nPlayResY: ${CANVAS_H}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n${styleLine("Default", preset.size, preset)}\n${styleLine("Punch", preset.punch_size ?? preset.size + 10, preset)}\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n`;
  const fade = `{\\fad(${preset.fade_in_ms ?? 60},${preset.fade_out_ms ?? 80})}`;
  const orange = assColor(preset.highlight_hex ?? "#FD9437");
  const white = assColor(preset.primary_hex);
  const body = cues.map((cue) => {
    const eventText = input.highlightEnabled === false
      ? cue.text : highlightedAssText(cue.text, keywords, orange, white);
    if (/\r|\n/u.test(eventText)) {
      throw new SubtitleAssError("CUE_OUT_OF_RANGE", "event text contains a raw newline");
    }
    return `Dialogue: 0,${fmtTime(cue.start)},${fmtTime(cue.end)},${cue.style ?? "Default"},,0,0,0,,${fade}${eventText}`;
  }).join("\n");
  const ass = `${header}${body}\n`;
  const metadata: SubtitleMetadata = {
    schema: 1,
    preset: presetId,
    coverage: alignment.coverage,
    speed,
    lead,
    cues,
    assSha256: createHash("sha256").update(ass, "utf8").digest("hex"),
    ...(input.highlightEnabled === false ? { highlightEnabled: false } : {}),
  };
  return { ass, metadata };
}
