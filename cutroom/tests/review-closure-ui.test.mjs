import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildCandidateHistory,
  buildPublishPackageView,
  buildReworkDraft,
  groupQaIssues,
  materialAuthorizationAction,
  materialAuthorizationSetup,
  reviewGate,
  reviewRenderAction,
} from "../web-ui/public/review-closure.mjs";
import {
  buildCoverCandidates,
  normalizePublishDraft,
} from "../web-ui/public/publish-controls.mjs";


test("QA blocked candidate is visible but cannot start or approve review", () => {
  const candidate = {
    candidateId: "c1",
    versionLabel: "candidate-v1",
    status: "qa_blocked",
    qa: {
      blockingCount: 1,
      warningCount: 0,
      issues: [
        { severity: "blocking", check: "black", detail: "黑帧", startSeconds: 3 },
      ],
    },
  };
  assert.deepEqual(reviewGate(candidate, null, {}), {
    canStartWatch: false,
    canApprove: false,
    blockingReason: "QA_BLOCKED",
  });
  assert.equal(groupQaIssues(candidate).blocking.length, 1);
});

test("old candidate stays read-only and uses versioned media URL", () => {
  const history = buildCandidateHistory({
    currentCandidate: { candidateId: "c2" },
    candidateVersions: [
      { candidateId: "c2", versionLabel: "candidate-v2", mediaUrl: "/c2" },
      { candidateId: "c1", versionLabel: "candidate-v1", mediaUrl: "/c1" },
    ],
  }, "c1");
  assert.equal(history.selected.readOnly, true);
  assert.equal(history.selected.mediaUrl, "/c1");
  assert.equal(history.current.candidateId, "c2");
});

test("rework draft carries category position and explicit execute semantics", () => {
  assert.deepEqual(buildReworkDraft({ candidateId: "c2" }, {
    category: "subtitle",
    notes: "字幕慢半拍",
    positionSeconds: 12.5,
  }), {
    candidateId: "c2",
    category: "subtitle",
    notes: "字幕慢半拍",
    positionSeconds: 12.5,
  });
  assert.throws(
    () => buildReworkDraft({ candidateId: "c2" }, { category: "subtitle", notes: "" }),
    /REWORK_NOTES_REQUIRED/,
  );
});

test("unknown QA severities become warnings and passed checks remain separate", () => {
  const groups = groupQaIssues({
    qa: { issues: [
      { severity: "future", check: "one", detail: "复核" },
      { severity: "info", check: "two", detail: "通过", ok: true },
    ] },
  });
  assert.equal(groups.warning.length, 1);
  assert.equal(groups.passed.length, 1);
});

test("publish package view labels stale candidate packages", () => {
  const view = buildPublishPackageView({
    recordId: "p1",
    candidateId: "c1",
    candidateLabel: "candidate-v1",
    packageStatus: "expired",
    packageComplete: true,
    files: Array.from({ length: 7 }, (_, index) => `f-${index}`),
  }, "c2");
  assert.equal(view.expired, true);
  assert.equal(view.current, false);
  assert.match(view.status, /已过期/);
});

test("publish form keeps tags cover mode and delivery type", () => {
  const value = normalizePublishDraft({
    platform: "视频号",
    title: " 产业更新 ",
    tags: "招商, 产业，园区,招商",
    coverChoice: "candidate-2",
    deliveryType: "editable",
  });
  assert.deepEqual(value.tags, ["招商", "产业", "园区"]);
  assert.equal(value.title, "产业更新");
  assert.equal(value.coverChoice, "candidate-2");
  assert.equal(value.deliveryType, "editable");
});

test("cover candidates expose actual candidate frames and one selected state", () => {
  const covers = buildCoverCandidates(
    { mediaUrl: "/media/candidate.mp4", duration: 100 },
    "candidate-2",
  );
  assert.deepEqual(covers.map((item) => item.time), [15, 50, 85]);
  assert.equal(covers.filter((item) => item.selected).length, 1);
  assert.ok(covers.every((item) => item.mediaUrl === "/media/candidate.mp4"));
});

test("editable package view uses the dynamic manifest count", () => {
  const view = buildPublishPackageView({
    recordId: "p-editable",
    candidateId: "c2",
    candidateLabel: "candidate-v2",
    packageStatus: "current",
    packageComplete: true,
    coverChoice: "candidate-3",
    deliveryType: "editable",
    files: Array.from({ length: 12 }, (_, index) => `f-${index}`),
  }, "c2");
  assert.equal(view.complete, true);
  assert.equal(view.deliveryType, "editable");
  assert.match(view.status, /12 个文件/);
});

test("publish workspace wires labeled controls native cover picker and busy feedback", () => {
  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  assert.match(app, /buildPublishFormValue/);
  assert.match(app, /buildCoverCandidates/);
  assert.match(app, /\/api\/picker\/image/);
  for (const label of ["发布平台", "发布标题", "发布说明", "发布标签", "封面选择", "交付类型"]) {
    assert.match(app, new RegExp(label));
  }
  assert.match(app, /setButtonBusy\(submit, true/);
  assert.match(app, /aria-live/);
});

test("review workspace exposes four sections and distinct rework actions", () => {
  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  for (const heading of ["精剪候选版本", "自动 QA", "完整审片", "发布包"]) {
    assert.match(app, new RegExp(heading));
  }
  assert.match(app, /\/review\/rework\/preview/);
  assert.match(app, /保存返工单/);
  assert.match(app, /确认返工并回到对应阶段/);
  assert.match(app, /aria-busy/);
  assert.match(app, /const saveIdempotencyKey/);
  assert.match(app, /category\.disabled = true/);
  assert.match(app, /notes\.disabled = true/);
  assert.match(app, /position\.disabled = true/);
  assert.match(app, /\/material-authorization/);
});

test("review workspace offers a new candidate or QA retry after the first version", () => {
  assert.deepEqual(
    reviewRenderAction({ workflow: { current_stage: "fine_render" } }, { candidateId: "c1" }),
    { visible: true, label: "生成新候选并自动质检" },
  );
  assert.deepEqual(
    reviewRenderAction(
      { workflow: { current_stage: "auto_qa" } },
      { candidateId: "c1", status: "qa_blocked" },
    ),
    { visible: true, label: "重试自动 QA" },
  );
});

test("material authorization blocker exposes one explicit attestation action", () => {
  const candidate = {
    candidateId: "c1",
    isCurrent: true,
    qa: {
      issues: [{
        severity: "blocking",
        check: "material_authorization",
        ok: false,
        detail: "镜头素材授权未明确通过",
      }],
    },
  };
  assert.deepEqual(
    materialAuthorizationAction({
      materialAuthorization: { approved: false, assetIds: ["a", "b"] },
    }, candidate),
    {
      visible: true,
      label: "仅确认本片 2 个镜头素材可商用",
      selfProducedLabel: "确认 2 个镜头素材为团队自摄并复用",
    },
  );
  assert.equal(
    materialAuthorizationAction({
      materialAuthorization: { approved: true, assetIds: ["a", "b"] },
    }, candidate).visible,
    false,
  );
});

test("locked self-produced shots can be declared before the first render", () => {
  assert.deepEqual(materialAuthorizationSetup({
    materialAuthorization: { approved: false, assetIds: ["a", "b"] },
  }), {
    visible: true,
    label: "仅确认本片 2 个镜头素材可商用",
    selfProducedLabel: "确认 2 个镜头素材为团队自摄并复用",
  });
});
