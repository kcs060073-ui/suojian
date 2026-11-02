import assert from "node:assert/strict";
import test from "node:test";

import {
  WORKFLOW_PHASES,
  WORKFLOW_STAGES,
  buildStagePresentation,
  groupWorkflowSteps,
  resolveWorkflowViewStage,
} from "../web-ui/public/workflow-view.mjs";
import {
  describeQaIssue,
  qaIssueSeekSeconds,
} from "../web-ui/public/qa-view.mjs";

test("all twelve steps have a distinct plain-language task and outcome", () => {
  assert.equal(WORKFLOW_STAGES.length, 12);
  const descriptions = WORKFLOW_STAGES.map((stage) =>
    buildStagePresentation({ current_stage: stage.key }, stage.key));
  assert.equal(new Set(descriptions.map((item) => item.instruction)).size, 12);
  assert.ok(descriptions.every((item) => item.number > 0 && item.outcome && item.phase));
  assert.match(descriptions[1].instruction, /文案/);
  assert.match(descriptions[9].instruction, /阻断/);
});

test("workflow navigation groups every step once and identifies the active phase", () => {
  const steps = WORKFLOW_STAGES.map((stage, index) => ({
    ...stage,
    number: index + 1,
    current: stage.key === "auto_qa",
    viewing: stage.key === "auto_qa",
    stageStatus: index < 9 ? "completed" : "not_started",
  }));
  const groups = groupWorkflowSteps(steps);
  assert.equal(groups.length, WORKFLOW_PHASES.length);
  assert.deepEqual(groups.flatMap((group) => group.steps.map((step) => step.key)),
    WORKFLOW_STAGES.map((step) => step.key));
  assert.equal(groups.filter((group) => group.active).length, 1);
  assert.equal(groups.find((group) => group.active).key, "finish");
});

test("completed tasks open at the last saved step", () => {
  const workflow = {
    current_stage: null,
    stages: Object.fromEntries(WORKFLOW_STAGES.map((stage) => [stage.key, { status: "completed" }])),
  };
  assert.equal(resolveWorkflowViewStage(WORKFLOW_STAGES, workflow), "publish_package");
});

test("QA issue presentation is readable and never mistakes absent time for zero", () => {
  const issue = {
    check: "material_authorization",
    detail: Array.from({ length: 8 }, (_, index) =>
      `镜头素材授权未明确通过：ast_${index}/unknown`).join("；"),
  };
  const view = describeQaIssue(issue);
  assert.equal(view.title, "素材授权待核对");
  assert.match(view.summary, /8 个镜头/);
  assert.doesNotMatch(view.summary, /ast_/);
  assert.equal(qaIssueSeekSeconds(issue), null);
  assert.equal(qaIssueSeekSeconds({ startSeconds: 0 }), 0);
  assert.equal(qaIssueSeekSeconds({ startSeconds: "5.2" }), 5.2);
});
