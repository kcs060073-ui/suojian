import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { buildAssDocument, SubtitleAssError } from "../src/subtitle-ass";
import { parseMakeAssOptions } from "../scripts/make-ass";

const root = path.resolve(__dirname, "..");
const fixtureRoot = path.join(__dirname, "fixtures");

const words = (text: string, start = 0, secondsPerChar = 0.2) => [{
  w: text,
  start,
  end: start + Array.from(text).length * secondsPerChar,
}];

test("renders the modern clean preset with exact approved styles and fade", () => {
  const result = buildAssDocument({ scriptText: "新疆风电", words: words("新疆风电") });

  assert.match(result.ass, /Style: Default,Alimama ShuHeiTi Bold,72,&H00FFFFFF&,[^\n]*,4,1,2,40,40,430,1/);
  assert.match(result.ass, /Style: Punch,Alimama ShuHeiTi Bold,82,&H00FFFFFF&,[^\n]*,4,1,2,40,40,430,1/);
  assert.match(result.ass, /\\fad\(60,80\)/);
  assert.match(result.ass, /&H003794FD&/);
  assert.equal(result.metadata.schema, 1);
  assert.equal(result.metadata.preset, "sub-modern-clean-v1");
  assert.equal(result.metadata.speed, 1.25);
  assert.equal(result.metadata.lead, 0.08);
  assert.match(result.metadata.assSha256, /^[a-f0-9]{64}$/);
});

test("wraps replace-only cues at twelve full-width characters into at most two lines", () => {
  const text = "一二三四五六七八九十一二三四五六七八九十甲乙丙丁";
  const result = buildAssDocument({ scriptText: text, words: words(text), speed: 1, lead: 0 });

  assert.equal(result.metadata.cues.length, 1);
  assert.match(result.metadata.cues[0].text, /^一二三四五六七八九十一二\\N三四五六七八九十甲乙丙丁$/);
  assert.equal(result.metadata.cues[0].text.includes("\\N\\N"), false);
});

test("keeps a 24-glyph modern cue renderable when followed by trailing punctuation", () => {
  const spoken = "一二三四五六七八九十一二三四五六七八九十甲乙丙丁";
  const result = buildAssDocument({
    scriptText: `${spoken}，`,
    words: words(spoken),
    speed: 1,
    lead: 0,
  });

  assert.equal(result.metadata.cues.length, 1);
  assert.equal(result.metadata.cues[0].text, "一二三四五六七八九十一二\\N三四五六七八九十甲乙丙丁，");
});

test("splits tail-pad overlaps at their midpoint without cumulative dwell", () => {
  const result = buildAssDocument({
    scriptText: "甲乙，丙丁。",
    words: [
      { w: "甲乙", start: 0, end: 0.2 },
      { w: "丙丁", start: 0.2, end: 0.4 },
    ],
    speed: 1,
    lead: 0,
  });

  assert.equal(result.metadata.cues.length, 2);
  assert.equal(result.metadata.cues[0].end, result.metadata.cues[1].start);
  assert.ok(result.metadata.cues[0].end > 0.2 && result.metadata.cues[0].end < 0.32);
  assert.ok(result.metadata.cues[1].end <= 0.52);
});

test("fails with stable errors for missing anchors and a duration outside the cue timeline", () => {
  assert.throws(
    () => buildAssDocument({ scriptText: "新疆", words: words("新疆"), duration: 0.1 }),
    (error: unknown) => error instanceof SubtitleAssError && error.code === "CUE_OUT_OF_RANGE",
  );
  assert.throws(
    () => buildAssDocument({ scriptText: "，", words: words("新疆") }),
    (error: unknown) => error instanceof SubtitleAssError && error.code === "CUE_ANCHOR_MISSING",
  );
});

test("keeps the real Xinjiang same-logic anchor in its approved range", () => {
  const result = buildAssDocument({
    scriptText: "40%同样的逻辑",
    words: [
      { w: "百分之四十", start: 0, end: 1 },
      { w: "同样的逻辑", start: 3.1, end: 4.1 },
    ],
  });
  const cue = result.metadata.cues.find((item) => item.text.includes("同样的逻辑"));
  assert.ok(cue);
  assert.ok(cue.start >= 2.28 && cue.start <= 2.52);
  assert.ok(result.metadata.coverage >= 0.8);
});

