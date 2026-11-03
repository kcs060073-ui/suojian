export interface ScriptUnit { id: number; text: string }
type SentenceSegmenter = new (locale: string, options: { granularity: "sentence" }) => {
  segment(input: string): Iterable<{ segment: string }>;
};

export function buildScriptUnits(script: string): ScriptUnit[] {
  if (!script.trim()) throw new Error("SCRIPT_UNITS_EMPTY: 原稿不可为空");
  const Segmenter = (Intl as unknown as { Segmenter?: SentenceSegmenter }).Segmenter;
  if (!Segmenter) throw new Error("SCRIPT_SEGMENTER_UNAVAILABLE: 需要 Intl.Segmenter");
  const units: ScriptUnit[] = [];
  let leadingWhitespace = "";
  for (const { segment } of new Segmenter("zh", { granularity: "sentence" }).segment(script)) {
    if (!segment.trim()) {
      if (units.length) units[units.length - 1].text += segment;
      else leadingWhitespace += segment;
    } else {
      units.push({ id: units.length + 1, text: leadingWhitespace + segment });
      leadingWhitespace = "";
    }
  }
  if (!units.length || units.map((unit) => unit.text).join("") !== script) {
    throw new Error("SCRIPT_UNITS_INVALID: 原稿单元必须完整保留原文");
  }
  return units;
}

export function materializeScriptGroups(units: ScriptUnit[], groups: unknown): string[] {
  const invalid = () => new Error("SCRIPT_SEGMENT_GROUPS_INVALID: 分组须按序完整覆盖所有原文单元");
  if (!units.length || units.some((unit, index) => unit.id !== index + 1
    || typeof unit.text !== "string" || !unit.text.trim())) throw invalid();
  if (!Array.isArray(groups) || !groups.length) throw invalid();
  const segments: string[] = [];
  let expectedId = 1;
  for (const group of groups) {
    if (!Array.isArray(group) || !group.length) throw invalid();
    let text = "";
    for (const id of group) {
      if (!Number.isSafeInteger(id) || id !== expectedId || id > units.length) throw invalid();
      text += units[expectedId - 1].text;
      expectedId += 1;
    }
    segments.push(text);
  }
  if (expectedId !== units.length + 1) throw invalid();
  return segments;
}

// A planning unit is a clause, not a complete punctuation sentence. Keep the
// source immutable; the model may combine short clauses but cannot rewrite them.
export const MAX_STORYBOARD_PHRASE_CHARS = 40;
const phraseLength = (text: string) => Array.from(text.replace(/\s/g, "")).length;

export function buildStoryboardUnits(script: string): ScriptUnit[] {
  const sentences = buildScriptUnits(script);
  const parts: string[] = [];
  for (const sentence of sentences) {
    let start = 0;
    for (let index = 0; index < sentence.text.length; index++) {
      const char = sentence.text[index];
      if (!/[，,；;：:]/.test(char)) continue;
      // Preserve times and numbers such as 08:30 and 16,000.
      if (/[,：:]/.test(char) && /[0-9０-９]/.test(sentence.text[index - 1] || "")
          && /[0-9０-９]/.test(sentence.text[index + 1] || "")) continue;
      parts.push(sentence.text.slice(start, index + 1));
      start = index + 1;
    }
    const tail = sentence.text.slice(start);
    if (tail.trim()) parts.push(tail);
    else if (tail && parts.length) parts[parts.length - 1] += tail;
  }
  if (parts.some(text => phraseLength(text) > MAX_STORYBOARD_PHRASE_CHARS)) {
    throw new Error("STORYBOARD_CLAUSE_TOO_LONG: 存在无法按当前标点拆分的长句，需先形成保留原文的语义短句，不能直接生成长镜头");
  }
  if (parts.join("") !== script) throw new Error("SCRIPT_UNITS_INVALID: 短句单元必须完整保留原文");
  return parts.map((text, index) => ({ id: index + 1, text }));
}

export function validateStoryboardPhrases<T extends { text: string }>(segments: readonly T[]): void {
  if (segments.some(segment => phraseLength(segment.text) > MAX_STORYBOARD_PHRASE_CHARS)) {
    throw new Error("STORYBOARD_PHRASE_TOO_LONG: 分镜仍包含超过40字的长段，请按语义拆成便于选材的短句；未发起配音");
  }
}
