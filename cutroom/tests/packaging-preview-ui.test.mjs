import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPackagingPreviewModel,
  canConfirmPackaging,
  slotRows,
} from "../web-ui/public/packaging-preview.mjs";
import { buildCatalogCheckModel } from "../web-ui/public/packaging-catalog.mjs";

const CATALOG = {
  categories: [
    { key: "subtitle", assets: [{ assetRef: "sub-a", name: "粗宋字幕" }] },
    { key: "bgm", assets: [{ assetRef: "bgm-a", name: "稳重 BGM" }] },
    { key: "sfx", assets: [{ assetRef: "sfx-a", name: "提示音" }] },
  ],
};

test("confirm is enabled only for the current viewed ready preview", () => {
  const current = {
    planDigest: "plan-a",
    pictureLockDigest: "picture-a",
    preview: {
      status: "ready",
      viewedAt: "2026-09-20T12:00:00Z",
      planDigest: "plan-a",
      pictureLockDigest: "picture-a",
    },
  };
  assert.equal(buildPackagingPreviewModel(current).canConfirm, true);
  assert.equal(canConfirmPackaging({ ...current, planDigest: "plan-b" }), false);
  assert.equal(
    canConfirmPackaging({ ...current, preview: { ...current.preview, viewedAt: null } }),
    false,
  );
});

test("slot rows include sfx and disable only optional slots", () => {
  const rows = slotRows(
    [{ slot: "subtitle", asset_ref: "sub-a", is_backup: false }],
    CATALOG,
  );
  assert.equal(rows.find((row) => row.key === "sfx").label, "音效");
  assert.equal(rows.find((row) => row.key === "subtitle").canDisable, false);
  assert.equal(rows.find((row) => row.key === "bgm").canDisable, true);
  assert.equal(rows.find((row) => row.key === "subtitle").selected.name, "粗宋字幕");
});

test("preview model exposes real media and explicit non-final disclaimer", () => {
  const model = buildPackagingPreviewModel({
    taskId: "task-1",
    planDigest: "plan-a",
    pictureLockDigest: "picture-a",
    preview: {
      previewId: "pv-1",
      status: "rendering",
      coverage: [{ slot: "bgm", status: "rendered" }],
    },
  });
  assert.equal(
    model.mediaUrl,
    "/api/tasks/task-1/packaging-previews/pv-1/media",
  );
  assert.match(model.disclaimer, /不是最终成片/);
  assert.equal(model.canConfirm, false);
});

test("catalog check orders problems before unchanged and preserves relative paths", () => {
  const model = buildCatalogCheckModel({
    counts: { added: 1, updated: 1, missing: 1, invalid: 1, unchanged: 1 },
    items: [
      { status: "unchanged", relativePath: "a.png" },
      { status: "added", relativePath: "b.png" },
      { status: "invalid", relativePath: "c.png" },
      { status: "missing", relativePath: "d.png" },
      { status: "updated", relativePath: "e.png" },
    ],
  });
  assert.deepEqual(model.items.map((item) => item.status), [
    "invalid", "missing", "updated", "added", "unchanged",
  ]);
  assert.equal(model.counts.invalid, 1);
});