test("CLI accepts explicit paths and writes the ASS plus schema-one metadata", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "subtitle-ass-cli-"));
  try {
    const scriptPath = path.join(temp, "script.txt");
    const wordsPath = path.join(temp, "words.json");
    const outputPath = path.join(temp, "output.ass");
    const metadataPath = path.join(temp, "output.metadata.json");
    const semanticsPath = path.join(temp, "semantics.json");
    fs.writeFileSync(scriptPath, "新疆40%同样的逻辑", "utf8");
    fs.writeFileSync(wordsPath, JSON.stringify([
      { w: "新疆百分之四十", start: 0, end: 1 },
      { w: "同样的逻辑", start: 3.1, end: 4.1 },
    ]));
    fs.writeFileSync(semanticsPath, JSON.stringify({ keywords: [], punchLines: [] }));
    execFileSync(process.execPath, ["-r", "ts-node/register", path.join(root, "scripts", "make-ass.ts"),
      "--script", scriptPath, "--words", wordsPath, "--semantics", semanticsPath,
      "--output", outputPath, "--metadata", metadataPath,
      "--preset", "sub-modern-clean-v1", "--speed", "1.25", "--lead", "0.08", "--duration", "4",
    ], { cwd: root });
    assert.ok(fs.existsSync(outputPath));
    const metadata = JSON.parse(fs.readFileSync(metadataPath, "utf8"));
    assert.equal(metadata.schema, 1);
    assert.equal(metadata.preset, "sub-modern-clean-v1");
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});
test("rejects a cue composed only of interpolated alignment characters", () => {
  assert.throws(
    () => buildAssDocument({
      scriptText: "甲乙，丙，丁戊",
      words: [
        { w: "甲乙", start: 0, end: 0.4 },
        { w: "丁戊", start: 0.8, end: 1.2 },
      ],
      speed: 1,
      lead: 0,
    }),
    (error: unknown) => error instanceof SubtitleAssError && error.code === "CUE_ANCHOR_MISSING",
  );
});

test("writes orange inline ASS tags for numeric tokens and configured keywords before returning to white", () => {
  const result = buildAssDocument({
    scriptText: "40%新疆",
    words: [{ w: "百分之四十新疆", start: 0, end: 1.2 }],
    keywords: ["新疆"],
    speed: 1,
    lead: 0,
  });

  assert.match(result.ass, /Dialogue:.*\\fad\(60,80\).*\{\\c&H003794FD&\}40%\{\\c&H00FFFFFF&\}\{\\c&H003794FD&\}新疆\{\\c&H00FFFFFF&\}/);
});

test("keeps an adjacent numeric token whole when the twelve-character line boundary would split it", () => {
  const text = "一二三四五六七八九十40%甲";
  const result = buildAssDocument({ scriptText: text, words: words(text), speed: 1, lead: 0 });

  assert.equal(result.metadata.cues[0].text, "一二三四五六七八九十\\N40%甲");
  assert.equal(result.metadata.cues[0].text.includes("4\\N0"), false);
  assert.equal(result.metadata.cues[0].text.includes("0\\N%"), false);
});

test("clamps only the optional tail pad at an explicit duration while retaining natural end validation", () => {
  const result = buildAssDocument({
    scriptText: "甲",
    words: [{ w: "甲", start: 0, end: 0.1 }],
    speed: 1,
    lead: 0,
    duration: 0.15,
  });
  assert.equal(result.metadata.cues[0].end, 0.15);
  assert.throws(
    () => buildAssDocument({ scriptText: "甲", words: [{ w: "甲", start: 0, end: 0.2 }], speed: 1, lead: 0, duration: 0.15 }),
    (error: unknown) => error instanceof SubtitleAssError && error.code === "CUE_OUT_OF_RANGE",
  );
});
test("chooses the longest overlapping keyword once and escapes literal ASS control characters", () => {
  const result = buildAssDocument({
    scriptText: "新疆园区{甲}\\乙",
    words: [{ w: "新疆园区甲乙", start: 0, end: 1.2 }],
    keywords: ["新疆", "新疆园区"],
    speed: 1,
    lead: 0,
  });

  assert.match(result.ass, /\{\\c&H003794FD&\}新疆园区\{\\c&H00FFFFFF&\}/);
  assert.doesNotMatch(result.ass, /\{\\c&H003794FD&\}新疆\{\\c&H00FFFFFF&\}\{\\c&H003794FD&\}园区/);
  assert.match(result.ass, /\\\{甲\\\}\\\\乙/);
});
test("keeps a full numeric quantity token whole and colors it as one orange span", () => {
  const text = "一二三四五六七八九十300多亿甲";
  const result = buildAssDocument({ scriptText: text, words: words(text), speed: 1, lead: 0 });

  assert.equal(result.metadata.cues[0].text, "一二三四五六七八九十\\N300多亿甲");
  assert.equal(result.metadata.cues[0].text.includes("300\\N多亿"), false);
  assert.match(result.ass, /\{\\c&H003794FD&\}300多亿\{\\c&H00FFFFFF&\}/);
});

