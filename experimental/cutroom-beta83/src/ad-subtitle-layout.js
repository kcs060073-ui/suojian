"use strict";

// Display-only rules. Never rewrite locked copy, TTS input, or cue timing.
const RULE_VERSION = "ad-subtitle-layout-2";
const EDGE_PUNCTUATION = /^[\s，。！？、；：,.!?;:]+|[\s，。！？、；：,.!?;:]+$/gu;
const PROTECTED_TERMS = ["制造业", "老厂房", "新载体", "可租可售", "无二房东", "按需分割", "相关资料", "点击下方链接", "预约考察", "产业扶持政策", "免租期", "平方米", "一层承重", "单层最大", "单层最小"];
const NUMBER = "[0-9０-９]+(?:[,，][0-9０-９]{3})*(?:[.．][0-9０-９]+)?";
const NUMERIC_TOKEN = new RegExp(`(?:最大|最小|层高|承重)?${NUMBER}(?:[到至~～—-]${NUMBER})?(?:多|余)?(?:万平方米|平方米|平米|㎡|m²|m2|个月|分钟|公里|千瓦时|千瓦|万元|亿元|万吨|公斤|千克|亿|万|千|百|元|米|吨|亩|月|年|天|秒|%|％)?(?:[/／](?:平方米|平米|㎡|m²|m2|月|年|天))?`, "giu");
const lexical = new Intl.Segmenter("zh-CN", { granularity: "word" });

function cleanEdge(text) {
  return text.replace(EDGE_PUNCTUATION, "").trim();
}

function protectedSpans(text, extraTerms) {
  const spans = [];
  for (const match of text.matchAll(NUMERIC_TOKEN)) spans.push([match.index, match.index + match[0].length]);
  for (const term of [...PROTECTED_TERMS, ...extraTerms].filter(Boolean)) {
    let offset = text.indexOf(term);
    while (offset >= 0) {
      spans.push([offset, offset + term.length]);
      offset = text.indexOf(term, offset + term.length);
    }
  }
  // A short name immediately introducing a colon is usually a project/company name.
  for (const match of text.matchAll(/(?:^|[，。！？；,!?;])([\p{L}]{2,16})[：:]/gu)) {
    const start = match.index + match[0].indexOf(match[1]);
    spans.push([start, start + match[1].length]);
  }
  return spans;
}

function displayWidth(text) {
  return Array.from(text).reduce((width, char) => width + (/\s/u.test(char) ? 0.35 : /[\x00-\x7f]/u.test(char) ? 0.55 : 1), 0);
}

function layoutSubtitle(raw, maxChars = 13, maxLines = 2, protectedTerms = []) {
  if (!Number.isInteger(maxChars) || maxChars < 4 || !Number.isInteger(maxLines) || maxLines < 1 || maxLines > 4) {
    throw new Error("AD_SUBTITLE_LAYOUT_INVALID: invalid line limits");
  }
  const text = cleanEdge(String(raw).replace(/\s+/gu, " "));
  if (!/[\p{L}\p{N}]/u.test(text)) throw new Error("AD_SUBTITLE_EMPTY: punctuation-only subtitle");
  if (displayWidth(text) <= maxChars || maxLines === 1) return text;
  const spans = protectedSpans(text, protectedTerms);
  const boundaries = [0];
  for (const token of lexical.segment(text)) {
    const boundary = token.index + token.segment.length;
    if (boundary === text.length) continue;
    if (spans.some(([start, end]) => start < boundary && boundary < end)) continue;
    if (/^[，。！？、；：,.!?;:）)】》”’]/u.test(text.slice(boundary))) continue;
    if (/[（(【《“‘]$/u.test(text.slice(0, boundary))) continue;
    boundaries.push(boundary);
  }
  boundaries.push(text.length);
  const target = Math.min(maxChars, displayWidth(text) / Math.min(maxLines, Math.ceil(displayWidth(text) / maxChars)));
  const cache = new Map();
  function solve(start, remaining) {
    const key = `${start}:${remaining}`;
    if (cache.has(key)) return cache.get(key);
    let best = null;
    for (const end of boundaries) {
      if (end <= start || (remaining === 1 && end !== text.length)) continue;
      const line = cleanEdge(text.slice(start, end));
      if (!/[\p{L}\p{N}]/u.test(line)) continue;
      const width = displayWidth(line);
      let score = 8 + (width - target) ** 2 + Math.max(0, width - maxChars) ** 2 * 80;
      if (width < 3 && text.length > 6) score += 90;
      if (end < text.length) {
        const left = text.slice(start, end).trim(), right = text.slice(end).trim();
        if (/[，。！？、；：,.!?;:]$/u.test(left)) score -= 20;
        if (/^(?:选择|还是|能上|支持|可租|无二|一层|全一楼|单层|按需|免租|想要|点击|预约)/u.test(right)) score -= 12;
        if (/[的和与及把被在对将是能可到从更最还要让有向为跟或但并]$/u.test(left)) score += 45;
        if (/^[的了着过们吗呢吧]/u.test(right)) score += 90;
      }
      const tail = end === text.length ? { score: 0, lines: [] } : remaining > 1 ? solve(end, remaining - 1) : null;
      if (!tail) continue;
      const option = { score: score + tail.score, lines: [line, ...tail.lines] };
      if (!best || option.score < best.score) best = option;
    }
    cache.set(key, best);
    return best;
  }
  return solve(0, maxLines).lines.join("\n");
}

function layoutSegments(segments, preset = {}, ratio = "9:16") {
  const maxChars = preset.max_chars_per_line ?? (ratio === "16:9" ? 30 : 13);
  const maxLines = preset.max_lines ?? 2;
  const mergeLimit = maxChars * maxLines;
  const groups = [];
  for (const segment of segments) {
    const previous = groups.at(-1);
    const joined = previous && `${previous.text}${segment.text}`;
    const sentenceEnded = previous && /[。！？!?；;][”’"']?$/u.test(previous.text.trim());
    const continuous = previous && Number.isFinite(previous.subtitleEnd) && Number.isFinite(segment.start) && Math.abs(previous.subtitleEnd - segment.start) <= 0.02;
    const contrasting = /^(?:还是|但是|然而|而是|否则|另一方面)/u.test(segment.text.trim());
    const contentWidth = joined && displayWidth(cleanEdge(joined).replace(/[，、：,:;]/gu, ""));
    if (previous && !segment.subtitleSemanticBoundary && !previous.subtitleSemanticBoundary && continuous && !sentenceEnded && !contrasting && contentWidth <= mergeLimit) {
      previous.text = joined;
      previous.subtitleEnd = segment.end;
      previous.sourceSegments.push(segment);
    } else {
      groups.push({ ...segment, subtitleStart: segment.start, subtitleEnd: segment.end, sourceSegments: [segment] });
    }
  }
  const output = [];
  for (const group of groups) {
    const { sourceSegments } = group;
    output.push({ ...sourceSegments[0], subtitleStart: group.subtitleStart, subtitleEnd: group.subtitleEnd, subtitleText: group.text, displayText: layoutSubtitle(group.text, maxChars, maxLines, preset.protected_terms ?? []), subtitleLayoutVersion: RULE_VERSION });
    for (const segment of sourceSegments.slice(1)) {
      output.push({ ...segment, displayText: "", subtitleLayoutVersion: RULE_VERSION });
    }
  }
  return output;
}

module.exports = { RULE_VERSION, layoutSubtitle, layoutSegments };
if (require.main === module) {
  try {
    const request = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
    process.stdout.write(JSON.stringify(layoutSegments(request.segments, request.preset, request.ratio)));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
