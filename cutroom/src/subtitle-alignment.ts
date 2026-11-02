import OpenCC from "opencc-js";

export interface TimedWord {
  w: string;
  start: number;
  end: number;
}

export interface AlignedChar {
  scriptIndex: number;
  start: number;
  end: number;
  confidence: number;
}

export interface AlignmentResult {
  chars: AlignedChar[];
  coverage: number;
}

export class AlignmentError extends Error {
  constructor(public readonly code: string, message: string) {
    super(`${code}: ${message}`);
    this.name = "AlignmentError";
  }
}

interface NormalizedScriptChar {
  value: string;
  scriptIndex: number;
  spanStart?: number;
  spanEnd?: number;
}

interface TimedNormalizedChar {
  value: string;
  start: number;
  end: number;
}

interface CharacterTiming {
  start: number;
  end: number;
  confidence: number;
}

const toSimplified = OpenCC.Converter({ from: "tw", to: "cn" });
const spokenCharacter = /[\p{L}\p{N}%]/u;

function normalizeCharacter(character: string): string {
  return toSimplified(character).normalize("NFKC").toUpperCase();
}

const chineseDigits: Record<string, number> = {
  零: 0, 〇: 0, 一: 1, 二: 2, 三: 3, 四: 4,
  五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 两: 2,
};
const chineseUnits: Record<string, number> = { 十: 10, 百: 100, 千: 1000, 万: 10000 };

function chineseNumberToArabic(value: string): number {
  let total = 0;
  let section = 0;
  let digit = 0;
  for (const character of value) {
    if (character in chineseDigits) {
      digit = chineseDigits[character];
    } else {
      const unit = chineseUnits[character];
      if (unit === undefined) return Number.NaN;
      if (unit >= 10000) {
        total += (section + digit) * unit;
        section = 0;
        digit = 0;
      } else {
        section += (digit || 1) * unit;
        digit = 0;
      }
    }
  }
  return total + section + digit;
}

function normalizeWord(word: string): string {
  const simplified = toSimplified(word).normalize("NFKC").toUpperCase();
  return simplified.replace(/百分之([零〇一二三四五六七八九十百千万两]+)/g, (_, numberText: string) => {
    const number = chineseNumberToArabic(numberText);
    return Number.isNaN(number) ? _ : `${number}%`;
  });
}

function normalizeScript(scriptText: string): NormalizedScriptChar[] {
  const result: NormalizedScriptChar[] = [];
  let scriptIndex = 0;
  while (scriptIndex < scriptText.length) {
    const percentage = scriptText.slice(scriptIndex).match(/^百分之([零〇一二三四五六七八九十百千万兩两]+)/);
    if (percentage !== null) {
      const percentageText = percentage[0];
      for (const value of normalizeWord(percentageText)) {
        if (spokenCharacter.test(value)) result.push({ value, scriptIndex, spanStart: scriptIndex, spanEnd: scriptIndex + percentageText.length });
      }
      scriptIndex += percentageText.length;
      continue;
    }

    const codePoint = scriptText.codePointAt(scriptIndex);
    if (codePoint === undefined) break;
    const character = String.fromCodePoint(codePoint);
    if (spokenCharacter.test(character)) {
      for (const value of normalizeCharacter(character)) {
        if (spokenCharacter.test(value)) result.push({ value, scriptIndex });
      }
    }
    scriptIndex += character.length;
  }
  return result;
}

function validateInputs(words: TimedWord[], speed: number, leadSeconds: number): void {
  if (words.length === 0) {
    throw new AlignmentError("EMPTY_WORDS", "Whisper word sequence is empty");
  }
  if (!Number.isFinite(speed) || speed <= 0) {
    throw new AlignmentError("INVALID_SPEED", "speed must be a positive finite number");
  }
  if (!Number.isFinite(leadSeconds)) {
    throw new AlignmentError("INVALID_LEAD", "leadSeconds must be finite");
  }

  let previousEnd = -Infinity;
  for (const word of words) {
    if (!Number.isFinite(word.start) || !Number.isFinite(word.end) || word.start < 0 || word.end < word.start) {
      throw new AlignmentError("INVALID_WORD_TIMING", "Whisper word has invalid timestamps");
    }
    if (word.start < previousEnd) {
      throw new AlignmentError("TIMELINE_REGRESSION", "Whisper word timestamps regress");
    }
    previousEnd = word.end;
  }
}