test("does not split a configured keyword at a twelve-character wrap boundary", () => {
  const text = "一二三四五六七八九十新疆园区甲";
  const result = buildAssDocument({
    scriptText: text,
    words: words(text),
    keywords: ["新疆园区"],
    speed: 1,
    lead: 0,
  });

  assert.equal(result.metadata.cues[0].text, "一二三四五六七八九十\\N新疆园区甲");
  assert.equal(result.metadata.cues[0].text.includes("新疆\\N园区"), false);
});
test("fails closed when an indivisible protected keyword cannot fit either of two lines", () => {
  const keyword = "甲乙丙丁戊己庚辛壬癸子丑寅";
  assert.throws(
    () => buildAssDocument({
      scriptText: `一二三四五六七八九十一${keyword}`,
      words: words(`一二三四五六七八九十一${keyword}`),
      keywords: [keyword],
      speed: 1,
      lead: 0,
    }),
    (error: unknown) => error instanceof SubtitleAssError && error.code === "CUE_OUT_OF_RANGE",
  );
});
test("does not cut a full numeric quantity token at the 24-character preliminary cue boundary", () => {
  const prefix = "一二三四五六七八九十一二三四五六七八九十甲乙";
  const result = buildAssDocument({
    scriptText: `${prefix}300多亿甲`,
    words: words(`${prefix}300多亿甲`),
    speed: 1,
    lead: 0,
  });

  assert.equal(result.metadata.cues.length, 2);
  assert.equal(result.metadata.cues[0].text.includes("30"), false);
  assert.equal(result.metadata.cues[1].text, "300多亿甲");
});

test("does not cut a configured keyword at the 24-character preliminary cue boundary", () => {
  const prefix = "一二三四五六七八九十一二三四五六七八九十甲乙";
  const keyword = "新疆园区甲";
  const result = buildAssDocument({
    scriptText: `${prefix}${keyword}`,
    words: words(`${prefix}${keyword}`),
    keywords: [keyword],
    speed: 1,
    lead: 0,
  });

  assert.equal(result.metadata.cues.length, 2);
  assert.equal(result.metadata.cues[0].text.includes("新疆"), false);
  assert.equal(result.metadata.cues[1].text, keyword);
});
function dialogueLines(ass: string): string[] {
  return ass.split("\n").filter((line) => line.startsWith("Dialogue: "));
}

test("treats a literal script LF as a hard cue boundary without leaking it into Dialogue text", () => {
  const result = buildAssDocument({
    scriptText: "甲乙\n丙丁",
    words: [{ w: "甲乙丙丁", start: 0, end: 0.8 }],
    speed: 1,
    lead: 0,
  });
  const dialogues = dialogueLines(result.ass);

  assert.deepEqual(result.metadata.cues.map((cue) => cue.text), ["甲乙", "丙丁"]);
  assert.equal(dialogues.length, 2);
  assert.ok(dialogues.every((line) => !/[\r\n]/.test(line)));
  assert.equal(result.ass.includes("甲乙\n丙丁"), false);
});

test("ignores CRLF blank lines while keeping every semantic line as a safe replace cue", () => {
  const result = buildAssDocument({
    scriptText: "甲乙\r\n\r\n丙丁\r\n\r\n",
    words: [{ w: "甲乙丙丁", start: 0, end: 0.8 }],
    speed: 1,
    lead: 0,
  });
  const dialogues = dialogueLines(result.ass);

  assert.deepEqual(result.metadata.cues.map((cue) => cue.text), ["甲乙", "丙丁"]);
  assert.equal(dialogues.length, 2);
  assert.ok(dialogues.every((line) => !/[\r\n]/.test(line)));
  assert.equal(result.ass.includes("\r"), false);
});

