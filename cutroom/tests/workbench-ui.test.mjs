import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  BOARD_LANES,
  buildTaskCenter,
  createPreferenceStore,
  taskDestination,
} from "../web-ui/public/state.mjs";
import * as workflowView from "../web-ui/public/workflow-view.mjs";
const {
  WORKFLOW_STAGES,
  buildWorkflowSteps,
  renderWorkflow,
} = workflowView;
import {
  CREATE_FIELD_ORDER,
  ROUTE_OPTIONS,
  buildCreateRequest,
  createTaskDraftStore,
  createStepIndex,
  initializeWorkbenchPage,
} from "../web-ui/public/task-create.mjs";
import {
  PACKAGING_CATEGORIES,
  PACKAGING_GROUPS,
  buildLibraryRegistration,
  filterPackagingCategories,
  groupShotLibraries,
} from "../web-ui/public/libraries-view.mjs";
import {
  availabilityLabel,
  groupModelProfiles,
  normalizeCapabilities,
  providerSupportsCapability,
  providerPresetDefinition,
  selectableProfiles,
  voiceOptionPresentation,
} from "../web-ui/public/settings-view.mjs";
import * as settingsView from "../web-ui/public/settings-view.mjs";
import * as taskWorkspace from "../web-ui/public/task-workspace.mjs";
const {
  AI_ACTIONS,
  buildRecoveryWorkspace,
  buildStoryboardActionState,
  buildWorkspaceSummary,
  roughCutDescription,
  workspaceKind,
} = taskWorkspace;
import {
  buildSceneEdit,
  selectScene,
} from "../web-ui/public/scene-editor.mjs";
import {
  advancePlayhead,
  mediaVersionsForCards,
  moveCardId,
  pictureLockBlockers,
  sceneAtPlayhead,
  sourceTimeAtPlayhead,
  trimBounds,
} from "../web-ui/public/rough-preview.mjs";
import {
  buildPackagingWorkspace,
  partitionRecommendedPlans,
} from "../web-ui/public/packaging-editor.mjs";
import {
  buildReviewWorkspace,
  createPlaybackWatchController,
  createWatchReporter,
  nextWatchReport,
  reviewMediaUrl,
} from "../web-ui/public/review-view.mjs";
import {
  buildPublishPackageView,
} from "../web-ui/public/publish-view.mjs";
import {
  buildTaskSearchResult,
  filterAndSortTasks,
} from "../web-ui/public/task-center-controls.mjs";
import {
  buildPreflightView,
} from "../web-ui/public/preflight-view.mjs";
import {
  buildMaterialIndexQuery,
  buildMaterialIndexView,
} from "../web-ui/public/material-index.mjs";
import {
  buildStoryboardFeedbackView,
} from "../web-ui/public/storyboard-feedback.mjs";
import { buildJobStatus, buildServiceJobSummary } from "../web-ui/public/job-status.mjs";

const TASKS = [
  { taskId: "task-awaiting", title: "新疆数字人 v2", route: "digital_human", currentStage: "rough_cut", runStatus: "awaiting_user", nextAction: "确认粗剪画面", updatedAt: "2026-09-07T08:00:00Z", gates: { brief_locked: true } },
  { taskId: "task-running", title: "自动分析任务", route: "digital_human", currentStage: "material_sync", runStatus: "running", nextAction: "等待素材同步完成", updatedAt: "2026-09-07T07:00:00Z", gates: {} },
  { taskId: "task-queued", title: "排队任务", route: "digital_human", currentStage: "fine_render", runStatus: "queued", nextAction: "等待前序任务完成", updatedAt: "2026-09-07T06:00:00Z", gates: {} },
  { taskId: "task-failed", title: "失败任务", route: "digital_human", currentStage: "auto_qa", runStatus: "failed", nextAction: "查看失败原因", updatedAt: "2026-09-07T05:00:00Z", gates: {} },
  { taskId: "task-complete", title: "完成任务", route: "digital_human", currentStage: "publish_package", runStatus: "completed", nextAction: "查看发布包", updatedAt: "2026-09-07T04:00:00Z", gates: {} },
];

test("粗剪帮助文案按数字人和信息流路线分开", () => {
  assert.equal(
    roughCutDescription("digital_human"),
    "只能调整镜头级画面，不会切断或删除连续的数字人口播主轴。",
  );
  assert.equal(
    roughCutDescription("ad_mix"),
    "可以调整逐句镜头的画面、顺序和时间边界，不会改写已锁定的广告文案和配音。",
  );
});

test("job status distinguishes pause requested paused and unknown ETA", () => {
  const pauseRequested = buildJobStatus({
    status: "running",
    pauseRequested: true,
    estimateStatus: "estimated",
    estimatedRemainingSeconds: 90,
    observedAt: "2026-09-22T01:00:00Z",
  });
  const paused = buildJobStatus({
    status: "paused",
    pauseRequested: false,
    estimateStatus: "unknown",
    observedAt: "2026-09-22T01:00:00Z",
  });

  assert.match(pauseRequested.statusDetail, /安全步骤/);
  assert.equal(paused.statusLabel, "已暂停");
  assert.equal(paused.remainingLabel, "剩余时间待估算");
  assert.equal(paused.ariaLive, "polite");
});

test("service job summary exposes phase queue pause and ETA without color-only status", () => {
  const running = buildServiceJobSummary([{
    status: "running",
    phase: "渲染镜头 4",
    pauseRequested: true,
    estimateStatus: "estimated",
    estimatedRemainingSeconds: 90,
  }]);
  assert.match(running.label, /渲染镜头 4/);
  assert.match(running.label, /安全步骤结束后暂停/);
  assert.match(running.label, /预计剩余/);

  const queued = buildServiceJobSummary([{
    status: "queued", phase: "等待精剪", queuePosition: 3,
    estimateStatus: "unknown",
  }]);
  assert.match(queued.label, /队列第 3 位/);
  assert.match(queued.label, /待估算/);
});

test("task controls search title/id, filter route/status, and sort newest", () => {
  const fixture = [
    ...TASKS,
    { taskId: "ad-02", title: "秋季招商广告", route: "ad_mix", runStatus: "queued", updatedAt: "2026-09-08T08:00:00Z" },
    { taskId: "ad-01", title: "春季招商广告", route: "ad_mix", runStatus: "completed", updatedAt: "2026-09-06T08:00:00Z" },
  ];
  const visible = filterAndSortTasks(fixture, {
    query: "ad-02", route: "ad_mix", status: "queued", sort: "updated_desc",
  });
  assert.deepEqual(visible.map((item) => item.taskId), ["ad-02"]);
});

test("task controls preserve input and return an actionable empty state", () => {
  const before = structuredClone(TASKS);
  const result = buildTaskSearchResult(TASKS, { query: "不存在" });
  assert.deepEqual(result.tasks, []);
  assert.match(result.emptyMessage, /清除筛选|换个关键词/);
  assert.deepEqual(TASKS, before);
});

test("preflight view keeps unknown values and exposes all four confirmations", () => {
  const view = buildPreflightView({
    estimated_space_bytes: 1073741824,
    estimates: {
      machine_seconds: {
        status: "unavailable", min: null, max: null, unit: "seconds",
        basis: null, reason: "尚无适用的本机处理耗时记录",
      },
      human_seconds: {
        status: "estimated", min: 120, max: 600, unit: "seconds",
        basis: "四个人工确认点", reason: null,
      },
      api_cost: {
        status: "unavailable", min: null, max: null, unit: null,
        reason: "调用前按供应商配置再次确认",
        included_costs: ["AI 分镜文本模型"], excluded_costs: ["缺失画面视频生成"],
      },
    },
    queue: {
      heavy_slot: "unknown", waiting_count: null, observed_at: "2026-09-22T01:00:00Z",
      estimated_wait_seconds: {
        status: "unavailable", min: null, max: null, unit: "seconds",
        reason: "队列样本不足",
      },
    },
    confirmations: [
      { key: "brief", label: "文案锁定" },
      { key: "picture", label: "画面锁定" },
      { key: "packaging", label: "包装确认" },
      { key: "review", label: "完整审片" },
    ],
    safety_notices: ["核对真实性", "检查隐私", "确认授权"],
  });
  assert.equal(view.metrics.machineTime.value, "暂无法估算");
  assert.equal(view.metrics.machineTime.status, "unknown");
  assert.equal(view.metrics.disk.value, "1 GB");
  assert.equal(view.confirmations.length, 4);
  assert.match(view.notices.join(" "), /真实性/);
  assert.match(view.notices.join(" "), /隐私/);
  assert.match(view.notices.join(" "), /授权/);
  assert.match(view.costScope.excluded.join(" "), /缺失画面/);
});