function normalizeWords(words: TimedWord[]): TimedNormalizedChar[] {
  const result: TimedNormalizedChar[] = [];
  for (const word of words) {
    const values = Array.from(normalizeWord(word.w))
      .filter((character) => spokenCharacter.test(character));
    if (values.length === 0) continue;

    const duration = (word.end - word.start) / values.length;
    values.forEach((value, index) => {
      result.push({
        value,
        start: word.start + duration * index,
        end: word.start + duration * (index + 1),
      });
    });
  }
  if (result.length === 0) {
    throw new AlignmentError("EMPTY_WORDS", "Whisper word sequence has no spoken characters");
  }
  return result;
}

function alignNeedlemanWunsch(script: NormalizedScriptChar[], source: TimedNormalizedChar[]): Array<number | undefined> {
  const costs = Array.from(
    { length: script.length + 1 },
    () => Array<number>(source.length + 1).fill(0),
  );
  for (let scriptIndex = 1; scriptIndex <= script.length; scriptIndex += 1) costs[scriptIndex][0] = scriptIndex;
  for (let sourceIndex = 1; sourceIndex <= source.length; sourceIndex += 1) costs[0][sourceIndex] = sourceIndex;

  for (let scriptIndex = 1; scriptIndex <= script.length; scriptIndex += 1) {
    for (let sourceIndex = 1; sourceIndex <= source.length; sourceIndex += 1) {
      const substitution = costs[scriptIndex - 1][sourceIndex - 1]
        + (script[scriptIndex - 1].value === source[sourceIndex - 1].value ? 0 : 1);
      const deletion = costs[scriptIndex - 1][sourceIndex] + 1;
      const insertion = costs[scriptIndex][sourceIndex - 1] + 1;
      costs[scriptIndex][sourceIndex] = Math.min(substitution, deletion, insertion);
    }
  }

  const matches: Array<number | undefined> = Array(script.length).fill(undefined);
  let scriptIndex = script.length;
  let sourceIndex = source.length;
  while (scriptIndex > 0 || sourceIndex > 0) {
    if (scriptIndex > 0 && sourceIndex > 0) {
      const substitution = script[scriptIndex - 1].value === source[sourceIndex - 1].value ? 0 : 1;
      if (costs[scriptIndex][sourceIndex] === costs[scriptIndex - 1][sourceIndex - 1] + substitution) {
        matches[scriptIndex - 1] = sourceIndex - 1;
        scriptIndex -= 1;
        sourceIndex -= 1;
        continue;
      }
    }
    if (scriptIndex > 0 && costs[scriptIndex][sourceIndex] === costs[scriptIndex - 1][sourceIndex] + 1) {
      scriptIndex -= 1;
    } else {
      sourceIndex -= 1;
    }
  }
  return matches;
}

function buildCharacterTimings(
  matches: Array<number | undefined>,
  script: NormalizedScriptChar[],
  source: TimedNormalizedChar[],
): CharacterTiming[] {
  const timings: Array<CharacterTiming | undefined> = matches.map((sourceIndex, index) => {
    if (sourceIndex === undefined) return undefined;
    const sourceCharacter = source[sourceIndex];
    if (script[index].value !== sourceCharacter.value) return undefined;
    return {
      start: sourceCharacter.start,
      end: sourceCharacter.end,
      confidence: 1,
    };
  });

  let index = 0;
  while (index < timings.length) {
    if (timings[index] !== undefined) {
      index += 1;
      continue;
    }
    const runStart = index;
    while (index < timings.length && timings[index] === undefined) index += 1;
    const runEnd = index;
    const left = runStart === 0 ? undefined : timings[runStart - 1];
    const right = runEnd === timings.length ? undefined : timings[runEnd];
    if (left === undefined || right === undefined) {
      throw new AlignmentError("ALIGNMENT_UNANCHORED", "Unmatched script characters require anchors on both sides");
    }

    const duration = (right.start - left.end) / (runEnd - runStart);
    if (duration < 0) {
      throw new AlignmentError("TIMELINE_REGRESSION", "Interpolation anchors regress");
    }
    for (let offset = 0; offset < runEnd - runStart; offset += 1) {
      timings[runStart + offset] = {
        start: left.end + duration * offset,
        end: left.end + duration * (offset + 1),
        confidence: 0,
      };
    }
  }

  return timings.map((timing) => {
    if (timing === undefined) throw new AlignmentError("ALIGNMENT_UNANCHORED", "Missing alignment timing");
    return timing;
  });
}