test("positional CLI restores the manifest-backed legacy style and brief semantics", () => {
  const name = `task6d-legacy-${process.pid}-${Date.now()}`;
  const scripts = path.join(root, "public", "scripts");
  const scriptPath = path.join(scripts, `${name}.script.txt`);
  const wordsPath = path.join(scripts, `${name}.words.json`);
  const briefPath = path.join(scripts, `${name}.brief.json`);
  const outputPath = path.join(scripts, `${name}.ass`);
  const metadataPath = path.join(scripts, `${name}.subtitle.json`);
  try {
    fs.mkdirSync(scripts, { recursive: true });
    const scriptText = "新疆算力。算力发展，是我们的金句。";
    fs.writeFileSync(scriptPath, scriptText, "utf8");
    fs.writeFileSync(wordsPath, JSON.stringify(words("新疆算力算力发展是我们的金句", 0, 0.2)), "utf8");
    fs.writeFileSync(briefPath, JSON.stringify({
      keywords: ["新疆"],
      punchLines: ["  算力发展，是我们的金句！  "],
      title: "legacy brief may contain unrelated planning fields",
    }), "utf8");

    execFileSync(process.execPath, [
      "-r", "ts-node/register", path.join(root, "scripts", "make-ass.ts"), name, "1",
    ], { cwd: root });

    const ass = fs.readFileSync(outputPath, "utf8");
    assert.ok(ass.split("\n").includes("Style: Default,Source Han Serif SC Heavy,93,&H00FFFFFF&,&H003794FD&,&H00000000&,&H64000000&,0,0,0,0,100,100,1,0,1,5,2,2,40,40,560,1"));
    assert.ok(ass.split("\n").includes("Style: Punch,Source Han Serif SC Heavy,100,&H00FFFFFF&,&H003794FD&,&H00000000&,&H64000000&,0,0,0,0,100,100,2,0,1,6,3,2,40,40,560,1"));
    assert.match(ass, /Dialogue:.*Default.*\{\\c&H003794FD&\}新疆\{\\c&H00FFFFFF&\}/);
    assert.match(ass, /Dialogue:.*Punch.*算力发展，(?:\\N)?是我们的金句。/);
    assert.equal(JSON.parse(fs.readFileSync(metadataPath, "utf8")).preset, "sub-legacy-heavy-v1");
  } finally {
    for (const file of [scriptPath, wordsPath, briefPath, outputPath, metadataPath]) {
      fs.rmSync(file, { force: true });
    }
  }
});

test("explicit modern CLI binds supplied keyword and punch-line semantics without changing modern styling", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "subtitle-semantics-cli-"));
  try {
    const scriptPath = path.join(temp, "script.txt");
    const wordsPath = path.join(temp, "words.json");
    const semanticsPath = path.join(temp, "semantics.json");
    const outputPath = path.join(temp, "output.ass");
    const metadataPath = path.join(temp, "output.metadata.json");
    fs.writeFileSync(scriptPath, "新疆算力。算力发展，是我们的金句。", "utf8");
    fs.writeFileSync(wordsPath, JSON.stringify(words("新疆算力算力发展是我们的金句", 0, 0.2)), "utf8");
    fs.writeFileSync(semanticsPath, JSON.stringify({
      keywords: ["新疆"],
      punchLines: ["算力发展，是我们的金句"],
    }), "utf8");

    execFileSync(process.execPath, ["-r", "ts-node/register", path.join(root, "scripts", "make-ass.ts"),
      "--script", scriptPath, "--words", wordsPath, "--semantics", semanticsPath,
      "--output", outputPath, "--metadata", metadataPath,
      "--preset", "sub-modern-clean-v1", "--speed", "1", "--lead", "0",
    ], { cwd: root });

    const ass = fs.readFileSync(outputPath, "utf8");
    assert.match(ass, /Style: Default,Alimama ShuHeiTi Bold,72/);
    assert.match(ass, /Style: Punch,Alimama ShuHeiTi Bold,82/);
    assert.match(ass, /Dialogue:.*Default.*\{\\c&H003794FD&\}新疆\{\\c&H00FFFFFF&\}/);
    assert.match(ass, /Dialogue:.*Punch.*算力发展，(?:\\N)?是我们的金句。/);
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});

test("explicit CLI fails closed on malformed subtitle semantics before output writes", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "subtitle-invalid-semantics-"));
  try {
    const scriptPath = path.join(temp, "script.txt");
    const wordsPath = path.join(temp, "words.json");
    const semanticsPath = path.join(temp, "semantics.json");
    const outputPath = path.join(temp, "output.ass");
    const metadataPath = path.join(temp, "output.metadata.json");
    fs.writeFileSync(scriptPath, "新疆", "utf8");
    fs.writeFileSync(wordsPath, JSON.stringify(words("新疆")), "utf8");
    fs.writeFileSync(semanticsPath, JSON.stringify({ keywords: "新疆", punchLines: [] }), "utf8");

    const result = spawnSync(process.execPath, ["-r", "ts-node/register", path.join(root, "scripts", "make-ass.ts"),
      "--script", scriptPath, "--words", wordsPath, "--semantics", semanticsPath,
      "--output", outputPath, "--metadata", metadataPath,
    ], { cwd: root, encoding: "utf8" });

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /semantics.*keywords.*punchLines/i);
    assert.equal(fs.existsSync(outputPath), false);
    assert.equal(fs.existsSync(metadataPath), false);
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});