test("material index query preserves task scope and all filters", () => {
  assert.equal(
    buildMaterialIndexQuery({
      query: "园区 门头",
      libraryId: "lib/primary",
      mediaKind: "video",
      availability: "offline",
      authorization: "unknown",
      limit: 24,
      offset: 48,
    }, "task/1"),
    "/api/assets?taskId=task%2F1&query=%E5%9B%AD%E5%8C%BA+%E9%97%A8%E5%A4%B4&libraryId=lib%2Fprimary&mediaKind=video&availability=offline&authorization=unknown&limit=24&offset=48",
  );
});

test("material index keeps offline records visible and makes them non-previewable", () => {
  const view = buildMaterialIndexView({
    total: 2,
    limit: 24,
    offset: 0,
    items: [
      {
        assetId: "offline-1",
        relativePath: "航拍.mp4",
        mediaKind: "video",
        availability: "offline",
        online: false,
        previewUrl: null,
        authorizationStatus: "unknown",
      },
      {
        assetId: "online-1",
        relativePath: "门头.mp4",
        mediaKind: "video",
        availability: "available",
        online: true,
        previewUrl: "/preview/online-1",
        authorizationStatus: "unknown",
      },
    ],
  });

  assert.equal(view.countLabel, "共 2 项素材");
  assert.equal(view.items[0].canPreview, false);
  assert.match(view.items[0].availabilityLabel, /离线/);
  assert.match(view.items[0].authorizationLabel, /未知|待核验/);
  assert.equal(view.items[1].canPreview, true);
  assert.equal(view.hasPrevious, false);
  assert.equal(view.hasNext, false);
});

test("material index controls are wired to the read-only asset endpoint", () => {
  const html = readFileSync(new URL("../web-ui/public/index.html", import.meta.url), "utf8");
  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  assert.match(html, /id="material-search"[^>]*type="search"/);
  assert.match(html, /id="material-kind-filter"/);
  assert.match(html, /id="material-library-filter"/);
  assert.match(html, /id="material-availability-filter"/);
  assert.match(html, /id="material-authorization-filter"/);
  assert.match(html, /id="material-index-results"[^>]*aria-live="polite"/);
  assert.match(app, /buildMaterialIndexQuery/);
  assert.match(app, /void loadMaterialIndex\(\)/);
  assert.match(app, /item\.canPreview/);
});

test("scene feedback exposes reject correction and apply as separate actions", () => {
  const view = buildStoryboardFeedbackView({
    feedbackId: "feedback-1",
    selectedAssetId: "day-factory",
    candidates: [{
      assetId: "day-factory",
      relativePath: "白天/厂房.mp4",
      sourceDirectory: "本次项目",
      reason: "纠正要求匹配：白天、厂房",
      warnings: ["授权状态尚未核验"],
      previewUrl: "/api/preview/day-factory",
      mediaKind: "video",
    }],
  });

  assert.equal(view.actions.map((item) => item.key).join(","), "reject,rerank,apply");
  assert.match(view.candidates[0].reason, /旁白|纠正要求/);
  assert.equal(view.candidates[0].selected, true);
  assert.equal(view.canApply, true);
});

test("供应商展示使用真实名称且模型配置名称可自动生成", () => {
  assert.equal(typeof settingsView.providerAccountPresentation, "function");
  assert.equal(typeof settingsView.suggestedModelProfileName, "function");
  assert.deepEqual(settingsView.providerAccountPresentation({
    name: "智谱1",
    preset: "openai_compatible",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    defaultModel: "glm-5.3",
  }), {
    name: "智谱1",
    detail: "OpenAI 兼容 · https://open.bigmodel.cn/api/paas/v4 · 默认 glm-5.3",
  });
  assert.equal(
    settingsView.suggestedModelProfileName("智谱1", "planning", "glm-5.3"),
    "智谱1-文案策划-glm-5.3",
  );
  assert.equal(
    settingsView.suggestedModelProfileName("方舟", "broll", "seedance-2"),
    "方舟-B-roll-seedance-2",
  );
});