function spokenScriptIndices(scriptText: string, start: number, end: number): number[] {
  const indices: number[] = [];
  let scriptIndex = start;
  while (scriptIndex < end) {
    const codePoint = scriptText.codePointAt(scriptIndex);
    if (codePoint === undefined) break;
    const character = String.fromCodePoint(codePoint);
    if (spokenCharacter.test(character)) indices.push(scriptIndex);
    scriptIndex += character.length;
  }
  return indices;
}

function buildAlignedChars(
  scriptText: string,
  script: NormalizedScriptChar[],
  timings: CharacterTiming[],
  speed: number,
  leadSeconds: number,
): AlignedChar[] {
  const chars: AlignedChar[] = [];
  let normalizedIndex = 0;
  while (normalizedIndex < script.length) {
    const item = script[normalizedIndex];
    if (item.spanStart !== undefined && item.spanEnd !== undefined) {
      let spanEndIndex = normalizedIndex + 1;
      while (
        spanEndIndex < script.length
        && script[spanEndIndex].spanStart === item.spanStart
        && script[spanEndIndex].spanEnd === item.spanEnd
      ) {
        spanEndIndex += 1;
      }
      if (timings.slice(normalizedIndex, spanEndIndex).some((timing) => timing.confidence !== 1)) {
        throw new AlignmentError("ALIGNMENT_UNANCHORED", "Canonical script span lacks exact source anchors");
      }

      const originalIndices = spokenScriptIndices(scriptText, item.spanStart, item.spanEnd);
      const sourceStart = timings[normalizedIndex].start;
      const sourceEnd = timings[spanEndIndex - 1].end;
      const duration = (sourceEnd - sourceStart) / originalIndices.length;
      originalIndices.forEach((scriptIndex, index) => {
        chars.push({
          scriptIndex,
          start: (sourceStart + duration * index) / speed - leadSeconds,
          end: (sourceStart + duration * (index + 1)) / speed - leadSeconds,
          confidence: 1,
        });
      });
      normalizedIndex = spanEndIndex;
      continue;
    }

    const timing = timings[normalizedIndex];
    chars.push({
      scriptIndex: item.scriptIndex,
      start: timing.start / speed - leadSeconds,
      end: timing.end / speed - leadSeconds,
      confidence: timing.confidence,
    });
    normalizedIndex += 1;
  }
  return chars;
}

export function alignScriptToWords(
  scriptText: string,
  words: TimedWord[],
  speed: number,
  leadSeconds: number,
): AlignmentResult {
  validateInputs(words, speed, leadSeconds);
  const script = normalizeScript(scriptText);
  if (script.length === 0) {
    throw new AlignmentError("EMPTY_SCRIPT", "Script has no spoken characters");
  }
  const source = normalizeWords(words);
  const matches = alignNeedlemanWunsch(script, source);
  const coverage = matches.filter((sourceIndex, index) => sourceIndex !== undefined && script[index].value === source[sourceIndex].value).length / script.length;
  if (coverage < 0.8) {
    throw new AlignmentError("ALIGNMENT_LOW_COVERAGE", `coverage ${coverage.toFixed(3)} is below 0.80`);
  }

  const timings = buildCharacterTimings(matches, script, source);
  const chars = buildAlignedChars(scriptText, script, timings, speed, leadSeconds);
  for (let index = 1; index < chars.length; index += 1) {
    if (chars[index].start < chars[index - 1].end) {
      throw new AlignmentError("TIMELINE_REGRESSION", "Aligned character timestamps regress");
    }
  }
  return { chars, coverage };
}
