import assert from "node:assert/strict";
import test from "node:test";
const { layoutSubtitle, layoutSegments } = require("../src/ad-subtitle-layout.js");

test("current preview wraps on complete phrases instead of the eleventh character", () => {
  assert.equal(layoutSubtitle("上周周边制造业企业选择老厂房续租，", 11, 2), "上周周边制造业企业\n选择老厂房续租");
  assert.equal(layoutSubtitle("嘉兴平湖新仓智谷天地：国资、可租可售、", 11, 2), "嘉兴平湖新仓智谷天地\n国资、可租可售");
  assert.equal(layoutSubtitle("层高11到12米，一层承重5吨，", 11, 2), "层高11到12米\n一层承重5吨");
});

test("display hides boundary punctuation but preserves meaningful decimals and units", () => {
  for (const value of ["1.2元/㎡", "3到6个月", "11到12米", "10吨", "12000", "2200", "99.5%", "1,200万元"]) {
    const result = layoutSubtitle(`价格${value}，支持按需分割。`, 9, 2);
    assert.ok(result.includes(value), result);
    assert.ok(!/[，。！？、；：,.!?;:]$/u.test(result));
    assert.ok(!result.split("\n").some((line: string) => /^[，。！？、；：,.!?;:]/u.test(line)));
  }
});

test("layout never mutates raw copy, shots, cue times, or deletes spoken characters", () => {
  const input = [
    { text: "还是换层高11米、", start: 4.318, end: 6.452, src: "clip.mp4" },
    { text: "能上10吨行车的新载体？", start: 6.452, end: 8.79, src: "clip2.mp4" },
  ];
  const before = JSON.stringify(input);
  const output = layoutSegments(input, { max_chars_per_line: 11, max_lines: 2 }, "16:9");
  assert.equal(JSON.stringify(input), before);
  const spoken = (text: string) => text.replace(/[\s\p{P}]/gu, "");
  assert.equal(spoken(output.map((row: any) => row.displayText).join("")), spoken(input.map(row => row.text).join("")));
  output.forEach((row: any, i: number) => {
    assert.equal(row.text, input[i].text);
    assert.equal(row.start, input[i].start);
    assert.equal(row.end, input[i].end);
    assert.equal(row.src, input[i].src);
    assert.equal(row.text, input[i].text);
    assert.ok(row.displayText.split("\n").length <= 2);
  });
});

test("keeps a caption flowing across adjacent shot cuts, with one continuous time span", () => {
  const input = [
    { text: "上周周边制造业企业选择老厂房续租，", start: 0, end: 4.318, src: "a.mp4" },
    { text: "还是换层高11米、", start: 4.318, end: 6.452, src: "b.mp4" },
    { text: "能上10吨行车的新载体？", start: 6.452, end: 8.79, src: "c.mp4" },
  ];
  const output = layoutSegments(input, { max_chars_per_line: 11, max_lines: 2 }, "16:9");
  const captions = output.filter((row: any) => row.displayText);
  assert.equal(captions.length, 2);
  assert.equal(captions[1].subtitleText, "还是换层高11米、能上10吨行车的新载体？");
  assert.equal(captions[0].subtitleStart, 0);
  assert.equal(captions[0].subtitleEnd, 4.318);
  assert.equal(output[1].text, input[1].text);
  assert.equal(output[2].displayText, "");
  assert.equal(captions[1].subtitleStart, 4.318);
  assert.equal(captions[1].subtitleEnd, 8.79);
});

test("protected names and English words remain whole, and punctuation-only cues fail", () => {
  const result = layoutSubtitle("欢迎来到CUTROOM创新产业园，", 8, 2, ["创新产业园"]);
  assert.ok(result.includes("CUTROOM"));
  assert.ok(result.includes("创新产业园"));
  assert.throws(() => layoutSubtitle("，。！？", 11, 2), /AD_SUBTITLE_EMPTY/);
  assert.throws(() => layoutSubtitle("文案", 0, 2), /LAYOUT_INVALID/);
});
