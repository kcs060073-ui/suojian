import assert from "node:assert/strict";
import test from "node:test";
import {
  AlignmentError,
  alignScriptToWords,
} from "../src/subtitle-alignment";

test("aligns traditional script characters to simplified Whisper words while preserving script indices", () => {
  const result = alignScriptToWords(
    "新疆風電",
    [{ w: "新疆风电", start: 0, end: 1 }],
    1,
    0.08,
  );

  assert.equal(result.coverage, 1);
  assert.deepEqual(result.chars.map((char) => char.scriptIndex), [0, 1, 2, 3]);
  assert.deepEqual(result.chars.map((char) => [Number(char.start.toFixed(2)), Number(char.end.toFixed(2))]), [
    [-0.08, 0.17],
    [0.17, 0.42],
    [0.42, 0.67],
    [0.67, 0.92],
  ]);
  assert.deepEqual(result.chars.map((char) => char.confidence), [1, 1, 1, 1]);
});

test("keeps the following matching phrase anchored after a numeric spoken-form mismatch", () => {
  const result = alignScriptToWords(
    "40%同样的逻辑",
    [
      { w: "百分之四十", start: 0, end: 1 },
      { w: "同样的逻辑", start: 3.1, end: 4.1 },
    ],
    1.25,
    0.08,
  );

  const same = result.chars.find((char) => char.scriptIndex === 3);
  assert.ok(same);
  assert.ok(Math.abs(same.start - 2.4) <= 0.001);
  assert.equal(result.coverage, 1);
});

test("fails closed when too little script text can align", () => {
  assert.throws(
    () => alignScriptToWords(
      "天地玄黄宇宙洪荒",
      [{ w: "abcd", start: 0, end: 1 }],
      1,
      0,
    ),
    (error: unknown) => error instanceof AlignmentError && error.code === "ALIGNMENT_LOW_COVERAGE",
  );
});

test("fails closed for an empty Whisper word sequence", () => {
  assert.throws(
    () => alignScriptToWords("新疆", [], 1, 0),
    (error: unknown) => error instanceof AlignmentError && error.code === "EMPTY_WORDS",
  );
});

test("fails closed when Whisper word timestamps regress", () => {
  assert.throws(
    () => alignScriptToWords(
      "新疆",
      [
        { w: "新", start: 1, end: 1.1 },
        { w: "疆", start: 0.9, end: 1.2 },
      ],
      1,
      0,
    ),
    (error: unknown) => error instanceof AlignmentError && error.code === "TIMELINE_REGRESSION",
  );
});
test("interpolates a short unmatched run between monotonic Whisper anchors", () => {
  const result = alignScriptToWords(
    "甲乙丙丁戊",
    [
      { w: "甲", start: 0, end: 0.4 },
      { w: "乙", start: 0.4, end: 0.8 },
      { w: "丁", start: 1.2, end: 1.6 },
      { w: "戊", start: 1.6, end: 2 },
    ],
    1,
    0,
  );

  assert.equal(result.coverage, 0.8);
  assert.deepEqual(result.chars[2], {
    scriptIndex: 2,
    start: 0.8,
    end: 1.2,
    confidence: 0,
  });
});
test("fails closed when equal-length unrelated text has no exact anchors", () => {
  assert.throws(
    () => alignScriptToWords(
      "天地玄黄",
      [{ w: "abcd", start: 0, end: 1 }],
      1,
      0,
    ),
    (error: unknown) => error instanceof AlignmentError && error.code === "ALIGNMENT_LOW_COVERAGE",
  );
});

test("canonicalizes scripted spoken percentages without losing downstream original indices", () => {
  const result = alignScriptToWords(
    "百分之四十同样的逻辑",
    [
      { w: "40%", start: 0, end: 1 },
      { w: "同样的逻辑", start: 3.1, end: 4.1 },
    ],
    1.25,
    0.08,
  );

  const same = result.chars.find((char) => char.scriptIndex === 5);
  assert.ok(same);
  assert.ok(Math.abs(same.start - 2.4) <= 0.001);
  assert.equal(result.coverage, 1);
});

test("expands a canonicalized scripted percentage back to every original script index", () => {
  const result = alignScriptToWords(
    "百分之四十",
    [{ w: "40%", start: 0, end: 1 }],
    1,
    0,
  );

  assert.equal(result.coverage, 1);
  assert.deepEqual(result.chars.map((char) => char.scriptIndex), [0, 1, 2, 3, 4]);
  for (let index = 1; index < result.chars.length; index += 1) {
    assert.ok(result.chars[index].start >= result.chars[index - 1].end);
  }
});