test("explicit CLI requires semantics before creating subtitle outputs", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "subtitle-missing-semantics-"));
  try {
    const scriptPath = path.join(temp, "script.txt");
    const wordsPath = path.join(temp, "words.json");
    const outputPath = path.join(temp, "output.ass");
    const metadataPath = path.join(temp, "output.metadata.json");
    fs.writeFileSync(scriptPath, "新疆", "utf8");
    fs.writeFileSync(wordsPath, JSON.stringify(words("新疆")), "utf8");

    const result = spawnSync(process.execPath, ["-r", "ts-node/register", path.join(root, "scripts", "make-ass.ts"),
      "--script", scriptPath, "--words", wordsPath,
      "--output", outputPath, "--metadata", metadataPath,
    ], { cwd: root, encoding: "utf8" });

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /--semantics/);
    assert.equal(fs.existsSync(outputPath), false);
    assert.equal(fs.existsSync(metadataPath), false);
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});

test("positional invocation preserves preset, lead, speed, and replace-mode modifiers", () => {
  const options = parseMakeAssOptions([
    "demo", "1.1", "--preset", "sub-modern-clean-v1", "--lead", "0.2",
    "--mode", "replace",
  ]);

  assert.equal(options.preset, "sub-modern-clean-v1");
  assert.equal(options.lead, 0.2);
  assert.equal(options.speed, 1.1);
});

test("rejects mixed positional and explicit invocation before creating subtitle outputs", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "subtitle-mixed-invocation-"));
  try {
    const scriptPath = path.join(temp, "script.txt");
    const wordsPath = path.join(temp, "words.json");
    const outputPath = path.join(temp, "output.ass");
    const metadataPath = path.join(temp, "output.metadata.json");
    fs.writeFileSync(scriptPath, "新疆", "utf8");
    fs.writeFileSync(wordsPath, JSON.stringify(words("新疆")), "utf8");

    assert.throws(
      () => parseMakeAssOptions(["dummy", "--script", scriptPath, "--words", wordsPath]),
      /位置参数.*显式参数.*混用/,
    );
    const result = spawnSync(process.execPath, ["-r", "ts-node/register", path.join(root, "scripts", "make-ass.ts"),
      "dummy", "--script", scriptPath, "--words", wordsPath,
      "--output", outputPath, "--metadata", metadataPath,
    ], { cwd: root, encoding: "utf8" });

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /位置参数.*显式参数.*混用/);
    assert.equal(fs.existsSync(outputPath), false);
    assert.equal(fs.existsSync(metadataPath), false);
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});

test("renders the full real Xinjiang legacy script with all three newline-separated punch lines", () => {
  const scriptText = fs.readFileSync(path.join(fixtureRoot, "xinjiang-real.script.txt"), "utf8").replace(/\r/g, "");
  const semantics = JSON.parse(fs.readFileSync(
    path.join(fixtureRoot, "xinjiang-real.semantics.json"), "utf8",
  )) as { keywords: string[]; punchLines: string[] };
  const spoken = Array.from(scriptText).filter((character) => /[\p{L}\p{N}%]/u.test(character)).join("");

  const result = buildAssDocument({
    scriptText,
    words: [{ w: spoken, start: 0, end: Array.from(spoken).length * 0.2 }],
    preset: "sub-legacy-heavy-v1",
    speed: 1,
    lead: 0,
    keywords: semantics.keywords,
    punchLines: semantics.punchLines,
  });

  const punches = result.metadata.cues.filter((cue) => cue.style === "Punch");
  assert.equal(punches.length, 3);
  assert.deepEqual(
    punches.map((cue) => cue.text.replace(/\\N/g, "")),
    semantics.punchLines,
  );
  assert.ok(result.metadata.cues
    .filter((cue) => cue.style !== "Punch")
    .every((cue) => !cue.text.includes("\\N")));
  assert.equal(dialogueLines(result.ass).filter((line) => line.includes(",Punch,")).length, 3);
});
