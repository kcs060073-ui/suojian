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

// Only explicit compound quantities are canonicalized; ordinary single-character
// words (一度、三分之一、万 after an Arabic decimal) retain their original form.
const numberSpans = /百分之[零〇一二三四五六七八九十百千万两]+|[零〇一二三四五六七八九两]*[十百千][零〇一二三四五六七八九十百千两]*/g;
function canonicalNumberSpan(text: string): string {
  return text.startsWith("百分之") ? normalizeWord(text) : String(chineseNumberToArabic(text));
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
    const tail = toSimplified(scriptText.slice(scriptIndex)).normalize("NFKC");
    const span = new RegExp(`^(?:${numberSpans.source})`).exec(tail);
    if (span !== null) {
      for (const value of canonicalNumberSpan(span[0])) {
        result.push({ value, scriptIndex, spanStart: scriptIndex, spanEnd: scriptIndex + span[0].length });
      }
      scriptIndex += span[0].length;
      continue;
    }

    const codePoint = scriptText.codePointAt(scriptIndex);
    if (codePoint === undefined) break;
    const character = String.fromCodePoint(codePoint);
    const numericSeparator = /[.,:．，：]/u.test(character)
      && /[0-9０-９]/u.test(scriptText[scriptIndex - 1] ?? "")
      && /[0-9０-９]/u.test(scriptText[scriptIndex + 1] ?? "");
    if (spokenCharacter.test(character) || numericSeparator) {
      for (const value of normalizeCharacter(character)) {
        if (spokenCharacter.test(value) || numericSeparator) result.push({ value, scriptIndex });
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
  const raw = words.flatMap(word => {
    const values = Array.from(toSimplified(word.w).normalize("NFKC").toUpperCase())
      .filter(character => !/\s/u.test(character));
    return values.map((value, i) => ({value,
      start: word.start + (word.end-word.start)*i/values.length,
      end: word.start + (word.end-word.start)*(i+1)/values.length}));
  });
  const kept = raw.map((item, index) => spokenCharacter.test(item.value) ||
    (/[.,:]/u.test(item.value) && /[0-9]/u.test(raw[index-1]?.value ?? "") &&
     /[0-9]/u.test(raw[index+1]?.value ?? "")) ? item : {...item, value:"\0"});
  const text = kept.map(item=>item.value).join("");
  const result: TimedNormalizedChar[] = [];
  let cursor = 0;
  for (const span of text.matchAll(numberSpans)) {
    const startIndex = span.index!, endIndex = startIndex+span[0].length;
    result.push(...kept.slice(cursor, startIndex));
    const values = Array.from(canonicalNumberSpan(span[0]));
    const a = kept[startIndex].start, b = kept[endIndex-1].end;
    values.forEach((value,i) => result.push({value,
      start:a+(b-a)*i/values.length, end:a+(b-a)*(i+1)/values.length}));
    cursor = endIndex;
  }
  result.push(...kept.slice(cursor));
  const spoken = result.filter(item => item.value !== "\0");
  if (spoken.length === 0) {
    throw new AlignmentError("EMPTY_WORDS", "Whisper word sequence has no spoken characters");
  }
  return spoken;
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

function verifyNumbers(script: NormalizedScriptChar[], source: TimedNormalizedChar[],
                       matches: Array<number | undefined>): void {
  const text = script.map(item => item.value).join("");
  const sourceText = source.map(item => item.value).join("");
  const pattern = /[0-9]+(?:[.,:][0-9]+)*(?:%)?/g;
  for (const fact of text.matchAll(pattern)) {
    const lo = fact.index!, hi = lo + fact[0].length;
    const selected = matches.slice(lo, hi);
    const start = selected[0];
    const exact = start !== undefined && selected.every((index, offset) =>
      index === start + offset && source[index!]?.value === script[lo+offset].value);
    const end = start === undefined ? 0 : start + fact[0].length;
    const extended = start !== undefined &&
      (/[0-9]/.test(sourceText[start-1] ?? "") || /[0-9%]/.test(sourceText[end] ?? "") ||
       (/[.,:]/.test(sourceText[start-1] ?? "") && /[0-9]/.test(sourceText[start-2] ?? "")) ||
       (/[.,:]/.test(sourceText[end] ?? "") && /[0-9]/.test(sourceText[end+1] ?? "")));
    if (!exact || extended) {
      throw new AlignmentError("ALIGNMENT_UNVERIFIED_NUMBER",
        `原稿数字 ${fact[0]} 缺少一致的转写或已确认修正，不能猜测时码`);
    }
  }
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

  verifyNumbers(script, source, matches);
  const timings = buildCharacterTimings(matches, script, source);
  const chars = buildAlignedChars(scriptText, script, timings, speed, leadSeconds);
  for (let index = 1; index < chars.length; index += 1) {
    if (chars[index].start < chars[index - 1].end) {
      throw new AlignmentError("TIMELINE_REGRESSION", "Aligned character timestamps regress");
    }
  }
  return { chars, coverage };
}
