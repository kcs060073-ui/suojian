import * as assert from "node:assert/strict";
import { test } from "node:test";

import { parseStructuredOutputWithSingleRepair } from "../scripts/model-json-output";

type Example = { title: string; items: string[] };

function parseExample(value: unknown): Example {
  assert.equal(typeof value, "object");
  assert.ok(value !== null);
  const record = value as Record<string, unknown>;
  assert.equal(typeof record.title, "string");
  assert.ok(Array.isArray(record.items));
  assert.ok(record.items.every((item) => typeof item === "string"));
  return { title: record.title as string, items: record.items as string[] };
}

test("valid structured output is accepted without a repair request", async () => {
  let repairCalls = 0;
  const parsed = await parseStructuredOutputWithSingleRepair(
    '```json\n{"title":"新疆","items":["园区"]}\n```',
    parseExample,
    async () => {
      repairCalls += 1;
      return "{}";
    },
  );

  assert.deepEqual(parsed, { title: "新疆", items: ["园区"] });
  assert.equal(repairCalls, 0);
});

test("malformed model JSON is repaired once and then strictly validated", async () => {
  let repairCalls = 0;
  const parsed = await parseStructuredOutputWithSingleRepair(
    '{"title":"抢到"东数西算"风口","items":["园区"]}',
    parseExample,
    async (invalid) => {
      repairCalls += 1;
      assert.match(invalid, /东数西算/);
      return '{"title":"抢到\\"东数西算\\"风口","items":["园区"]}';
    },
  );

  assert.deepEqual(parsed, { title: '抢到"东数西算"风口', items: ["园区"] });
  assert.equal(repairCalls, 1);
});

test("a second malformed response fails closed without another repair loop", async () => {
  let repairCalls = 0;
  await assert.rejects(
    parseStructuredOutputWithSingleRepair(
      '{"title":"坏"引号","items":[]}',
      parseExample,
      async () => {
        repairCalls += 1;
        return '{"title":"仍然"坏","items":[]}';
      },
    ),
    /修复后仍不是有效的预期 JSON 结构/,
  );
  assert.equal(repairCalls, 1);
});