test("供应商列表提供带确认的删除操作", () => {
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  assert.match(app, /button\("删除", "btn danger"\)/);
  assert.match(app, /confirm\(/);
  assert.match(app, /deleteProviderAccount/);
});

test("toast 错误反馈比普通提示停留更久", () => {
  assert.equal(typeof settingsView.toastDuration, "function");
  assert.ok(settingsView.toastDuration("info") >= 3000);
  assert.ok(
    settingsView.toastDuration("error") > settingsView.toastDuration("info"),
  );
});

test("豆包音色与配音配置的真实失败分支统一使用错误 toast", () => {
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  const functionBlock = (name, nextName) => {
    const start = app.indexOf(`async function ${name}`);
    const end = app.indexOf(nextName, start);
    assert.notEqual(start, -1, `missing function ${name}`);
    assert.notEqual(end, -1, `missing boundary after ${name}`);
    return app.slice(start, end);
  };
  assert.match(
    functionBlock("testSelectedTaskVoice", "function renderCreateLibraries"),
    /showToast\("试听已生成并开始播放。"\);[\s\S]*?catch \(error\) \{\s*showErrorToast\("试听失败：" \+ error\.message\);/,
  );
  assert.match(
    functionBlock("updateProviderCredentials", "function renderSettings"),
    /showErrorToast\("豆包凭据保存失败：" \+ error\.message\)/,
  );
  assert.match(
    functionBlock("testModelProfile", "async function testProfileVoice"),
    /showToast\(created \? "模型已保存并检测通过，可以用于新任务。" : "连接测试通过，该配置可以用于新任务。"\);[\s\S]*?catch \(error\) \{[\s\S]*?showErrorToast\(\(created \? "模型已保存，但检测未通过：" : "检测未通过："\) \+ error\.message\);/,
  );
  assert.match(
    functionBlock("testProfileVoice", "async function updateProfileDefaultVoice"),
    /showToast\("常用音色试听已生成。"\);[\s\S]*?catch \(error\) \{\s*showErrorToast\("音色试听失败：" \+ error\.message\);/,
  );
  assert.match(
    functionBlock("updateProfileDefaultVoice", "async function deleteModelProfile"),
    /showToast\("常用音色已更新；已有任务仍使用创建时冻结的音色。"\);[\s\S]*?catch \(error\) \{\s*showErrorToast\(error\.message\);/,
  );
  assert.match(
    app,
    /profile\.lastErrorCode === "MODEL_RESOURCE_NOT_GRANTED"[\s\S]*?https:\/\/console\.volcengine\.com\/speech\/service\/10[\s\S]*?去火山控制台开通服务/,
  );
});

test("豆包保存并检测按保存连接目录三阶段执行且持续显示结果", () => {
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  const start = app.indexOf("async function updateProviderCredentials");
  const end = app.indexOf("function ttsResourceLabel", start);
  const block = app.slice(start, end);
  assert.match(app, /providerCredentialChecks: \{\}/);
  assert.match(app, /checkingProviderCredentials: new Set\(\)/);
  assert.match(app, /function setButtonBusy\(button, busy, label\)/);
  assert.match(app, /"保存并检测"/);
  assert.match(app, /inlineFeedback\(check\.title, check\.detail, check\.kind\)/);
  assert.match(app, /result\.setAttribute\("role", "status"\)/);
  assert.match(app, /result\.setAttribute\("aria-live", "polite"\)/);
  assert.match(block, /if \(state\.checkingProviderCredentials\.has\(accountId\)\) return;/);
  const saveIndex = block.indexOf("/credentials");
  const probeIndex = block.indexOf("/test");
  const refreshIndex = block.indexOf("/voices/refresh");
  assert.ok(saveIndex >= 0, "保存阶段必须调用 credentials");
  assert.ok(probeIndex > saveIndex, "连接检测必须发生在保存之后");
  assert.ok(refreshIndex > probeIndex, "音色目录刷新必须发生在连接检测之后");
  assert.match(block, /providerProfileForCredentialCheck\(accountId, values\.ttsResourceId\)/);
  assert.match(block, /生成一段很短的测试语音/);
  assert.match(block, /state\.checkingProviderCredentials\.delete\(accountId\)/);
});

test("toast 使用固定覆盖层且不被普通面板遮挡", () => {
  const styles = readFileSync(
    new URL("../web-ui/public/styles.css", import.meta.url),
    "utf8",
  );
  const toastRule = styles.match(/\.toast\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(toastRule, /position:\s*fixed/);
  const zIndex = Number(toastRule.match(/z-index:\s*(\d+)/)?.[1] || 0);
  assert.ok(zIndex >= 1000);
});

test("方案 A 使用统一语义令牌与可复用界面组件", () => {
  const css = readFileSync(
    new URL("../web-ui/public/styles.css", import.meta.url),
    "utf8",
  );
  for (const token of [
    "--color-canvas",
    "--color-surface",
    "--color-surface-subtle",
    "--color-text",
    "--color-text-muted",
    "--color-accent",
    "--space-1",
    "--space-2",
    "--space-3",
    "--space-4",
    "--space-6",
    "--space-8",
    "--radius-control",
    "--radius-card",
    "--shadow-overlay",
    "--motion-fast",
  ]) {
    assert.match(css, new RegExp(token.replace("--", "\\\-\\\-") + ":"));
  }
  assert.match(css, /\.btn\s*\{[\s\S]*?min-height:\s*44px/);
  assert.match(css, /\.field\s*\{[\s\S]*?min-height:\s*44px/);
  assert.match(css, /\.section-card\s*\{/);
  assert.match(css, /\.status-badge\s*\{/);
  assert.match(css, /\.inline-feedback\s*\{/);
  assert.match(css, /:focus-visible\s*\{/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
});

test("七个页面共享一致页头层级、操作区和状态语言", () => {
  const html = readFileSync(
    new URL("../web-ui/public/index.html", import.meta.url),
    "utf8",
  );
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  for (const view of [
    "tasks",
    "task-detail",
    "create",
    "library",
    "packlib",
    "settings",
    "settings-storage",
  ]) {
    assert.match(
      html,
      new RegExp('<section class="page" data-view="' + view + '"[^>]*aria-labelledby="[^"]+"'),
      view + " 缺少页面标题关联",
    );
  }
  assert.equal((html.match(/class="head page-header"/g) || []).length, 7);
  assert.match(html, /class="tasks-actions page-actions"/);
  assert.match(html, /class="run-badge status-badge"/);
  assert.match(html, /class="safe-badge status-badge success"/);
  assert.match(html, /class="settings-account-panel section-card"/);
  assert.match(app, /function statusBadge\(text, tone = "neutral"\)/);
  assert.match(app, /function inlineFeedback\(title, detail, tone = "neutral"\)/);
  assert.match(app, /inlineFeedback\(check\.title, check\.detail, check\.kind\)/);
});

test("发布页明确显示包目录、动态完整状态和交付选项", () => {
  const view = buildPublishPackageView({
    record_id: "pub-1",
    package_dir: "E:\\deliverables\\task-1",
    package_complete: true,
    files: [
      "final.mp4",
      "cover.png",
      "title.txt",
      "description.txt",
      "tags.txt",
      "qa-report.json",
      "manifest.json",
    ],
  });
  assert.deepEqual(view, {
    id: "pub-1",
    directory: "E:\\deliverables\\task-1",
    candidateId: "",
    candidateLabel: "未绑定候选",
    packageStatus: "current",
    status: "完整 · 7 个文件",
    complete: true,
    current: true,
    expired: false,
    coverChoice: "candidate-1",
    deliveryType: "standard",
    files: [
      "final.mp4",
      "cover.png",
      "title.txt",
      "description.txt",
      "tags.txt",
      "qa-report.json",
      "manifest.json",
    ],
  });
});

test("看板五列与列表共享同一批任务事实", () => {
  const center = buildTaskCenter(TASKS);
  assert.deepEqual(
    BOARD_LANES.map((lane) => lane.label),
    ["等待我处理", "自动处理中", "排队", "阻塞 / 失败", "最近完成"],
  );
  assert.deepEqual(
    center.lanes.map((lane) => lane.tasks.map((task) => task.taskId)),
    TASKS.map((task) => [task.taskId]),
  );
  assert.deepEqual(
    center.list.map((task) => task.taskId).sort(),
    TASKS.map((task) => task.taskId).sort(),
  );
});

test("看板与列表打开完全相同的任务地址", () => {
  for (const task of TASKS) {
    assert.equal(taskDestination(task, "board"), "#/tasks/" + task.taskId);
    assert.equal(taskDestination(task, "list"), "#/tasks/" + task.taskId);
  }
});

test("十二步完整且阶段状态与运行状态分开", () => {
  assert.equal(WORKFLOW_STAGES.length, 12);
  assert.deepEqual(
    WORKFLOW_STAGES.map((stage) => stage.label),
    [
      "创建与预检", "文案锁定", "素材增量同步", "AI分镜推荐", "粗剪",
      "调整画面顺序", "画面锁定", "包装方案确认", "精剪成片",
      "自动质检", "完整审片", "发布包",
    ],
  );
  const steps = buildWorkflowSteps(WORKFLOW_STAGES, {
    current_stage: "rough_cut",
    run_status: "queued",
    stages: { rough_cut: { status: "ready" } },
  });
  assert.equal(steps.find((step) => step.key === "rough_cut").stageStatus, "ready");
  assert.equal(steps.find((step) => step.key === "rough_cut").runStatus, "queued");
});

test("已完成阶段可只读回看，当前阶段保持不变且未来阶段不可进入", () => {
  const workflow = {
    current_stage: "fine_render",
    run_status: "awaiting_user",
    stages: Object.fromEntries(WORKFLOW_STAGES.map((stage, index) => [
      stage.key,
      { status: index < 8 ? "completed" : index === 8 ? "ready" : "not_started" },
    ])),
  };

  const steps = buildWorkflowSteps(WORKFLOW_STAGES, workflow, "packaging_confirm");
  assert.equal(steps.find((step) => step.key === "packaging_confirm").viewable, true);
  assert.equal(steps.find((step) => step.key === "packaging_confirm").viewing, true);
  assert.equal(steps.find((step) => step.key === "fine_render").current, true);
  assert.equal(steps.find((step) => step.key === "auto_qa").viewable, false);
  assert.equal(typeof workflowView.resolveWorkflowViewStage, "function");
  assert.equal(
    workflowView.resolveWorkflowViewStage(WORKFLOW_STAGES, workflow, "auto_qa"),
    "fine_render",
  );
});

test("阶段导航只触发允许回看的步骤并保留键盘按钮语义", () => {
  class FakeNode {
    constructor(tagName) {
      this.tagName = tagName;
      this.children = [];
      this.dataset = {};
      this.attributes = {};
      this.listeners = {};
      this.disabled = false;
      this.textContent = "";
      this.className = "";
    }

    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(type, listener) { this.listeners[type] = listener; }
    click() { if (!this.disabled) this.listeners.click?.(); }
  }

  const previousDocument = globalThis.document;
  globalThis.document = {
    createDocumentFragment: () => new FakeNode("fragment"),
    createElement: (tagName) => new FakeNode(tagName),
  };
  try {
    const selected = [];
    const container = new FakeNode("div");
    const workflow = {
      current_stage: "fine_render",
      stages: {
        packaging_confirm: { status: "completed" },
        fine_render: { status: "ready" },
        auto_qa: { status: "not_started" },
      },
    };
    renderWorkflow(container, workflow, {
      viewingStageKey: "packaging_confirm",
      onSelect: (stageKey) => selected.push(stageKey),
    });

    const phaseBar = container.children[0].children[0];
    const stepPanel = container.children[0].children[1];
    assert.equal(phaseBar.children.length, 4);
    const controls = stepPanel.children[0].children;
    const completed = controls.find((item) => item.dataset.stageKey === "packaging_confirm");
    const current = controls.find((item) => item.dataset.stageKey === "fine_render");
    const future = controls.find((item) => item.dataset.stageKey === "auto_qa");
    assert.equal(completed.tagName, "button");
    assert.equal(completed.attributes["aria-pressed"], "true");
    assert.equal(current.attributes["aria-current"], "step");
    assert.equal(future.disabled, true);
    completed.click();
    future.click();
    const deliveryPhase = phaseBar.children.find((item) => item.dataset.phaseKey === "deliver");
    deliveryPhase.click();
    assert.equal(stepPanel.children[0].children[0].dataset.stageKey, "publish_package");
    assert.equal(stepPanel.children[0].children[0].disabled, true);
    assert.deepEqual(selected, ["packaging_confirm"]);
  } finally {
    globalThis.document = previousDocument;
  }
});

test("历史阶段生成只读工作区上下文，未来阶段回落到当前工作区", () => {
  const detail = {
    workflow: {
      current_stage: "fine_render",
      stages: {
        packaging_confirm: { status: "completed" },
        fine_render: { status: "ready" },
        auto_qa: { status: "not_started" },
      },
    },
  };

  assert.equal(typeof taskWorkspace.buildWorkflowViewContext, "function");
  assert.deepEqual(taskWorkspace.buildWorkflowViewContext(detail, "packaging_confirm"), {
    actualStageKey: "fine_render",
    actualStageLabel: "精剪成片",
    viewingStageKey: "packaging_confirm",
    viewingStageLabel: "包装方案确认",
    workspace: "packaging",
    readOnly: true,
  });
  assert.deepEqual(taskWorkspace.buildWorkflowViewContext(detail, "auto_qa"), {
    actualStageKey: "fine_render",
    actualStageLabel: "精剪成片",
    viewingStageKey: "fine_render",
    viewingStageLabel: "精剪成片",
    workspace: "review",
    readOnly: false,
  });
});

test("主题、任务视图和侧栏偏好写入持久存储", () => {
  const memory = new Map();
  const storage = {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, value),
  };
  const preferences = createPreferenceStore(storage);
  preferences.setTheme("dark");
  preferences.setTaskView("list");
  preferences.setRailCollapsed(true);
  assert.deepEqual(createPreferenceStore(storage).read(), {
    theme: "dark",
    taskView: "list",
    railCollapsed: true,
  });
  assert.throws(() => preferences.setTheme("neon"), /theme/);
});

test("正式入口保留 V4 三栏结构且不包含模拟业务成功", () => {
  const html = readFileSync(
    new URL("../web-ui/public/index.html", import.meta.url),
    "utf8",
  );
  assert.match(html, /id="cutroom-workbench"/);
  assert.match(html, /class="shell"/);
  assert.match(html, /class="rail"/);
  assert.match(html, /class="taskboard"/);
  assert.match(html, /id="workspace"/);
  assert.match(html, /镜头素材/);
  assert.match(html, /包装素材/);
  assert.match(html, /跟随系统/);
  assert.doesNotMatch(html, /模拟完整观看|原型直接跳到|模拟素材盘离线/);
});

test("创建向导保持冻结顺序且园区路线仍不能继续", () => {
  assert.deepEqual(CREATE_FIELD_ORDER, [
    "route",
    "subtype",
    "digital_human_mp4",
    "final_script",
    "primary_library_id",
    "selected_generic_ids",
    "purpose",
    "target_duration_seconds",
    "preflight",
  ]);
  assert.equal(ROUTE_OPTIONS.find((item) => item.key === "digital_human").available, true);
  assert.equal(ROUTE_OPTIONS.find((item) => item.key === "park_video").available, false);
  assert.equal(ROUTE_OPTIONS.find((item) => item.key === "ad_mix").available, true);
  assert.deepEqual(
    buildCreateRequest({
      route: "digital_human",
      subtype: "zhaoshang",
      title: "新疆数字人",
      digital_human_mp4: "E:\\input\\xinjiang.mp4",
      final_script: "最终口播文案",
      primary_library_id: "park-xinjiang",
      selected_generic_ids: ["generic-industry"],
      include_default_generic: true,
      purpose: "视频号招商传播",
      target_duration_seconds: 90,
      orchestration_mode: "standalone",
      planning_profile_id: "profile-planning-a",
      broll_profile_id: "profile-broll-a",
    }),
    {
      route: "digital_human",
      subtype: "zhaoshang",
      title: "新疆数字人",
      digital_human_mp4: "E:\\input\\xinjiang.mp4",
      final_script: "最终口播文案",
      primary_library_id: "park-xinjiang",
      selected_generic_ids: ["generic-industry"],
      include_default_generic: true,
      purpose: "视频号招商传播",
      target_duration_seconds: 90,
      orchestration_mode: "standalone",
      model_profile_ids: {
        planning: "profile-planning-a",
        broll: "profile-broll-a",
      },
    },
  );
  assert.throws(
    () => buildCreateRequest({ route: "park_video" }),
    /尚未开放/,
  );
});

test("三类角色可保存多个模型配置并兼容归入旧配置", () => {
  const profiles = [
    {
      profileId: "planning-a",
      capability: "planning",
      displayName: "智谱策划",
      model: "glm-5.3",
      availabilityStatus: "available",
      isDefault: true,
    },
    {
      profileId: "legacy-copy",
      capability: "copywriting",
      displayName: "旧文案模型",
      model: "glm-5.3-flash",
      availabilityStatus: "untested",
      isDefault: false,
    },
    {
      profileId: "tts-a",
      capability: "tts",
      displayName: "女声",
      model: "tts-a",
      availabilityStatus: "available",
      isDefault: true,
    },
  ];
  const grouped = groupModelProfiles(profiles);
  assert.deepEqual(grouped.planning.map((item) => item.profileId), ["planning-a", "legacy-copy"]);
  assert.deepEqual(selectableProfiles(profiles, "planning").map((item) => item.profileId), ["planning-a"]);
  assert.equal(availabilityLabel("available"), "可用");
  assert.equal(availabilityLabel("untested"), "未检测");
});

test("配音配置只显示兼容账号且豆包音色使用可读名称", () => {
  assert.equal(providerSupportsCapability({ provider: "doubao_tts" }, "tts"), true);
  assert.equal(providerSupportsCapability({ provider: "volcengine_ark" }, "tts"), false);
  assert.equal(providerSupportsCapability({ provider: "openai_compatible", name: "兼容服务" }, "tts"), true);
  assert.equal(providerSupportsCapability({ provider: "volcengine_ark" }, "planning"), true);
  assert.deepEqual(voiceOptionPresentation({
    voiceId: "zh_male_yangguangqingnian_moon_bigtts",
    displayName: "阳光青年 2.0",
    gender: "男声",
    languages: ["中文"],
    styles: ["自然", "广告"],
  }), {
    label: "阳光青年 2.0",
    detail: "中文 · 男声 · 自然 / 广告",
  });
});

test("新建任务只选择文案策划、B-roll 和配音三类模型", () => {
  const request = buildCreateRequest({
    route: "digital_human",
    subtype: "zhaoshang",
    title: "独立模型任务",
    digital_human_mp4: "E:\\input\\video.mp4",
    final_script: "最终文案",
    primary_library_id: "park",
    selected_generic_ids: [],
    include_default_generic: true,
    purpose: "测试",
    target_duration_seconds: 60,
    planning_profile_id: "profile-planning",
    broll_profile_id: "profile-broll",
  });

  assert.deepEqual(request.model_profile_ids, {
    planning: "profile-planning",
    broll: "profile-broll",
  });
  assert.equal("general_text" in request.model_profile_ids, false);

  const html = readFileSync(
    new URL("../web-ui/public/index.html", import.meta.url),
    "utf8",
  );
  assert.match(html, /name="planning_profile_id"/);
  assert.match(html, /name="broll_profile_id"/);
  assert.match(html, /name="tts_profile_id"/);
  assert.doesNotMatch(html, /name="copywriting_profile_id"/);
  assert.doesNotMatch(html, /name="storyboard_profile_id"/);
  assert.doesNotMatch(html, /name="packaging_profile_id"/);
  assert.doesNotMatch(html, /name="general_text_profile_id"/);
});

test("Agent 创建任务不提交规划模型，但保留配音和 B-roll 配置", () => {
  const request = buildCreateRequest({
    route: "ad_mix",
    orchestration_mode: "agent",
    title: "Agent 广告任务",
    final_script: "最终广告文案",
    primary_library_id: "park",
    selected_generic_ids: [],
    include_default_generic: true,
    purpose: "测试",
    target_duration_seconds: 30,
    aspect_ratio: "9:16",
    voice_mode: "ai_tts",
    planning_profile_id: "profile-planning",
    tts_profile_id: "profile-tts",
    tts_voice_id: "voice-one",
    broll_profile_id: "profile-broll",
  });

  assert.equal(request.orchestration_mode, "agent");
  assert.deepEqual(request.model_profile_ids, {
    broll: "profile-broll",
    tts: "profile-tts",
  });
});

test("Agent 分镜阶段等待 Agent 提交规划且不提供付费规划按钮", () => {
  assert.equal(typeof buildStoryboardActionState, "function");
  assert.deepEqual(buildStoryboardActionState({
    task: { task_id: "task-agent", orchestration_mode: "agent" },
    workflow: { current_stage: "ai_storyboard" },
  }), {
    label: "等待 Agent 提交规划",
    allowPaidTextRecommendation: false,
    sourceLabel: "当前 Agent",
    taskId: "task-agent",
  });
  assert.equal(buildStoryboardActionState({
    task: { task_id: "task-standalone", orchestration_mode: "standalone" },
    workflow: { current_stage: "ai_storyboard" },
  }).allowPaidTextRecommendation, true);
});

test("设置页用三张用途卡完成保存检测和删除", () => {
  const html = readFileSync(
    new URL("../web-ui/public/index.html", import.meta.url),
    "utf8",
  );
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  assert.match(html, /settings-provider-accounts/);
  assert.match(html, /添加供应商/);
  assert.match(app, /文案策划/);
  assert.match(app, /B-roll 画面/);
  assert.match(app, /配音/);
  assert.match(app, /保存并检测/);
  assert.match(app, /测试连接/);
  assert.match(app, /修改常用音色/);
  assert.match(app, /default-voice/);
  assert.match(app, /设为默认/);
  assert.match(app, /deleteModelProfile/);
});

test("个人包装素材设置展示自定义路径和离线重连状态", () => {
  assert.equal(typeof settingsView.personalPackagingStorageView, "function");
  assert.deepEqual(settingsView.personalPackagingStorageView({
    path: "E:\\团队成员甲\\包装素材",
    isDefault: false,
    exists: false,
    categories: [],
  }), {
    path: "E:\\团队成员甲\\包装素材",
    locationLabel: "自定义位置",
    statusLabel: "当前离线，请重新选择目录",
    statusClass: "missing",
  });
});

test("豆包供应商只在主流程展示四项凭据并折叠旧版兼容字段", () => {
  const html = readFileSync(
    new URL("../web-ui/public/index.html", import.meta.url),
    "utf8",
  );
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  assert.match(html, /name="preset"/);
  assert.match(html, /name="note"/);
  assert.match(html, /data-provider-generic[^>]*>[\s\S]*?name="baseUrl"/);
  assert.match(html, /name="defaultModel"/);
  assert.doesNotMatch(html, /<details[^>]*class="[^"]*provider-advanced/);
  assert.match(html, /data-provider-channel/);
  assert.match(html, /data-provider-tts/);
  assert.match(html, /name="accessKeyId"/);
  assert.match(html, /name="secretAccessKey"/);
  assert.match(html, /name="ttsApiKey"/);
  assert.match(html, /name="ttsResourceId"/);
  assert.match(html, />豆包语音合成 2\.0</);
  assert.match(
    html,
    /<details[^>]*class="[^"]*legacy-tts-credentials[^"]*"[^>]*data-provider-tts-legacy[\s\S]*?name="appId"[\s\S]*?name="secret"[\s\S]*?<\/details>/,
  );
  assert.doesNotMatch(html, /name="channelId"[^>]*required/);
  assert.doesNotMatch(html, /先添加本机 New API 网关账号/);

  assert.equal(providerPresetDefinition("volcengine_ark").showChannel, false);
  assert.equal(providerPresetDefinition("new_api").showChannel, true);
  assert.equal(providerPresetDefinition("doubao_tts").showTtsFields, true);
  assert.equal(
    providerPresetDefinition("doubao_tts").defaultBaseUrl,
    "https://openspeech.bytedance.com/api/v1/tts",
  );
  assert.match(html, /API Key 用于合成、试听和连接检测/);
  assert.match(html, /AK\/SK 用于自动获取音色目录/);
  assert.match(html, /旧版兼容设置/);
  assert.doesNotMatch(html, /name="ttsApiKey"[^>]*data-tts-required/);
  assert.doesNotMatch(html, /name="accessKeyId"[^>]*data-tts-required/);
  assert.doesNotMatch(html, /name="secretAccessKey"[^>]*data-tts-required/);
  assert.match(html, /name="ttsResourceId"[^>]*data-tts-required="true"/);
  assert.match(app, /const legacy = element\("details", "legacy-tts-credentials"\)/);
  assert.match(app, /form\.querySelectorAll\("\[data-provider-generic\]"\)/);
  assert.match(
    app,
    /account\.provider === "doubao_tts"[\s\S]*?"豆包配音服务 · " \+ ttsResourceLabel\(account\.settings\?\.tts_resource_id\)/,
  );
});

test("新建任务草稿跨页面与刷新保存，并可在创建或取消后清除", () => {
  const memory = new Map();
  const storage = {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, value),
    removeItem: (key) => memory.delete(key),
  };
  const drafts = createTaskDraftStore(storage);
  const draft = {
    route: "digital_human",
    orchestration_mode: "agent",
    subtype: "zhaoshang",
    title: "新疆数字人 v2",
    digital_human_mp4: "E:\\素材\\xinjiang.mp4",
    final_script: "这是最终口播文案。",
    primary_library_id: "park-xinjiang",
    selected_generic_ids: ["generic-industry"],
    include_default_generic: true,
    purpose: "视频号招商传播",
    target_duration_seconds: "90",
    planning_profile_id: "",
    broll_profile_id: "",
  };

  drafts.save(draft);
  assert.deepEqual(createTaskDraftStore(storage).read(), draft);
  drafts.clear();
  assert.equal(drafts.read(), null);
});

test("信息流广告草稿保留本条视频单独选择的音色", () => {
  const memory = new Map();
  const storage = {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, value),
    removeItem: (key) => memory.delete(key),
  };
  const drafts = createTaskDraftStore(storage);
  const saved = drafts.save({
    route: "ad_mix",
    tts_profile_id: "profile-doubao",
    tts_voice_id: "voice-selected-for-this-video",
  });
  assert.equal(saved.tts_voice_id, "voice-selected-for-this-video");
  assert.equal(drafts.read().tts_voice_id, "voice-selected-for-this-video");
});

test("初始化授权失败仍先显示视频类型并给出可执行提示", async () => {
  const events = [];
  const error = new Error("当前页面未获得本机工作台授权。");
  error.code = "AUTH_REQUIRED";

  const result = await initializeWorkbenchPage({
    renderStaticOptions: () => events.push("static-options"),
    loadBootstrap: async () => {
      events.push("load-bootstrap");
      throw error;
    },
    renderConnected: () => events.push("connected"),
    renderFailure: (failure) => events.push(failure.statusLabel),
  });

  assert.deepEqual(events, [
    "static-options",
    "load-bootstrap",
    "本机授权已失效",
  ]);
  assert.equal(result.connected, false);
  assert.equal(result.failure.actionLabel, "请从启动入口重新打开工作台");
});

test("镜头目录区分本次主素材与通用补充，包装库保持八类资产", () => {
  const groups = groupShotLibraries([
    { library_id: "park", purpose: "task_specific", name: "新疆园区" },
    { library_id: "generic", purpose: "generic", name: "先进制造通用素材" },
    { library_id: "pack", purpose: "packaging", name: "包装目录" },
  ]);
  assert.deepEqual(groups.primary.map((item) => item.library_id), ["park"]);
  assert.deepEqual(groups.generic.map((item) => item.library_id), ["generic"]);
  assert.equal(groups.packaging.length, 0);
  assert.deepEqual(
    PACKAGING_CATEGORIES.map((item) => item.label),
    ["字幕", "音色", "BGM", "音效", "文字卡", "贴纸", "花字", "Logo / 水印"],
  );
  assert.deepEqual(
    buildLibraryRegistration({
      name: "园区实拍库",
      mountPath: "E:\\园区素材",
      purpose: "task_specific",
    }),
    {
      name: "园区实拍库",
      mountPath: "E:\\园区素材",
      purpose: "task_specific",
      kind: "local",
      isDefaultGeneric: false,
    },
  );
});

test("包装素材二级导航可筛选，设置只保留 AI 与存储入口", () => {
  assert.deepEqual(
    PACKAGING_GROUPS.map((item) => item.label),
    ["全部包装素材", "字幕与字体", "配音与音色", "BGM 与音效", "视觉包装", "Logo 与水印"],
  );
  assert.deepEqual(
    filterPackagingCategories(PACKAGING_CATEGORIES, "audio").map((item) => item.key),
    ["bgm", "sfx"],
  );
  assert.deepEqual(
    filterPackagingCategories(PACKAGING_CATEGORIES, "visual").map((item) => item.key),
    ["title_card", "sticker", "fancy_text"],
  );

  const html = readFileSync(
    new URL("../web-ui/public/index.html", import.meta.url),
    "utf8",
  );
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(html, />外观</);
  assert.match(html, /data-settings-section="ai"/);
  assert.match(html, /data-settings-section="storage"/);
  assert.match(html, /data-packaging-filter="audio"/);
  assert.match(html, /id="packaging-detail"/);
  assert.match(app, /function selectPackagingFilter/);
  assert.match(app, /function renderPackagingDetail/);
  assert.match(app, /function loadStorageSettings/);
  assert.match(app, /cacheCleanupToken/);
  assert.match(app, /impactToken: state\.cacheCleanupToken/);
  assert.match(app, /availabilityStatus === "available"/);
  assert.match(app, /error\.code === "CACHE_PREVIEW_STALE"/);
});

test("设置能力投影不接收也不返回密钥或凭据引用", () => {
  const capabilities = normalizeCapabilities([
    {
      capability: "general_text",
      provider: "火山方舟",
      model: "deepseek-v4-pro",
      configured: true,
      lastCheckedAt: "2026-09-07T12:00:00Z",
      secret: "must-not-leak",
      credentialRef: "cutroom/general_text",
    },
  ]);
  assert.deepEqual(capabilities, [{
    capability: "general_text",
    provider: "火山方舟",
    model: "deepseek-v4-pro",
    configured: true,
    lastCheckedAt: "2026-09-07T12:00:00Z",
  }]);
});

test("耗时预检必须立即显示处理中并阻止重复提交", () => {
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  assert.match(app, /function setPreflightPending/);
  assert.match(app, /正在检查本机环境/);
  assert.match(app, /button\.disabled = pending/);
});

test("创建页步骤高亮必须跟随当前真实阶段", () => {
  assert.equal(createStepIndex("route"), 0);
  assert.equal(createStepIndex("input"), 1);
  assert.equal(createStepIndex("preflight"), 2);
  assert.equal(createStepIndex("create"), 3);
});

test("阶段工作区始终回答现在做什么、系统完成了什么、下一步是什么", () => {
  const detail = {
    workflow: {
      current_stage: "rough_cut",
      run_status: "awaiting_user",
      next_action: "confirm_picture_order",
      gates: { brief_locked: true },
    },
  };
  assert.deepEqual(buildWorkspaceSummary(detail), {
    currentStage: "rough_cut",
    currentLabel: "粗剪",
    runStatus: "awaiting_user",
    completed: ["文案已锁定"],
    nextAction: "confirm_picture_order",
    nextLabel: "检查画面并调整顺序",
  });
  assert.equal(workspaceKind("rough_cut"), "rough-cut");
  assert.equal(workspaceKind("picture_order"), "rough-cut");
  assert.equal(workspaceKind("packaging_confirm"), "packaging");
  assert.equal(workspaceKind("full_review"), "review");
  assert.equal(workspaceKind("publish_package"), "publish");
});

test("选中镜头后右侧只编辑当前对象，并完整展示可判断信息", () => {
  const selected = selectScene([
    {
      card_id: "task#4",
      idx: 4,
      narrative_text: "先进制造项目正在加速落地。",
      narrative_start: 41,
      narrative_end: 51,
      relative_path: "园区/生产线.mp4",
      source_type: "用户指定",
      system_suggestion: "语义匹配良好",
      final_decision: "采用生产线镜头",
      mix_form: "broll全屏",
    },
  ], "task#4");
  assert.deepEqual(selected, {
    id: "task#4",
    index: 4,
    narration: "先进制造项目正在加速落地。",
    time: "00:41–00:51",
    source: "园区/生产线.mp4",
    sourceType: "用户指定",
    truthfulness: "采用生产线镜头",
    suggestion: "语义匹配良好",
    mixForm: "broll全屏",
    mediaKind: null,
    previewUrl: null,
    thumbnailUrl: null,
    previewAvailable: false,
    previewProblem: null,
    narrativeStart: 41,
    narrativeEnd: 51,
    sourceStart: 41,
    sourceEnd: 51,
    playbackPolicy: "once",
    replaceScope: "只替换第 4 镜头，不影响其他镜头",
  });
  assert.deepEqual(buildSceneEdit("delete", selected), {
    action: "delete",
    card_id: "task#4",
  });
  assert.deepEqual(buildSceneEdit("split", selected, { playhead: 46 }), {
    action: "split",
    card_id: "task#4",
    playhead: 46,
  });
});

test("全局播放头定位场景并映射视频来源时间", () => {
  const cards = [
    { card_id: "a", narrative_start: 0, narrative_end: 4, enter_tc: 10, exit_tc: 14, media_kind: "video" },
    { card_id: "b", narrative_start: 4, narrative_end: 7, media_kind: "image" },
  ];
  assert.equal(sceneAtPlayhead(cards, 5).card_id, "b");
  assert.equal(sourceTimeAtPlayhead(cards[0], 2), 12);
  assert.deepEqual(advancePlayhead(cards, 3.9, 0.2), {
    playhead: 4.1,
    sceneId: "b",
    ended: false,
  });
});

test("短来源窗口按渲染规则循环且不漂移", () => {
  const card = {
    narrative_start: 0,
    narrative_end: 8,
    enter_tc: 20,
    exit_tc: 23,
    media_kind: "video",
    playback_policy: "loop",
  };
  assert.equal(sourceTimeAtPlayhead(card, 5), 22);
  assert.deepEqual(trimBounds(card, 20, 23), { enterTc: 20, exitTc: 23 });
});

test("粗剪预览优先使用服务端解析后的来源窗口", () => {
  const segmentCard = {
    narrative_start: 4,
    narrative_end: 8,
    enter_tc: 0,
    exit_tc: 0,
    resolved_source_start: 21,
    resolved_source_end: 25,
    source_range_start: 20,
    source_range_end: 30,
    media_kind: "video",
  };
  const digitalAnchor = {
    narrative_start: 12,
    narrative_end: 16,
    enter_tc: 0,
    exit_tc: 0,
    resolved_source_start: 12,
    resolved_source_end: 16,
    media_kind: "video",
    fixed_anchor: true,
  };

  assert.equal(sourceTimeAtPlayhead(segmentCard, 6), 23);
  assert.equal(sourceTimeAtPlayhead(digitalAnchor, 14), 14);
  assert.deepEqual(trimBounds(segmentCard, 22, 26), { enterTc: 22, exitTc: 26 });
  assert.throws(() => trimBounds(segmentCard, 19, 25), /允许范围/);
});

test("排序固定锚点并让锁定门禁保持显式", () => {
  const cards = [
    { card_id: "a", fixed_anchor: false },
    { card_id: "voice", fixed_anchor: true },
    { card_id: "c", fixed_anchor: false },
  ];
  assert.deepEqual(moveCardId(cards, "c", 0), ["c", "voice", "a"]);
  assert.deepEqual(
    pictureLockBlockers([
      { idx: 0, asset_id: "offline", media_kind: "image", preview_available: false, preview_problem: "素材离线" },
    ]),
    ["第 1 镜头：素材离线"],
  );
  assert.deepEqual(
    pictureLockBlockers([
      { card_id: "failed", idx: 0, asset_id: "a", media_kind: "video", preview_available: true },
    ], new Set(["failed"])),
    ["第 1 镜头：浏览器无法加载当前素材"],
  );
  assert.deepEqual(
    mediaVersionsForCards([
      { card_id: "a", preview_version: "v-a" },
      { card_id: "b", preview_version: "v-b" },
    ]),
    { a: "v-a", b: "v-b" },
  );
});

test("裁剪和拆分拒绝非有限值与端点", () => {
  const selected = {
    id: "a",
    narrativeStart: 2,
    narrativeEnd: 6,
    sourceStart: 10,
    sourceEnd: 16,
    playbackPolicy: "once",
  };
  assert.throws(
    () => buildSceneEdit("trim", selected, { enter_tc: Number.NaN, exit_tc: 14 }),
    /有效/,
  );
  assert.throws(
    () => buildSceneEdit("split", selected, { playhead: selected.narrativeStart }),
    /内部/,
  );
  assert.throws(
    () => trimBounds({ ...selected, narrative_start: 2, narrative_end: 6, enter_tc: 10, exit_tc: 16 }, 10, 13),
    /短于镜头/,
  );
});

test("AI 按钮明确模型职责、输入、影响和费用，候选结果不会直接覆盖", () => {
  assert.deepEqual(Object.keys(AI_ACTIONS), [
    "rewrite_brief", "recommend_storyboard", "match_shot", "transcribe",
    "synthesize_voice", "generate_video",
  ]);
  assert.equal(AI_ACTIONS.rewrite_brief.capability, "文案分析与改写模型");
  assert.equal(AI_ACTIONS.recommend_storyboard.capability, "分镜规划模型");
  assert.equal(AI_ACTIONS.match_shot.capability, "本地镜头匹配模型");
  assert.equal(AI_ACTIONS.transcribe.capability, "本地 ASR");
  assert.equal(AI_ACTIONS.synthesize_voice.capability, "TTS");
  assert.equal(AI_ACTIONS.generate_video.enabled, false);
  assert.match(AI_ACTIONS.generate_video.cost, /付费/);
  for (const action of Object.values(AI_ACTIONS)) {
    assert.ok(action.inputs.length);
    assert.ok(action.impact);
    assert.equal(action.candidateOnly, true);
  }
});

test("重复生成的推荐方案默认只展示当前选中方案，其余可展开回看", () => {
  const plans = Array.from({ length: 20 }, (_, index) => ({ planId: `plan-${index}` }));
  const current = partitionRecommendedPlans(plans, "plan-12");
  assert.deepEqual(current.visible.map((plan) => plan.planId), ["plan-12"]);
  assert.equal(current.history.length, 19);
  assert.equal(current.history.some((plan) => plan.planId === "plan-12"), false);
  assert.deepEqual(partitionRecommendedPlans(plans, "custom").visible.map((plan) => plan.planId), ["plan-0"]);
});

test("包装推荐与自选组合并存，预览和确认是两个动作", () => {
  const model = buildPackagingWorkspace([
    { plan_id: "plan-a", plan_name: "方案A · 推荐主线", status: "draft", item_count: 5, origin_type: "system_recommendation" },
    { plan_id: "plan-custom", plan_name: "我的组合", status: "draft", item_count: 3 },
  ], "plan-custom");
  assert.equal(model.recommended.length, 1);
  assert.equal(model.custom.length, 1);
  assert.equal(model.selected.planId, "plan-custom");
  assert.equal(model.previewAction, "preview");
  assert.equal(model.confirmAction, "confirm");
  assert.notEqual(model.previewAction, model.confirmAction);
});

test("包装工作区使用真实视频、观看门禁、模板和目录检查", () => {
  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  const preview = readFileSync(new URL("../web-ui/public/packaging-preview.mjs", import.meta.url), "utf8");
  assert.match(app, /data-packaging-preview/);
  assert.match(app, /packaging-previews\/" \+ encodeURIComponent\(preview\.previewId\) \+ "\/viewed/);
  assert.match(app, /currentTime \/ duration >= 0\.9/);
  assert.match(app, /data-action", "confirm-packaging/);
  assert.match(app, /previewId: preview\.previewId/);
  assert.match(preview, /"sfx"/);
  assert.match(app, /data-action", "disable-packaging-slot/);
  assert.match(app, /data-action", "save-packaging-template/);
  assert.match(app, /\/api\/packaging-templates\?route=/);
  assert.match(app, /\/api\/packaging-catalog\/check/);
  assert.match(app, /\/api\/packaging-catalog\/checks\/latest/);
  assert.match(app, /clearPackagingPreviewsForTask\(taskId\)/);
  assert.match(app, /data-action", "create-blank-packaging/);
  assert.match(app, /state\.selectedPlanId = result\.planId;\s*await openTask\(detail\.task\.task_id\);/);
  assert.match(app, /data-packaging-slot-preview/);
});

test("完整审片必须看完候选成片才能确认，发布包只使用已审片版本", () => {
  assert.deepEqual(
    buildReviewWorkspace(null, null),
    { candidateReady: false, watchCompleted: false, canApprove: false, canPublish: false },
  );
  const candidate = {
    mix_snapshot_id: "mix-1",
    qa_snapshot_id: "qa-snapshot-1",
    qa_report_id: "qa-1",
    output_path: "out/video.mp4",
  };
  assert.deepEqual(
    buildReviewWorkspace(
      { mix_snapshot_id: "mix-1", qa_report_id: "qa-1" },
      null,
    ),
    { candidateReady: false, watchCompleted: false, canApprove: false, canPublish: false },
  );
  assert.deepEqual(
    buildReviewWorkspace(candidate, { completed_at: null }, { human_reviewed: false }),
    { candidateReady: true, watchCompleted: false, canApprove: false, canPublish: false },
  );
  assert.deepEqual(
    buildReviewWorkspace(
      candidate,
      { completed_at: "2026-09-07T12:00:00Z", can_approve: true },
      { human_reviewed: true },
    ),
    { candidateReady: true, watchCompleted: true, canApprove: true, canPublish: true },
  );
  assert.deepEqual(
    buildReviewWorkspace(
      candidate,
      { completed_at: "2026-09-07T12:00:00Z", can_approve: false },
      { human_reviewed: false },
    ),
    { candidateReady: true, watchCompleted: false, canApprove: false, canPublish: false },
  );
  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  assert.match(app, /candidateId: selected\.candidateId/);
  assert.match(app, /snapshotId,/);
  assert.match(app, /qaReportId,/);
});

test("真实候选使用受控媒体地址，播放进度按节流规则上报且片尾触发完成", async () => {
  const candidate = {
    mix_snapshot_id: "mix-1",
    qa_snapshot_id: "qa-1",
    qa_report_id: "report-1",
  };
  assert.equal(
    reviewMediaUrl("新疆/数字人 v2", candidate),
    "/api/tasks/%E6%96%B0%E7%96%86%2F%E6%95%B0%E5%AD%97%E4%BA%BA%20v2/review-media",
  );
  assert.equal(reviewMediaUrl("task-1", null), null);

  const watch = { session_id: "watch-1", status: "active" };
  assert.equal(nextWatchReport(watch, 0.4, 10, 0, false), null);
  assert.deepEqual(
    nextWatchReport(watch, 1.0, 10, 0, false),
    { sessionId: "watch-1", positionSeconds: 1, completeAfterTick: false },
  );
  assert.deepEqual(
    nextWatchReport(watch, 10, 10, 9.4, true),
    { sessionId: "watch-1", positionSeconds: 10, completeAfterTick: true },
  );
  assert.equal(
    nextWatchReport({ ...watch, status: "completed" }, 10, 10, 9, true),
    null,
  );

  const sent = [];
  const reporter = createWatchReporter(watch, async (action) => sent.push(action));
  await reporter.report(0.4, 10);
  await reporter.report(1, 10);
  await reporter.report(1.2, 10);
  await reporter.report(10, 10, true);
  assert.deepEqual(sent, [
    { sessionId: "watch-1", positionSeconds: 1, completeAfterTick: false },
    { sessionId: "watch-1", positionSeconds: 10, completeAfterTick: true },
  ]);
});

test("首次播放自动建立唯一观看会话后才上报进度", async () => {
  let starts = 0;
  const sent = [];
  const controller = createPlaybackWatchController(
    null,
    async () => {
      starts += 1;
      return {
        session_id: "watch-auto-1",
        status: "active",
        last_position_seconds: 0,
      };
    },
    async (action) => sent.push(action),
  );

  const [first, second] = await Promise.all([
    controller.ensureStarted(),
    controller.ensureStarted(),
  ]);
  assert.equal(starts, 1);
  assert.equal(first.session_id, "watch-auto-1");
  assert.equal(second.session_id, "watch-auto-1");

  await controller.report(1, 10);
  assert.deepEqual(sent, [
    {
      sessionId: "watch-auto-1",
      positionSeconds: 1,
      completeAfterTick: false,
    },
  ]);
});

test("审片视频首次播放会等待服务端建立观看会话", () => {
  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  assert.match(app, /createPlaybackWatchController/);
  assert.match(app, /reviewVideo\.addEventListener\("play"/);
  assert.match(app, /reviewVideo\.pause\(\)/);
  assert.match(app, /await watchController\.ensureStarted\(\)/);
});

test("阶段条只允许回看已完成步骤且不会跳转后台阶段", () => {
  const html = readFileSync(new URL("../web-ui/public/index.html", import.meta.url), "utf8");
  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  assert.match(html, /id="stage-workspace"/);
  assert.match(html, /id="workspace-primary"/);
  assert.match(html, /id="workspace-inspector"/);
  assert.match(app, /renderTaskWorkspace/);
  assert.match(app, /buildWorkflowViewContext/);
  assert.match(app, /history-view-notice/);
  assert.match(app, /view\.readOnly/);
  assert.match(app, /renderPublish\(detail, primary, inspector, view\)/);
  assert.match(app, /if \(!view\.readOnly && canPublish\) \{\s*const form = element\("form", "publish-form"\)/);
  assert.match(app, /修改已锁定画面/);
  assert.match(app, /workspaceOverride/);
  assert.doesNotMatch(app, /setCurrentStage|jumpToStage|data-stage-jump/);
});

test("恢复工作区只展示服务端允许的恢复和取消动作", () => {
  const interrupted = buildRecoveryWorkspace({
    workflow: { run_status: "interrupted", current_stage: "material_sync" },
    jobs: [{
      jobId: "job-sync",
      status: "interrupted",
      phase: "同步本次素材",
      progress: { current: 12, total: 20 },
      canResume: true,
      canCancel: true,
      needsManualCheck: false,
      problem: "本机转写进程未能启动，请检查环境后重试。",
    }],
  });
  assert.deepEqual(interrupted, {
    runStatus: "interrupted",
    title: "处理在中途停止",
    description: "可以从服务端保存的安全检查点继续，不会从头覆盖已完成结果。",
    jobId: "job-sync",
    phase: "同步本次素材",
    progressLabel: "12 / 20",
    queueLabel: null,
    statusDetail: null,
    remainingLabel: "剩余时间待估算",
    startLabel: "开始时间待估算",
    observedLabel: null,
    ariaLive: "polite",
    canPause: false,
    pauseRequested: false,
    canResume: true,
    canCancel: true,
    needsManualCheck: false,
    problem: "本机转写进程未能启动，请检查环境后重试。",
  });

  const unknownPaidResult = buildRecoveryWorkspace({
    workflow: { run_status: "failed", current_stage: "ai_storyboard" },
    jobs: [{
      jobId: "job-ai",
      status: "failed",
      phase: "等待模型结果",
      progress: { current: 0, total: 1 },
      canResume: false,
      canCancel: false,
      needsManualCheck: true,
    }],
  });
  assert.equal(unknownPaidResult.canResume, false);
  assert.equal(unknownPaidResult.needsManualCheck, true);
  assert.match(unknownPaidResult.description, /不会自动重试/);

  const paused = buildRecoveryWorkspace({
    workflow: { run_status: "paused", current_stage: "fine_render" },
    jobs: [{
      jobId: "job-paused",
      status: "paused",
      phase: "渲染镜头 4",
      progress: { current: 4, total: 10 },
      canPause: false,
      canResume: true,
      canCancel: true,
      estimateStatus: "unknown",
    }],
  });
  assert.equal(paused.title, "任务已暂停");
  assert.equal(paused.canResume, true);
  assert.match(paused.statusDetail, /安全检查点/);

  assert.equal(buildRecoveryWorkspace({
    workflow: { run_status: "awaiting_user", current_stage: "rough_cut" },
    jobs: [],
  }), null);
});

test("任务素材目录离线时优先进入重新连接流程", () => {
  const offline = buildRecoveryWorkspace({
    workflow: { run_status: "awaiting_user", current_stage: "rough_cut" },
    sources: {
      primary: {
        library_id: "park-xinjiang",
        name: "新疆园区本次素材",
        mount_path: "E:\\园区素材\\招商政策",
        online: false,
      },
      selected_generic: [],
      default_generic: [],
    },
    jobs: [],
  });
  assert.equal(offline.mode, "offline-source");
  assert.equal(offline.libraryId, "park-xinjiang");
  assert.match(offline.description, /不会静默替换/);

  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  assert.match(app, /重新连接素材目录/);
  assert.match(app, /\/mount/);
});

test("工作台为键盘、读屏和异步检测提供完整反馈", () => {
  const html = readFileSync(new URL("../web-ui/public/index.html", import.meta.url), "utf8");
  const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
  const css = readFileSync(new URL("../web-ui/public/styles.css", import.meta.url), "utf8");

  assert.match(html, /class="skip-link" href="#workspace"/);
  assert.match(html, /id="workspace" tabindex="-1"/);
  assert.match(html, /id="provider-form-help"/);
  assert.match(html, /id="provider-account-form"[^>]*aria-describedby="provider-form-help"/);
  assert.match(html, /name="preset"[^>]*aria-label="供应商类型"/);
  assert.match(html, /name="name"[^>]*aria-label="供应商名称"/);
  assert.match(html, /name="note"[^>]*aria-label="备注"/);
  assert.match(html, /name="channelId"[^>]*aria-label="New API 固定渠道 ID"/);
  assert.match(html, /id="toast"[^>]*role="status"[^>]*aria-live="polite"[^>]*aria-atomic="true"/);

  assert.match(app, /result\.setAttribute\("aria-atomic", "true"\)/);
  assert.match(app, /setButtonBusy\(voices, refreshing, "正在刷新…"\)/);
  assert.match(app, /setButtonBusy\(testButton, isTesting, "检测中…"\)/);
  assert.match(css, /\.skip-link/);
  assert.match(css, /:focus-visible/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
});

test("Beta 8 文档统一说明豆包新版凭据与保存检测行为", () => {
  const documents = [
    "../README.md",
    "../web-ui/public/help.html",
    "../release/RELEASE_NOTES_v0.9.0-beta.8.md",
    "../docs/operations/model-profiles-new-api.md",
    "../docs/user-guide/cutroom-user-guide.md",
  ].map((path) => readFileSync(new URL(path, import.meta.url), "utf8"));

  for (const copy of documents) {
    assert.match(copy, /豆包 API Key[^。；\n<]*合成[^。；\n<]*试听[^。；\n<]*连接检测/);
    assert.match(copy, /AK\/SK[^。；\n<]*自动[^。；\n<]*音色目录/);
    assert.match(copy, /AppID[^。；\n<]*Access Token[^。；\n<]*旧版兼容[^。；\n<]*可以留空/);
    assert.match(copy, /保存并检测[^。；\n<]*极短[^。；\n<]*测试语音/);
  }

  const oldDesign = readFileSync(
    new URL("../docs/superpowers/specs/2026-09-14-doubao-tts-voice-selection-design.md", import.meta.url),
    "utf8",
  );
  assert.match(oldDesign, /凭据与首次配置说明已由.*2026-09-15.*系统化设计.*取代/);
});
