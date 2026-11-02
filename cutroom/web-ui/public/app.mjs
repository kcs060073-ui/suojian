import {
  ROUTE_LABELS,
  RUN_STATUS_LABELS,
  buildTaskCenter,
  createPreferenceStore,
  taskDestination,
} from "./state.mjs";
import {
  WORKFLOW_STAGES,
  buildStagePresentation,
  renderWorkflow,
  workflowLabel,
} from "./workflow-view.mjs";
import { describeQaIssue, qaIssueSeekSeconds } from "./qa-view.mjs";
import {
  ROUTE_OPTIONS,
  SUBTYPE_OPTIONS,
  buildCreateRequest,
  createTaskDraftStore,
  createStepIndex,
  initializeWorkbenchPage,
  preflightSummary,
} from "./task-create.mjs";
import {
  PACKAGING_CATEGORIES,
  PACKAGING_GROUPS,
  authorizationLabel,
  buildLibraryRegistration,
  filterPackagingCategories,
  groupShotLibraries,
  packagingAvailabilityLabel,
} from "./libraries-view.mjs";
import {
  CAPABILITY_HELP,
  availabilityLabel,
  groupModelProfiles,
  personalPackagingStorageView,
  providerAccountPresentation,
  providerSupportsCapability,
  providerPresetDefinition,
  selectableProfiles,
  suggestedModelProfileName,
  toastDuration,
  voiceOptionPresentation,
} from "./settings-view.mjs";
import {
  AI_ACTIONS,
  buildRecoveryWorkspace,
  buildStoryboardActionState,
  buildWorkflowViewContext,
  buildWorkspaceSummary,
  roughCutDescription,
  workspaceKind,
} from "./task-workspace.mjs";
import {
  buildSceneEdit,
  formatTimecode,
  selectScene,
} from "./scene-editor.mjs";
import {
  advancePlayhead,
  mediaVersionsForCards,
  moveCardId,
  pictureLockBlockers,
  sceneAtPlayhead,
  sourceTimeAtPlayhead,
  trimBounds,
} from "./rough-preview.mjs";
import {
  buildPackagingWorkspace,
  partitionRecommendedPlans,
} from "./packaging-editor.mjs";
import {
  SLOT_DEFINITIONS,
  buildPackagingPreviewModel,
  slotRows,
} from "./packaging-preview.mjs";
import {
  buildCatalogCheckModel,
} from "./packaging-catalog.mjs";
import {
  createPlaybackWatchController,
  reviewMediaUrl,
} from "./review-view.mjs";
import {
  buildPublishPackageView,
} from "./publish-view.mjs";
import {
  buildCoverCandidates,
  buildPublishFormValue,
} from "./publish-controls.mjs";
import {
  buildServiceJobSummary,
} from "./job-status.mjs";
import {
  buildTaskSearchResult,
} from "./task-center-controls.mjs";
import {
  buildPreflightView,
} from "./preflight-view.mjs";
import {
  buildMaterialIndexQuery,
  buildMaterialIndexView,
  formatMaterialDuration,
  formatMaterialSize,
} from "./material-index.mjs";
import {
  buildCandidateHistory,
  buildReworkDraft,
  groupQaIssues,
  materialAuthorizationAction,
  materialAuthorizationSetup,
  reviewGate,
  reviewRenderAction,
} from "./review-closure.mjs";
import {
  buildStoryboardFeedbackView,
  requireStoryboardInstruction,
} from "./storyboard-feedback.mjs";

const ACTION_LABELS = Object.freeze({
  complete_preflight: "完成创建前预检",
  lock_brief: "确认并锁定最终文案",
  sync_materials: "检查并同步本次素材",
  generate_storyboard: "让 AI 推荐分镜候选",
  generate_rough_cut: "生成可确认的粗剪",
  confirm_picture_order: "检查画面并调整顺序",
  lock_picture: "确认粗剪并锁定画面",
  confirm_packaging: "选择并确认本片包装方案",
  render_fine_cut: "执行精剪成片",
  run_auto_qa: "运行自动质检",
  complete_review: "从头到尾观看并确认成片",
  build_publish_package: "生成完整发布包",
});

const root = document.querySelector("#cutroom-workbench");
const app = root.querySelector(".app");
const pages = [...root.querySelectorAll(".page")];
const contexts = [...root.querySelectorAll("[data-context]")];
const navButtons = [...root.querySelectorAll(".nav")];
const themeButtons = [...root.querySelectorAll("[data-theme]")];
const taskViewButtons = [...root.querySelectorAll("[data-task-view]")];
const toast = root.querySelector("#toast");

const fallbackMemory = new Map();
const fallbackStorage = {
  getItem: (key) => fallbackMemory.get(key) ?? null,
  setItem: (key, value) => fallbackMemory.set(key, value),
  removeItem: (key) => fallbackMemory.delete(key),
};

let preferences;
let taskDrafts;
try {
  preferences = createPreferenceStore(window.localStorage);
  preferences.read();
  taskDrafts = createTaskDraftStore(window.localStorage);
  taskDrafts.read();
} catch {
  preferences = createPreferenceStore(fallbackStorage);
  taskDrafts = createTaskDraftStore(fallbackStorage);
}

const state = {
  bootstrap: { tasks: [], jobs: [] },
  selectedTaskId: null,
  taskView: "board",
  taskControls: { query: "", route: "", status: "", sort: "updated_desc" },
  toastTimer: null,
  createRequest: null,
  preflight: null,
  selectedSceneId: null,
  previewCandidateId: null,
  selectedReviewCandidateId: null,
  pendingReviewReworkId: null,
  roughPlayhead: 0,
  roughPlaying: false,
  roughAnimationFrame: null,
  roughMediaFailures: new Set(),
  selectedPlanId: null,
  pendingImpact: null,
  taskDetail: null,
  workspaceOverride: null,
  viewingStageKey: null,
  providerModels: {},
  providerVoices: {},
  profileVoices: {},
  loadingProviderVoices: new Set(),
  loadingProfileVoices: new Set(),
  refreshingVoiceCatalogs: new Set(),
  testingVoices: new Set(),
  editingProviderCredentials: new Set(),
  checkingProviderCredentials: new Set(),
  providerCredentialChecks: {},
  voicePreviewUrls: {},
  pendingCreateVoiceId: "",
  testingProfiles: new Set(),
  packagingFilter: "all",
  selectedPackagingCategory: null,
  packagingPreviews: {},
  loadingPackagingPreviews: new Set(),
  packagingViewed: new Set(),
  packagingPreviewPolls: new Map(),
  packagingTemplates: {},
  loadingPackagingTemplates: new Set(),
  packagingCatalogCheck: null,
  packagingCatalogCheckLoading: false,
  packagingCatalogCheckPoll: null,
  storage: null,
  cacheCleanupToken: null,
  materialIndex: { items: [], total: 0, limit: 24, offset: 0 },
  materialFilters: {
    query: "",
    libraryId: "",
    mediaKind: "",
    availability: "",
    authorization: "",
    limit: 24,
    offset: 0,
  },
  materialIndexLoading: false,
  materialSearchTimer: null,
  storyboardFeedback: {},
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function statusBadge(text, tone = "neutral") {
  return element("span", "status-badge " + tone, text);
}

function inlineFeedback(title, detail, tone = "neutral") {
  const result = element("div", "inline-feedback provider-check-result " + tone);
  result.setAttribute("role", "status");
  result.setAttribute("aria-live", "polite");
  result.setAttribute("aria-atomic", "true");
  result.append(
    element("b", null, title),
    element("span", null, detail),
  );
  return result;
}

function setButtonBusy(button, busy, label) {
  if (!button) return;
  if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent;
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  button.textContent = busy ? label : button.dataset.idleLabel;
}

function actionLabel(action) {
  return ACTION_LABELS[action] || (action ? "按任务提示继续" : "查看任务结果");
}

function showToast(message, { kind = "info" } = {}) {
  toast.textContent = message;
  toast.dataset.kind = kind;
  toast.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(
    () => { toast.hidden = true; },
    toastDuration(kind),
  );
}

function showErrorToast(message) {
  showToast(message, { kind: "error" });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.error?.message || "本机服务暂时无法完成该操作。");
    error.code = payload?.error?.code || "REQUEST_FAILED";
    error.diagnosticId = payload?.error?.diagnosticId || null;
    error.details = payload?.error?.details || null;
    throw error;
  }
  return payload;
}

function setTheme(theme) {
  root.dataset.theme = theme;
  root.style.colorScheme = theme === "system" ? "light dark" : theme;
  themeButtons.forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.theme === theme));
  });
  preferences.setTheme(theme);
}

function setTaskView(view) {
  state.taskView = view;
  root.querySelector("#task-board").hidden = view !== "board";
  root.querySelector("#task-list-view").hidden = view !== "list";
  taskViewButtons.forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.taskView === view));
  });
  preferences.setTaskView(view);
}

function showContext(name) {
  contexts.forEach((context) => {
    context.hidden = context.dataset.context !== name;
  });
}

function showPage(name, { crumb = null, context = "tasks" } = {}) {
  pages.forEach((page) => {
    page.hidden = page.dataset.view !== name;
  });
  showContext(context);
  navButtons.forEach((button) => {
    const current = context === "tasks"
      ? button.dataset.page === "tasks"
      : button.dataset.page === context;
    button.setAttribute("aria-current", current ? "page" : "false");
  });
  root.querySelector("#crumb").textContent = crumb || "任务中心";
}

function jsonRequest(method, body = {}) {
  return { method, body: JSON.stringify(body) };
}

function taskStatusClass(status) {
  if (status === "completed") return "completed";
  if (status === "running" || status === "queued") return "running";
  return "";
}

function formatTime(value) {
  if (!value) return "尚无记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间待确认";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function openButton(task, sourceView, label = "打开") {
  const button = element("button", "btn", label);
  button.type = "button";
  button.dataset.taskId = task.taskId;
  button.dataset.sourceView = sourceView;
  button.addEventListener("click", () => {
    window.location.hash = taskDestination(task, sourceView);
  });
  return button;
}

function renderBoard(center) {
  const board = root.querySelector("#task-board");
  const fragment = document.createDocumentFragment();
  for (const lane of center.lanes) {
    const section = element("section", "lane");
    section.dataset.lane = lane.key;
    const head = element("div", "lane-head");
    head.append(element("b", null, lane.label), element("span", null, String(lane.tasks.length)));
    section.append(head);
    const stack = element("div", "lane-stack");
    if (!lane.tasks.length) {
      stack.append(element("div", "lane-empty", "暂无任务"));
    }
    for (const task of lane.tasks) {
      const card = element("article", "lane-card");
      card.dataset.taskId = task.taskId;
      card.append(
        element("b", null, task.title),
        element("span", null, actionLabel(task.nextAction)),
        element(
          "span",
          "meta status-text " + taskStatusClass(task.runStatus),
          (RUN_STATUS_LABELS[task.runStatus] || "状态待确认") + " · " + workflowLabel(task.currentStage),
        ),
        openButton(task, "board", lane.key === "awaiting" ? "继续处理" : "查看任务"),
      );
      stack.append(card);
    }
    section.append(stack);
    fragment.append(section);
  }
  board.replaceChildren(fragment);
}

function renderList(center) {
  const body = root.querySelector("#task-list-body");
  const fragment = document.createDocumentFragment();
  for (const task of center.list) {
    const row = document.createElement("tr");
    row.dataset.taskId = task.taskId;
    const title = element("td");
    title.append(element("b", null, task.title));
    const route = element("td", null, ROUTE_LABELS[task.route] || "暂未识别路线");
    const stage = element("td", null, workflowLabel(task.currentStage));
    const run = element("td", "status-text " + taskStatusClass(task.runStatus), RUN_STATUS_LABELS[task.runStatus] || "状态待确认");
    const next = element("td", "next-cell", actionLabel(task.nextAction));
    const updated = element("td", null, formatTime(task.updatedAt));
    const action = element("td");
    action.append(openButton(task, "list"));
    row.append(title, route, stage, run, next, updated, action);
    fragment.append(row);
  }
  if (!center.list.length) {
    const row = document.createElement("tr");
    const cell = element("td", "lane-empty", "还没有任务。点击“新建视频”开始第一条视频。");
    cell.colSpan = 7;
    row.append(cell);
    fragment.append(row);
  }
  body.replaceChildren(fragment);
}

function renderQuickTasks(center) {
  const container = root.querySelector("#quick-task-list");
  const fragment = document.createDocumentFragment();
  const quickLaneKeys = new Set(["awaiting", "automatic", "completed"]);
  for (const lane of center.lanes.filter((item) => quickLaneKeys.has(item.key))) {
    if (!lane.tasks.length) continue;
    fragment.append(element("div", "task-label", lane.label + " · " + lane.tasks.length));
    for (const task of lane.tasks.slice(0, 4)) {
      const button = element(
        "button",
        "task-mini" + (task.taskId === state.selectedTaskId ? " selected" : ""),
      );
      button.type = "button";
      button.dataset.taskId = task.taskId;
      const status = element("em", lane.key === "automatic" || lane.key === "queued" ? "auto" : lane.key === "completed" ? "done" : null, RUN_STATUS_LABELS[task.runStatus] || "状态待确认");
      button.append(
        element("b", null, task.title),
        element("span", null, (ROUTE_LABELS[task.route] || "未识别路线") + " · " + workflowLabel(task.currentStage)),
        status,
      );
      button.addEventListener("click", () => {
        window.location.hash = taskDestination(task, "quick");
      });
      fragment.append(button);
    }
  }
  if (!fragment.childNodes.length) {
    fragment.append(element("div", "task-label", "暂无任务"));
  }
  container.replaceChildren(fragment);
}

function renderMetrics(center) {
  const counts = Object.fromEntries(center.lanes.map((lane) => [lane.key, lane.tasks.length]));
  const values = [
    [counts.awaiting, "等待确认"],
    [counts.automatic + counts.queued, "处理中 / 排队"],
    [counts.problem, "需要恢复"],
  ];
  const container = root.querySelector("#task-metrics");
  container.replaceChildren(...values.map(([count, label]) => {
    const metric = element("div", "metric");
    metric.append(element("b", null, count + " 条"), element("span", null, label));
    return metric;
  }));
}

function renderTaskCenter() {
  const search = buildTaskSearchResult(state.bootstrap.tasks, state.taskControls);
  const center = buildTaskCenter(search.tasks);
  const empty = root.querySelector("#task-filter-empty");
  empty.textContent = search.emptyMessage;
  empty.hidden = Boolean(search.tasks.length);
  renderBoard(center);
  renderList(center);
  renderQuickTasks(center);
  renderMetrics(center);
  setTaskView(state.taskView);
}

function updateTaskControls() {
  state.taskControls = {
    query: root.querySelector("#task-search").value,
    route: root.querySelector("#task-route-filter").value,
    status: root.querySelector("#task-status-filter").value,
    sort: root.querySelector("#task-sort").value,
  };
  renderTaskCenter();
}

function renderServiceState() {
  const summary = buildServiceJobSummary(state.bootstrap.jobs || []);
  const service = root.querySelector("#service-status");
  service.classList.remove("error");
  service.lastElementChild.textContent = "本机工作流就绪";
  root.querySelector("#heavy-status").textContent = summary.label;
}

function gateSummary(gates) {
  const labels = [
    ["brief_locked", "文案"],
    ["picture_lock", "画面"],
    ["packaging_confirmed", "包装"],
    ["human_reviewed", "审片"],
  ];
  const completed = labels.filter(([key]) => Boolean(gates?.[key])).map(([, label]) => label);
  return completed.length ? "已完成确认：" + completed.join("、") : "尚未完成人工确认";
}

function button(label, className = "btn") {
  const control = element("button", className, label);
  control.type = "button";
  return control;
}

async function runButtonAction(control, label, action) {
  if (control.disabled) return;
  const idleLabel = control.textContent;
  control.disabled = true;
  control.setAttribute("aria-busy", "true");
  control.textContent = label;
  try {
    return await action();
  } catch (error) {
    showActionError(error);
  } finally {
    control.disabled = false;
    control.setAttribute("aria-busy", "false");
    control.textContent = idleLabel;
  }
}

function workspaceHeading(kicker, title, description) {
  const head = element("div", "workspace-heading");
  const copy = element("div");
  copy.append(element("span", "section-kicker", kicker), element("h2", null, title));
  if (description) copy.append(element("p", null, description));
  head.append(copy);
  return head;
}

function renderAiDisclosure(actionKey) {
  const action = AI_ACTIONS[actionKey];
  const panel = element("section", "ai-disclosure");
  panel.append(
    element("span", "ai-chip", action.capability),
    element("b", null, action.label),
    element("p", null, "输入：" + action.inputs.join("、")),
    element("p", null, "影响：" + action.impact),
    element("em", null, action.cost),
  );
  return panel;
}

async function taskPost(path, body = {}) {
  const taskId = state.selectedTaskId;
  const result = await api(
    "/api/tasks/" + encodeURIComponent(taskId) + path,
    jsonRequest("POST", body),
  );
  state.viewingStageKey = null;
  if (path === "/picture/lock") state.workspaceOverride = null;
  if (path === "/packaging/compose" && result.planId) state.selectedPlanId = result.planId;
  if (path === "/packaging/propose" && result.planIds?.length) state.selectedPlanId = result.planIds[0];
  await refreshBootstrap();
  await openTask(taskId);
  return result;
}

function packagingPreviewKey(taskId, planId) {
  return `${taskId}:${planId}`;
}

function clearPackagingPreviewsForTask(taskId) {
  const prefix = `${taskId}:`;
  Object.keys(state.packagingPreviews).forEach((key) => {
    if (key.startsWith(prefix)) delete state.packagingPreviews[key];
  });
  for (const [key, timer] of state.packagingPreviewPolls) {
    if (!key.startsWith(prefix)) continue;
    clearTimeout(timer);
    state.packagingPreviewPolls.delete(key);
  }
}

function rerenderSelectedPackaging(taskId, planId) {
  const stage = state.viewingStageKey || state.taskDetail?.workflow?.current_stage;
  if (
    state.selectedTaskId === taskId
    && state.selectedPlanId === planId
    && state.taskDetail
    && !state.workspaceOverride
    && (!stage || stage === "packaging_confirm")
  ) {
    renderTaskWorkspace(state.taskDetail);
  }
}

function schedulePackagingPreviewRefresh(taskId, planId) {
  const key = packagingPreviewKey(taskId, planId);
  if (state.packagingPreviewPolls.has(key)) return;
  const timer = setTimeout(async () => {
    state.packagingPreviewPolls.delete(key);
    await loadPackagingPreview(taskId, planId, { force: true });
  }, 2000);
  state.packagingPreviewPolls.set(key, timer);
}

async function loadPackagingPreview(taskId, planId, { force = false } = {}) {
  const key = packagingPreviewKey(taskId, planId);
  if (state.loadingPackagingPreviews.has(key)) return;
  if (!force && Object.hasOwn(state.packagingPreviews, key)) return;
  state.loadingPackagingPreviews.add(key);
  // Called while rendering the inspector: do not synchronously re-enter and
  // append a second set of controls to the same live containers.
  try {
    const preview = await api(
      "/api/tasks/" + encodeURIComponent(taskId)
      + "/packaging/" + encodeURIComponent(planId) + "/preview",
    );
    state.packagingPreviews[key] = preview;
    if (["queued", "rendering"].includes(preview.status)) {
      schedulePackagingPreviewRefresh(taskId, planId);
    }
  } catch (error) {
    state.packagingPreviews[key] = {
      status: "failed",
      errorCode: error.code || "PREVIEW_STATUS_FAILED",
      message: error.message,
    };
  } finally {
    state.loadingPackagingPreviews.delete(key);
    rerenderSelectedPackaging(taskId, planId);
  }
}

async function requestPackagingPreview(taskId, planId, control) {
  const key = packagingPreviewKey(taskId, planId);
  setButtonBusy(control, true, "正在提交预览…");
  try {
    const preview = await api(
      "/api/tasks/" + encodeURIComponent(taskId)
      + "/packaging/" + encodeURIComponent(planId) + "/preview",
      jsonRequest("POST", {}),
    );
    state.packagingPreviews[key] = preview;
    showToast(preview.status === "ready" ? "真实包装预览已就绪。" : "真实包装预览已加入渲染队列。");
    if (["queued", "rendering"].includes(preview.status)) {
      schedulePackagingPreviewRefresh(taskId, planId);
    }
    rerenderSelectedPackaging(taskId, planId);
  } catch (error) {
    showActionError(error);
  } finally {
    setButtonBusy(control, false, "正在提交预览…");
  }
}

async function loadPackagingTemplates(route, { force = false } = {}) {
  if (!route || state.loadingPackagingTemplates.has(route)) return;
  if (!force && Object.hasOwn(state.packagingTemplates, route)) return;
  state.loadingPackagingTemplates.add(route);
  try {
    const payload = await api("/api/packaging-templates?route=" + encodeURIComponent(route));
    state.packagingTemplates[route] = payload.templates || [];
  } catch (error) {
    state.packagingTemplates[route] = [];
    showActionError(error);
  } finally {
    state.loadingPackagingTemplates.delete(route);
    if (state.taskDetail?.task?.task_type === route) {
      rerenderSelectedPackaging(state.selectedTaskId, state.selectedPlanId);
    }
  }
}

async function jobPost(jobId, action, body = {}) {
  const taskId = state.selectedTaskId;
  const result = await api(
    "/api/jobs/" + encodeURIComponent(jobId) + "/" + action,
    jsonRequest("POST", body),
  );
  state.workspaceOverride = null;
  state.viewingStageKey = null;
  await refreshBootstrap();
  await openTask(taskId);
  return result;
}

async function waitForJob(jobId, { intervalMs = 500, maxAttempts = 240 } = {}) {
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const job = await api("/api/jobs/" + encodeURIComponent(jobId));
    if (["completed", "failed", "cancelled", "interrupted"].includes(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error("代理预览生成超时，请稍后重试。");
}

async function runJobControl(control, jobId, action, body = {}) {
  control.disabled = true;
  control.setAttribute("aria-busy", "true");
  try {
    return await jobPost(jobId, action, body);
  } finally {
    control.disabled = false;
    control.removeAttribute("aria-busy");
  }
}

async function reconnectLibrary(libraryId, taskId = null) {
  const picked = await api("/api/picker/directory", jsonRequest("POST"));
  if (!picked.path) return null;
  const result = await api(
    "/api/libraries/" + encodeURIComponent(libraryId) + "/mount",
    jsonRequest("POST", { mountPath: picked.path }),
  );
  state.workspaceOverride = null;
  state.viewingStageKey = null;
  await refreshBootstrap();
  if (taskId) await openTask(taskId);
  showToast("新位置已保存，正在检查素材目录。");
  return result;
}

function showActionError(error) {
  showErrorToast(error.message + (error.diagnosticId ? "（诊断编号 " + error.diagnosticId + "）" : ""));
}

function renderStageSummary(detail) {
  const summary = buildWorkspaceSummary(detail);
  const view = buildWorkflowViewContext(detail, state.viewingStageKey);
  const stage = buildStagePresentation(detail.workflow, view.viewingStageKey);
  if (!stage) return;
  const blockerCount = Number(detail.currentCandidate?.qa?.blockingCount || 0);
  const reviewBlocked = !view.readOnly && stage.key === "full_review" && blockerCount > 0;
  root.querySelector("#task-step-index").textContent = `${stage.phase} · 第 ${stage.number} / 12 步`;
  root.querySelector("#task-step-title").textContent = stage.label;
  root.querySelector("#task-step-instruction").textContent = reviewBlocked
    ? `当前成片有 ${blockerCount} 项质检阻断，请先处理后再完整审片。`
    : stage.instruction;
  root.querySelector("#task-step-action-label").textContent = view.readOnly ? "正在回看" : "现在要做";
  root.querySelector("#task-next-action").textContent = view.readOnly
    ? "查看已保存的阶段结果" : reviewBlocked ? "处理质检阻断项" : summary.nextLabel;
  root.querySelector("#task-step-outcome").textContent = view.readOnly
    ? (buildStagePresentation(detail.workflow)
      ? `任务实际在第 ${buildStagePresentation(detail.workflow).number} 步 · ${view.actualStageLabel}`
      : "任务流程已完成，正在查看保存的结果。")
    : reviewBlocked
      ? "质检阻断解除后，再完整观看并确认当前成片。"
      : `完成本步后：${stage.outcome}`;
  root.querySelector("#stage-focus").classList.toggle("is-history", view.readOnly);
  root.querySelector("#stage-focus").classList.toggle("is-blocked", reviewBlocked);
}

function renderTaskWorkflow(detail) {
  const view = buildWorkflowViewContext(detail, state.viewingStageKey);
  state.viewingStageKey = view.readOnly ? view.viewingStageKey : null;
  renderWorkflow(root.querySelector("#task-workflow"), detail.workflow, {
    viewingStageKey: view.viewingStageKey,
    onSelect: (stageKey) => {
      const nextView = buildWorkflowViewContext(detail, stageKey);
      state.viewingStageKey = nextView.readOnly ? nextView.viewingStageKey : null;
      state.workspaceOverride = null;
      renderTaskWorkflow(detail);
      renderTaskWorkspace(detail);
    },
  });
}

function renderHistoryNotice(detail, view) {
  const notice = element("section", "history-view-notice");
  notice.setAttribute("role", "status");
  const copy = element("div");
  const finished = !view.actualStageKey;
  copy.append(
    element("span", "section-kicker", finished ? "任务已完成" : "只读回看"),
    element("b", null, "正在查看第 " + String(
      WORKFLOW_STAGES.findIndex((stage) => stage.key === view.viewingStageKey) + 1,
    ) + " 步 · " + view.viewingStageLabel),
    element("p", null, finished
      ? "全部步骤已完成，当前只查看保存的结果。"
      : "任务实际仍停留在“" + view.actualStageLabel + "”；回看不会改变门禁、版本或后台运行状态。"),
  );
  if (finished) {
    notice.append(copy);
    return notice;
  }
  const back = button("返回当前步骤", "btn");
  back.addEventListener("click", () => {
    state.viewingStageKey = null;
    state.workspaceOverride = null;
    renderTaskWorkflow(detail);
    renderTaskWorkspace(detail);
  });
  notice.append(copy, back);
  return notice;
}

function renderBriefStoryboard(detail, primary, inspector, view) {
  const stage = view.viewingStageKey;
  const storyboardAction = buildStoryboardActionState(detail);
  const stageTitles = {
    create_preflight: "本次任务输入",
    brief_lock: "最终口播文案",
    material_sync: "本次可用素材",
    ai_storyboard: "分镜候选与画面",
  };
  primary.append(workspaceHeading(
    "本步内容",
    stageTitles[stage] || "查看输入与候选",
  ));
  const primarySource = detail.sources?.primary;
  const genericSources = [
    ...(detail.sources?.selected_generic || []),
    ...(detail.sources?.default_generic || []),
  ];
  if (["create_preflight", "brief_lock"].includes(stage)) {
    const card = element("section", "script-card");
    card.append(
      element("span", "section-kicker", "已保存文案"),
      element("h3", null, "请按最终口播内容核对"),
    );
    if (detail.finalScriptText) {
      card.append(element("pre", null, detail.finalScriptText));
    } else {
      card.append(element("p", "muted-copy", "这条任务未找到可直接展示的文案文件；请核对创建时提供的文案来源。"));
    }
    primary.append(card);
    if (stage === "create_preflight") {
      const facts = element("div", "workspace-facts");
      facts.append(
        metricBlock("视频路线", ROUTE_LABELS[detail.task?.task_type] || "待确认"),
        metricBlock("本次主素材", primarySource?.name || "尚未指定"),
        metricBlock("补充目录", `${genericSources.length} 个`),
      );
      primary.append(facts);
    }
  } else if (stage === "material_sync") {
    const list = element("div", "source-review-list");
    const sources = [primarySource, ...genericSources].filter(Boolean);
    sources.forEach((source, index) => {
      const row = element("article", "source-review-row");
      row.append(
        element("span", "source-review-kind", index === 0 ? "本次主素材" : "通用补充"),
        element("b", null, source.name || "未命名目录"),
        element("span", null, `${source.asset_count ?? 0} 项素材 · ${source.online === false ? "当前离线" : "可访问"}`),
      );
      list.append(row);
    });
    if (!sources.length) list.append(inlineFeedback("尚无素材目录", "请先登记本次主素材目录。", "partial"));
    primary.append(list);
  } else if (stage === "ai_storyboard") {
    const cards = detail.sceneCards || [];
    const list = element("div", "storyboard-review-list");
    cards.forEach((card, index) => {
      const row = element("article", "storyboard-review-row");
      const copy = element("div");
      copy.append(
        element("b", null, `镜头 ${index + 1} · ${card.mix_form || "待确认画面"}`),
        element("p", null, card.narrative_text || "尚无旁白摘要"),
        element("small", null, card.relative_path || "画面素材待匹配"),
      );
      row.append(copy);
      list.append(row);
    });
    if (!cards.length) list.append(inlineFeedback("尚无分镜候选", "等待 AI 或当前 Agent 提交分镜后，这里会显示逐镜结果。"));
    primary.append(list);
  }
  const actionRow = element("div", "row-actions");
  if (!view.readOnly && !detail.workflow.gates?.brief_locked) {
    const lock = button("确认并锁定最终文案", "btn primary");
    lock.addEventListener("click", async () => {
      try { await taskPost("/brief/lock"); } catch (error) { showActionError(error); }
    });
    actionRow.append(lock);
  } else if (!view.readOnly && stage === "material_sync") {
    const sync = button("预览素材增量", "btn primary");
    sync.addEventListener("click", async () => {
      try {
        const impact = await api(
          "/api/tasks/" + encodeURIComponent(state.selectedTaskId) + "/sources/preview",
          jsonRequest("POST"),
        );
        const ok = window.confirm(
          "将同步当前允许的素材范围。新增 " + (impact.added?.length || 0) +
          " 项，更新 " + (impact.changed?.length || 0) + " 项，移除 " +
          (impact.removed?.length || 0) + " 项。继续吗？",
        );
        if (ok) await taskPost("/sources/sync", { impactToken: impact.impact_token });
      } catch (error) { showActionError(error); }
    });
    actionRow.append(sync);
  } else if (!view.readOnly && storyboardAction.allowPaidTextRecommendation) {
    const recommend = button(storyboardAction.label, "btn primary");
    recommend.addEventListener("click", async () => {
      const disclosure = AI_ACTIONS.recommend_storyboard;
      if (!window.confirm(
        disclosure.capability + "\n输入：" + disclosure.inputs.join("、") +
        "\n" + disclosure.impact + "\n" + disclosure.cost + "\n\n确认发起本次调用吗？",
      )) return;
      try {
        await taskPost("/storyboard/recommend", {
          confirmPaidCall: true,
          requestId: crypto.randomUUID(),
        });
      } catch (error) { showActionError(error); }
    });
    actionRow.append(recommend);
  } else if (!view.readOnly && stage === "ai_storyboard") {
    const waiting = element("section", "agent-planning-wait");
    waiting.append(
      element("b", null, storyboardAction.label),
      element("p", null, "任务 ID：" + (storyboardAction.taskId || "读取中")),
      element("p", null, "请回到 Codex，让当前 Agent 提交文案与分镜规划；CUTROOM 不会调用自己的文案模型。"),
    );
    actionRow.append(waiting);
  }
  if (!view.readOnly && actionRow.childElementCount) primary.append(actionRow);
  inspector.append(workspaceHeading(
    "规划来源",
    storyboardAction.sourceLabel,
    storyboardAction.allowPaidTextRecommendation
      ? "CUTROOM 使用本任务冻结的规划模型生成候选。"
      : "文案与分镜由当前 Agent 提交，CUTROOM 只校验和执行。",
  ));
  if (storyboardAction.allowPaidTextRecommendation) {
    inspector.append(renderAiDisclosure(stage === "ai_storyboard" ? "recommend_storyboard" : "transcribe"));
  }
  inspector.append(renderAiDisclosure("generate_video"));
}

function roughMediaError(scene, { onGeneratePreview = null } = {}) {
  const problem = scene?.preview_problem || "当前素材无法加载，请检查素材连接后重试。";
  const panel = element("div", "rough-media-error");
  panel.setAttribute("role", "alert");
  panel.append(
    element("b", null, "镜头无法预览"),
    element("span", null, problem),
  );
  if (scene?.media_kind === "video" && scene?.asset_id && onGeneratePreview) {
    const generate = button("生成代理预览", "btn secondary");
    generate.addEventListener("click", async () => {
      setButtonBusy(generate, true, "正在生成代理预览…");
      try {
        await onGeneratePreview(scene);
      } catch (error) {
        showActionError(error);
        setButtonBusy(generate, false, "正在生成代理预览…");
      }
    });
    panel.append(generate);
  }
  return panel;
}

function roughMediaElement(
  scene,
  { thumbnail = false, controls = false, onGeneratePreview = null } = {},
) {
  const videoThumbnail = thumbnail && scene?.media_kind === "video";
  const url = videoThumbnail ? scene?.preview_url : thumbnail ? scene?.thumbnail_url : scene?.preview_url;
  if (!scene?.preview_available || !url) return roughMediaError(scene, { onGeneratePreview });
  const media = document.createElement(scene.media_kind === "video" ? "video" : "img");
  media.className = thumbnail ? "rough-thumbnail" : "rough-media";
  media.src = url;
  if (media instanceof HTMLVideoElement) {
    media.preload = "metadata";
    media.playsInline = true;
    media.muted = thumbnail;
    media.controls = controls;
    if (videoThumbnail) media.addEventListener("loadedmetadata", () => { media.currentTime = 0.01; });
  } else {
    media.alt = scene.relative_path || "当前镜头画面";
  }
  return media;
}

function stopRoughPlayback() {
  state.roughPlaying = false;
  if (state.roughAnimationFrame != null) cancelAnimationFrame(state.roughAnimationFrame);
  state.roughAnimationFrame = null;
}

function sceneCardButton(card, selectedId, displayIndex) {
  const control = button("", "scene-card" + (card.card_id === selectedId ? " selected" : ""));
  control.dataset.sceneId = card.card_id;
  control.append(
    roughMediaElement(card, { thumbnail: true }),
    element("b", null, "镜头 " + displayIndex),
    element("em", null, formatTimecode(card.narrative_start) + "–" + formatTimecode(card.narrative_end)),
  );
  return control;
}

function showImpact(edit, applyCandidate = null) {
  return api(
    "/api/tasks/" + encodeURIComponent(state.selectedTaskId) + "/rough-cut/impact",
    jsonRequest("POST", { edit }),
  ).then((impact) => {
    state.pendingImpact = { edit, impact, applyCandidate };
    const details = root.querySelector("#impact-details");
    const affected = (impact.affected_card_ids || []).map((cardId) => {
      const scene = selectScene(state.taskDetail?.sceneCards || [], cardId);
      return scene ? "第 " + scene.index + " 镜头" : "新拆分镜头";
    });
    const invalidated = (impact.invalidated_stages || []).map(workflowLabel);
    details.replaceChildren(
      element("p", null, "影响对象：" + (affected.length ? affected.join("、") : "当前镜头")),
      element("p", null, impact.locked
        ? "画面已经锁定；应用后需要重新完成：" + invalidated.join("、")
        : "画面尚未锁定；本次只修改粗剪画面，不改动数字人口播主轴。"),
    );
    root.querySelector("#impact-modal").hidden = false;
  });
}

function storyboardCandidateMedia(candidate) {
  return {
    asset_id: candidate.assetId,
    media_kind: candidate.mediaKind,
    relative_path: candidate.relativePath,
    preview_available: Boolean(candidate.previewUrl),
    preview_url: candidate.previewUrl,
    thumbnail_url: candidate.previewUrl,
    preview_problem: candidate.previewUrl ? null : "该候选当前没有可用预览。",
  };
}

function renderStoryboardFeedbackPanel(detail, inspector, selected, onPreviewCandidate) {
  if (!state.storyboardFeedback || selected.mixForm === "数字人全屏") return;
  const stored = state.storyboardFeedback[selected.id] || {};
  const panel = element("section", "storyboard-feedback-panel");
  panel.append(
    element("h3", null, "纠正系统推荐"),
    element("p", "storyboard-feedback-help", "拒绝只记录判断；重荐只在本任务在线素材中本地排序；应用前仍需确认影响。"),
  );
  const instruction = document.createElement("textarea");
  instruction.className = "field storyboard-feedback-instruction";
  instruction.placeholder = "例如：不要夜景，改用白天厂房，突出生产空间";
  instruction.setAttribute("aria-label", "分镜拒绝原因或纠正要求");
  instruction.value = stored.instruction || "";
  instruction.addEventListener("input", () => {
    state.storyboardFeedback[selected.id] = {
      ...state.storyboardFeedback[selected.id],
      instruction: instruction.value,
    };
  });
  const actions = element("div", "row-actions storyboard-feedback-actions");
  const reject = button("拒绝当前推荐", "btn danger");
  const rerank = button("按要求重荐", "btn");
  actions.append(reject, rerank);
  panel.append(instruction, actions);

  const rerender = () => renderSceneInspector(detail, inspector, selected, {
    readOnly: false,
    onPreviewCandidate,
  });
  reject.addEventListener("click", async () => {
    try {
      const reason = requireStoryboardInstruction(instruction.value);
      setButtonBusy(reject, true, "正在记录…");
      const result = await api(
        "/api/tasks/" + encodeURIComponent(detail.task.task_id) + "/storyboard/feedback",
        jsonRequest("POST", {
          cardId: selected.id,
          decision: "rejected",
          instruction: reason,
        }),
      );
      state.storyboardFeedback[selected.id] = {
        ...stored,
        instruction: reason,
        rejectedFeedbackId: result.feedbackId,
      };
      showToast("已记录拒绝原因，当前镜头尚未改变。", { kind: "success" });
      rerender();
    } catch (error) {
      setButtonBusy(reject, false);
      showActionError(error);
      instruction.focus();
    }
  });
  rerank.addEventListener("click", async () => {
    try {
      const correction = requireStoryboardInstruction(instruction.value);
      setButtonBusy(rerank, true, "正在本地重排…");
      const result = await api(
        "/api/tasks/" + encodeURIComponent(detail.task.task_id) + "/storyboard/rerank",
        jsonRequest("POST", { cardId: selected.id, instruction: correction }),
      );
      state.storyboardFeedback[selected.id] = {
        ...result,
        instruction: correction,
        selectedAssetId: null,
      };
      showToast("已按纠正要求完成本地重排，当前镜头尚未改变。", { kind: "success" });
      rerender();
    } catch (error) {
      setButtonBusy(rerank, false);
      showActionError(error);
      instruction.focus();
    }
  });

  const view = buildStoryboardFeedbackView(stored);
  if (stored.feedbackId) {
    const candidateList = element("div", "storyboard-feedback-candidates");
    if (!view.candidates.length) {
      candidateList.append(element("p", "empty-inline", view.emptyMessage));
    }
    view.candidates.forEach((candidate) => {
      const control = button("", "storyboard-feedback-candidate");
      control.dataset.assetId = candidate.assetId;
      control.setAttribute("aria-pressed", String(candidate.selected));
      control.append(
        roughMediaElement(storyboardCandidateMedia(candidate), { thumbnail: true }),
        element("b", null, candidate.relativePath || candidate.assetId),
        element("span", null, candidate.sourceDirectory),
        element("p", null, candidate.reason),
      );
      if (candidate.warnings.length) {
        control.append(element("em", null, candidate.warnings.join("；")));
      }
      control.addEventListener("click", () => {
        state.storyboardFeedback[selected.id] = {
          ...state.storyboardFeedback[selected.id],
          selectedAssetId: candidate.assetId,
        };
        if (onPreviewCandidate) onPreviewCandidate(storyboardCandidateMedia(candidate));
        rerender();
      });
      candidateList.append(control);
    });
    const apply = button("应用所选候选", "btn primary");
    apply.disabled = !view.canApply;
    apply.addEventListener("click", async () => {
      const candidate = view.candidates.find((item) => item.selected);
      if (!candidate) return;
      try {
        await showImpact(
          buildSceneEdit("replace", selected, {
            asset_id: candidate.assetId,
            mix_form: candidate.mixForm || (candidate.mediaKind === "image" ? "信息卡" : "broll全屏"),
          }),
          {
            cardId: selected.id,
            feedbackId: view.feedbackId,
            assetId: candidate.assetId,
          },
        );
      } catch (error) { showActionError(error); }
    });
    panel.append(candidateList, apply);
  } else {
    panel.append(element("p", "storyboard-feedback-empty", view.emptyMessage));
  }
  inspector.append(panel);
}

function renderSceneInspector(detail, inspector, selected, {
  readOnly = false,
  onPreviewCandidate = null,
} = {}) {
  inspector.replaceChildren();
  if (!selected) {
    inspector.append(workspaceHeading("当前对象", "请选择一个镜头", "右侧只修改被选中的对象。"));
    return;
  }
  inspector.append(workspaceHeading("当前对象", "第 " + selected.index + " 镜头", selected.replaceScope));
  const facts = element("dl", "object-facts");
  [
    ["旁白", selected.narration],
    ["时间", selected.time],
    ["真实性判断", selected.truthfulness],
  ].forEach(([term, value]) => {
    facts.append(element("dt", null, term), element("dd", null, value));
  });
  inspector.append(facts);
  const technical = element("details", "scene-more-details");
  technical.append(element("summary", null, "查看素材来源与系统建议"));
  const technicalFacts = element("dl", "object-facts");
  [
    ["画面来源", selected.source],
    ["来源类型", selected.sourceType],
    ["系统建议", selected.suggestion],
  ].forEach(([term, value]) => {
    technicalFacts.append(element("dt", null, term), element("dd", null, value));
  });
  technical.append(technicalFacts);
  inspector.append(technical);
  if (readOnly) return;

  renderStoryboardFeedbackPanel(detail, inspector, selected, onPreviewCandidate);

  const replacement = element("details", "replace-panel");
  replacement.open = Boolean(state.previewCandidateId);
  replacement.append(element("summary", null, "需要换画面？查看候选素材"));
  const candidates = (detail.eligibleAssets || []).filter((asset) => (
    ["video", "image"].includes(asset.media_kind) && asset.task_role !== "digital_human"
  ));
  const candidateGrid = element("div", "candidate-grid");
  const selectCandidate = (asset, control) => {
    state.previewCandidateId = asset.asset_id;
    candidateGrid.querySelectorAll(".candidate-card").forEach((item) => {
      item.setAttribute("aria-pressed", item === control ? "true" : "false");
    });
    if (onPreviewCandidate) onPreviewCandidate(asset);
  };
  for (const asset of candidates) {
    const candidate = button("", "candidate-card");
    candidate.dataset.assetId = asset.asset_id;
    candidate.setAttribute("aria-pressed", asset.asset_id === state.previewCandidateId ? "true" : "false");
    candidate.append(
      roughMediaElement(asset, { thumbnail: true }),
      element("b", null, asset.relative_path || asset.asset_id),
      element("span", null, asset.source_mode === "primary" ? "本次主素材" : "通用补充"),
    );
    candidate.addEventListener("click", () => selectCandidate(asset, candidate));
    candidateGrid.append(candidate);
  }
  if (!candidates.length) candidateGrid.append(element("p", "empty-inline", "当前没有可替换的图片或视频素材"));
  const replace = button("用所选候选替换当前镜头", "btn primary");
  replace.addEventListener("click", async () => {
    const asset = candidates.find((item) => item.asset_id === state.previewCandidateId);
    if (!asset) return showToast("请先选择一个替换素材。");
    const mixForm = selected.mixForm === "broll全屏"
      ? "broll全屏"
      : selected.mixForm === "画中画"
        ? "画中画"
        : asset.media_kind === "image" ? "信息卡" : "broll全屏";
    try {
      await showImpact(buildSceneEdit("replace", selected, {
        asset_id: asset.asset_id,
        mix_form: mixForm,
      }));
    } catch (error) { showActionError(error); }
  });
  replacement.append(candidateGrid, replace);
  const aiDetails = element("details", "scene-more-details");
  aiDetails.append(element("summary", null, "了解素材匹配方式"), renderAiDisclosure("match_shot"));
  inspector.append(replacement, aiDetails);
}

function renderRoughCut(detail, primary, inspector, view) {
  const cards = detail.sceneCards || [];
  if (!(state.roughMediaFailures instanceof Set)) state.roughMediaFailures = new Set();
  state.roughMediaFailures = new Set(
    [...state.roughMediaFailures].filter((cardId) => cards.some((card) => card.card_id === cardId)),
  );
  if (!cards.some((item) => item.card_id === state.selectedSceneId)) {
    state.selectedSceneId =
      cards.find((item) => item.mix_form !== "数字人全屏")?.card_id ||
      cards[0]?.card_id ||
      null;
  }
  const selected = selectScene(cards, state.selectedSceneId);
  const selectedCard = cards.find((item) => item.card_id === state.selectedSceneId) || null;
  if (
    selectedCard
    && !sceneAtPlayhead(cards, state.roughPlayhead)
  ) state.roughPlayhead = Number(selectedCard.narrative_start) || 0;
  const heading = workspaceHeading(
    "画面工作区",
    cards.length ? ({
      rough_cut: "逐镜检查粗剪画面",
      picture_order: "调整镜头顺序与时机",
      picture_lock: "确认并锁定画面",
    }[view.viewingStageKey] || "检查画面") : "粗剪尚未生成",
    cards.length
      ? `选择下方镜头查看画面；${roughCutDescription(detail.task?.task_type)}`
      : "等待系统生成镜头后，再逐镜检查。",
  );
  if (!view.readOnly && state.workspaceOverride === "rough-cut") {
    const back = button("返回当前阶段");
    back.addEventListener("click", () => {
      state.workspaceOverride = null;
      renderTaskWorkspace(detail);
    });
    heading.append(back);
  }
  primary.append(heading);
  const preview = element("section", "video-stage");
  const mediaSlot = element("div", "rough-media-slot");
  const caption = element("p", "preview-caption", selected?.narration || "系统生成粗剪后可在这里逐镜确认");
  let mountedSceneId = null;
  let refreshEditorToolbar = () => {};
  const generatePreview = async (scene) => {
    const taskId = state.selectedTaskId;
    const queued = await api(
      "/api/tasks/" + encodeURIComponent(taskId)
      + "/rough-media/" + encodeURIComponent(scene.asset_id) + "/preview",
      jsonRequest("POST"),
    );
    const job = await waitForJob(queued.jobId);
    if (job.status !== "completed") throw new Error(job.problem || "代理预览生成失败，请重试。");
    state.roughMediaFailures.delete(scene.card_id);
    await refreshBootstrap();
    await openTask(taskId);
    showToast("代理预览已生成，可以继续检查并锁定画面。");
  };
  const mountScene = (scene) => {
    const isCandidate = Boolean(scene && !scene.card_id);
    mountedSceneId = scene?.card_id || scene?.asset_id || null;
    if (isCandidate) {
      stopRoughPlayback();
      play.textContent = "播放";
    }
    const media = roughMediaElement(scene, {
      controls: isCandidate,
      onGeneratePreview: generatePreview,
    });
    const markLoaded = () => {
      if (scene?.card_id && state.roughMediaFailures.delete(scene.card_id)) refreshEditorToolbar();
    };
    media.addEventListener?.(media instanceof HTMLVideoElement ? "loadedmetadata" : "load", markLoaded);
    media.addEventListener?.("error", () => {
      if (scene?.card_id) {
        state.roughMediaFailures.add(scene.card_id);
        stopRoughPlayback();
        play.textContent = "播放";
        refreshEditorToolbar();
      }
      mediaSlot.replaceChildren(roughMediaError({
        ...scene,
        preview_problem: "当前视频编码浏览器不支持，请先生成代理预览。",
      }, { onGeneratePreview: generatePreview }));
    });
    if (media instanceof HTMLVideoElement) {
      const syncVideo = () => {
        const target = sourceTimeAtPlayhead(scene, state.roughPlayhead);
        if (Number.isFinite(target) && Math.abs(media.currentTime - target) > 0.25) media.currentTime = target;
      };
      media.addEventListener("loadedmetadata", () => {
        syncVideo();
        if (state.roughPlaying) media.play().catch(() => {});
      });
      media.addEventListener("timeupdate", () => {
        if (!state.roughPlaying) syncVideo();
      });
      media.addEventListener("ended", syncVideo);
    }
    mediaSlot.replaceChildren(media);
    caption.textContent = scene?.narrative_text || selected?.narration || "暂无旁白文本";
  };
  mountScene(selectedCard);
  preview.append(
    element("span", "video-stage-meta", "9:16 · 粗剪代理预览"),
    mediaSlot,
    caption,
  );
  const duration = cards.reduce((latest, card) => Math.max(latest, Number(card.narrative_end) || 0), 0);
  const playback = element("div", "rough-playback");
  const play = button(state.roughPlaying ? "暂停" : "播放", "btn rough-play-toggle");
  const scrubber = document.createElement("input");
  scrubber.type = "range";
  scrubber.min = "0";
  scrubber.max = String(duration);
  scrubber.step = "0.01";
  scrubber.value = String(Math.min(duration, Math.max(0, state.roughPlayhead)));
  scrubber.setAttribute("aria-label", "粗剪全局播放头");
  const timeLabel = element("output", "rough-time", formatTimecode(state.roughPlayhead) + " / " + formatTimecode(duration));
  const updatePlayhead = (time) => {
    state.roughPlayhead = Math.min(duration, Math.max(0, Number(time) || 0));
    scrubber.value = String(state.roughPlayhead);
    timeLabel.textContent = formatTimecode(state.roughPlayhead) + " / " + formatTimecode(duration);
    const scene = sceneAtPlayhead(cards, state.roughPlayhead);
    if (scene && (scene.card_id || scene.asset_id) !== mountedSceneId) {
      state.selectedSceneId = scene.card_id;
      state.previewCandidateId = null;
      mountScene(scene);
      renderSceneInspector(detail, inspector, selectScene(cards, scene.card_id), {
        readOnly: view.readOnly,
        onPreviewCandidate: mountScene,
      });
      refreshEditorToolbar();
      primary.querySelectorAll(".scene-card").forEach((card) => {
        card.classList.toggle("selected", card.dataset.sceneId === scene.card_id);
      });
    }
    const media = mediaSlot.querySelector("video");
    if (media && scene) {
      const target = sourceTimeAtPlayhead(scene, state.roughPlayhead);
      if (Number.isFinite(target) && Math.abs(media.currentTime - target) > 0.25) media.currentTime = target;
    }
  };
  const tick = (now) => {
    if (!state.roughPlaying) return;
    const elapsed = state.roughClockAt ? (now - state.roughClockAt) / 1000 : 0;
    state.roughClockAt = now;
    const next = advancePlayhead(cards, state.roughPlayhead, elapsed);
    updatePlayhead(next.playhead);
    if (next.ended) {
      stopRoughPlayback();
      play.textContent = "播放";
      return;
    }
    state.roughAnimationFrame = requestAnimationFrame(tick);
  };
  play.addEventListener("click", () => {
    if (state.roughPlaying) {
      stopRoughPlayback();
      mediaSlot.querySelector("video")?.pause();
      play.textContent = "播放";
      return;
    }
    if (state.roughPlayhead >= duration) updatePlayhead(0);
    state.roughPlaying = true;
    state.roughClockAt = performance.now();
    mediaSlot.querySelector("video")?.play().catch(() => {});
    play.textContent = "暂停";
    state.roughAnimationFrame = requestAnimationFrame(tick);
  });
  scrubber.addEventListener("input", () => {
    updatePlayhead(scrubber.value);
    const video = mediaSlot.querySelector("video");
    if (video && state.roughPlaying) video.play().catch(() => {});
  });
  playback.append(play, scrubber, timeLabel);
  preview.append(playback);
  primary.append(preview);
  let toolbar = null;
  if (!view.readOnly) {
    toolbar = element("div", "editor-toolbar");
    const undo = button("↶ 撤销");
    const redo = button("↷ 重做");
    undo.addEventListener("click", () => runButtonAction(undo, "正在撤销…", async () => {
      await taskPost("/rough-cut/undo");
      showToast("撤销操作已完成。");
    }));
    redo.addEventListener("click", () => runButtonAction(redo, "正在重做…", async () => {
      await taskPost("/rough-cut/redo");
      showToast("重做操作已完成。");
    }));
    refreshEditorToolbar = () => {
      toolbar.replaceChildren(undo, redo);
      const advanced = element("details", "editor-advanced");
      advanced.open = view.viewingStageKey === "picture_order";
      advanced.append(element("summary", null, "调整所选镜头"));
      const advancedActions = element("div", "editor-advanced-actions");
      const activeSelected = selectScene(cards, state.selectedSceneId);
      if (activeSelected) {
        const raw = cards.find((item) => item.card_id === activeSelected.id);
        const fixedAnchor = raw?.fixed_anchor === true || activeSelected.mixForm === "数字人全屏";
        if (!fixedAnchor) {
          const currentIndex = cards.findIndex((item) => item.card_id === activeSelected.id);
          const reorderCards = cards.map((card) => ({
            ...card,
            fixed_anchor: card.fixed_anchor === true || card.mix_form === "数字人全屏",
          }));
          const requestMove = async (targetIndex) => {
            try {
              const ordered = moveCardId(reorderCards, activeSelected.id, targetIndex);
              await showImpact(buildSceneEdit("reorder", activeSelected, { card_ids: ordered }));
            } catch (error) { showActionError(error); }
          };
          const moveLeft = button("左移");
          moveLeft.dataset.moveDirection = "left";
          moveLeft.disabled = currentIndex <= 0;
          moveLeft.addEventListener("click", () => requestMove(currentIndex - 1));
          const moveRight = button("右移");
          moveRight.dataset.moveDirection = "right";
          moveRight.disabled = currentIndex >= cards.length - 1;
          moveRight.addEventListener("click", () => requestMove(currentIndex + 1));

          let trim = null;
          if (raw?.segment_id) {
            trim = element("div", "trim-controls");
            const enter = document.createElement("input");
            enter.type = "number";
            enter.step = "0.01";
            enter.value = String(Number.isFinite(Number(raw.resolved_source_start))
              ? Number(raw.resolved_source_start) : activeSelected.sourceStart);
            enter.setAttribute("aria-label", "来源入点（秒）");
            const exit = document.createElement("input");
            exit.type = "number";
            exit.step = "0.01";
            exit.value = String(Number.isFinite(Number(raw.resolved_source_end))
              && Number(raw.resolved_source_end) > Number(enter.value)
              ? Number(raw.resolved_source_end)
              : activeSelected.sourceEnd);
            exit.setAttribute("aria-label", "来源出点（秒）");
            const applyTrim = button("应用裁剪");
            applyTrim.dataset.editAction = "trim";
            applyTrim.addEventListener("click", async () => {
              try {
                const bounds = trimBounds(raw, enter.value, exit.value);
                await showImpact(buildSceneEdit("trim", activeSelected, {
                  enter_tc: bounds.enterTc,
                  exit_tc: bounds.exitTc,
                }));
              } catch (error) { showActionError(error); }
            });
            trim.append(enter, exit, applyTrim);
          }

          const split = button("在播放头拆分");
          split.dataset.editAction = "split";
          split.addEventListener("click", async () => {
            try {
              await showImpact(buildSceneEdit("split", activeSelected, {
                playhead: state.roughPlayhead,
              }));
            } catch (error) { showActionError(error); }
          });
          const remove = button("删除画面片段");
          remove.dataset.editAction = "delete";
          remove.addEventListener("click", async () => {
            try { await showImpact(buildSceneEdit("delete", activeSelected)); } catch (error) { showActionError(error); }
          });
          advancedActions.append(moveLeft, moveRight);
          if (trim) advancedActions.append(trim);
          advancedActions.append(split, remove);
        }
      }
      if (advancedActions.childElementCount) {
        advanced.append(advancedActions);
        toolbar.append(advanced);
      }
      const lock = button("确认粗剪并锁定画面", "btn primary");
      lock.dataset.pictureLock = "";
      const blockers = pictureLockBlockers(cards, state.roughMediaFailures);
      lock.disabled = blockers.length > 0;
      lock.title = blockers.join("；");
      lock.addEventListener("click", () => runButtonAction(lock, "正在锁定画面…", async () => {
          try {
            await taskPost("/picture/lock", {
              mediaVersions: mediaVersionsForCards(cards),
            });
            showToast("画面已锁定，请继续确认包装方案。");
          } catch (error) {
            const failed = cards.find((card) => card.asset_id === error.details?.assetId);
            if (error.code === "ROUGH_MEDIA_UNSUPPORTED" && failed) {
              state.selectedSceneId = failed.card_id;
              state.roughMediaFailures.add(failed.card_id);
              mediaSlot.replaceChildren(roughMediaError({
                ...failed,
                preview_problem: error.message,
              }, { onGeneratePreview: generatePreview }));
              caption.textContent = failed.narrative_text || "当前镜头需要代理预览";
              refreshEditorToolbar();
            }
            throw error;
          }
      }));
      toolbar.append(lock);
      if (blockers.length) {
        toolbar.append(element("p", "picture-lock-blockers", blockers.join("；")));
      }
    };
    refreshEditorToolbar();
  }
  primary.append(element("h3", "timeline-title", `镜头列表 · ${cards.length} 个`));
  const timeline = element("div", "scene-timeline");
  const zeroBased = cards.some((item) => Number(item.idx) === 0);
  cards.forEach((card) => {
    const displayIndex = Number(card.idx) + (zeroBased ? 1 : 0);
    const control = sceneCardButton(card, state.selectedSceneId, displayIndex);
    const fixedAnchor = card.fixed_anchor === true || card.mix_form === "数字人全屏";
    control.draggable = !fixedAnchor;
    if (!fixedAnchor) {
      control.addEventListener("dragstart", (event) => {
        event.dataTransfer?.setData("text/plain", card.card_id);
        if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
      });
      control.addEventListener("dragover", (event) => event.preventDefault());
      control.addEventListener("drop", async (event) => {
        event.preventDefault();
        const movingId = event.dataTransfer?.getData("text/plain");
        const moving = cards.find((item) => item.card_id === movingId);
        if (!moving || moving.card_id === card.card_id || moving.mix_form === "数字人全屏" || moving.fixed_anchor) return;
        const targetIndex = cards.findIndex((item) => item.card_id === card.card_id);
        try {
          const ordered = moveCardId(cards.map((item) => ({
            ...item,
            fixed_anchor: item.fixed_anchor === true || item.mix_form === "数字人全屏",
          })), moving.card_id, targetIndex);
          const movingScene = selectScene(cards, moving.card_id);
          await showImpact(buildSceneEdit("reorder", movingScene, { card_ids: ordered }));
        } catch (error) { showActionError(error); }
      });
    }
    control.addEventListener("click", () => {
      stopRoughPlayback();
      state.selectedSceneId = card.card_id;
      state.previewCandidateId = null;
      state.roughPlayhead = Number(card.narrative_start) || 0;
      renderTaskWorkspace(detail);
    });
    timeline.append(control);
  });
  if (!cards.length) timeline.append(element("div", "empty-inline", "等待粗剪任务生成真实场景卡片"));
  primary.append(timeline);
  if (toolbar) primary.append(toolbar);
  renderSceneInspector(detail, inspector, selected, {
    ...view,
    onPreviewCandidate: mountScene,
  });
}

function renderPlanInspector(detail, inspector, plan, { readOnly = false } = {}) {
  inspector.replaceChildren();
  if (!plan) {
    inspector.append(workspaceHeading("当前方案", "请先生成或选择方案"));
    return;
  }
  const taskId = detail.task.task_id;
  const previewKey = packagingPreviewKey(taskId, plan.plan_id);
  const preview = state.packagingPreviews[previewKey];
  if (preview === undefined && !state.loadingPackagingPreviews.has(previewKey)) {
    void loadPackagingPreview(taskId, plan.plan_id);
  }
  const previewModel = buildPackagingPreviewModel({
    taskId,
    planDigest: preview?.planDigest,
    pictureLockDigest: preview?.pictureLockDigest,
    preview,
  });
  inspector.append(workspaceHeading(
    "应用前预览",
    plan.plan_name,
    "这里播放真实短片；完整观看后才能确认并进入精剪。",
  ));

  const previewStage = element("section", "packaging-preview-stage");
  previewStage.setAttribute("aria-busy", String(state.loadingPackagingPreviews.has(previewKey)));
  const statusLabels = {
    not_requested: "尚未生成真实包装预览",
    queued: "预览已排队，等待本机渲染",
    rendering: "正在渲染真实包装预览",
    ready: preview?.viewedAt ? "真实预览已完整观看" : "真实预览已就绪，请播放至 90%",
    stale: "方案或画面已变化，旧预览不可确认",
    failed: "预览渲染失败",
    missing: "预览文件缺失，需要重新生成",
  };
  if (state.loadingPackagingPreviews.has(previewKey) && preview === undefined) {
    previewStage.append(inlineFeedback("正在读取预览状态…", "不会改变当前方案。"));
  } else if (previewModel.status === "ready" && previewModel.mediaUrl) {
    const video = document.createElement("video");
    video.className = "packaging-preview-video";
    video.controls = true;
    video.preload = "metadata";
    video.src = preview.mediaUrl || previewModel.mediaUrl;
    video.setAttribute("data-packaging-preview", previewModel.previewId);
    video.setAttribute("playsinline", "");
    const markViewed = async () => {
      const duration = Number(video.duration);
      const currentTime = Number(video.currentTime);
      const viewedEnough = duration > 0 && currentTime / duration >= 0.9;
      if (!viewedEnough || preview?.viewedAt) return;
      if (state.packagingViewed.has(previewModel.previewId)) return;
      state.packagingViewed.add(previewModel.previewId);
      try {
        const viewed = await api(
          "/api/tasks/" + encodeURIComponent(taskId)
          + "/packaging-previews/" + encodeURIComponent(preview.previewId) + "/viewed",
          jsonRequest("POST", {}),
        );
        state.packagingPreviews[previewKey] = viewed;
        showToast("已记录完整观看，现在可以确认包装方案。");
        rerenderSelectedPackaging(taskId, plan.plan_id);
      } catch (error) {
        state.packagingViewed.delete(previewModel.previewId);
        showActionError(error);
      }
    };
    video.addEventListener("timeupdate", markViewed);
    video.addEventListener("ended", markViewed);
    previewStage.append(video);
  } else {
    previewStage.append(element("div", "packaging-preview-placeholder", statusLabels[previewModel.status] || "预览状态待确认"));
  }
  previewStage.append(
    element("b", "packaging-preview-status", statusLabels[previewModel.status] || "预览状态待确认"),
    element("p", "packaging-preview-disclaimer", previewModel.disclaimer),
  );
  if (preview?.message || preview?.errorCode) {
    previewStage.append(inlineFeedback("无法使用当前预览", preview.message || preview.errorCode, "error"));
  }
  if (!readOnly && previewModel.status !== "ready") {
    const requestPreview = button(
      ["stale", "failed", "missing"].includes(previewModel.status) ? "重新生成真实预览" : "生成真实预览",
      "btn primary full",
    );
    requestPreview.dataset.action = "request-packaging-preview";
    requestPreview.disabled = state.loadingPackagingPreviews.has(previewKey)
      || ["queued", "rendering"].includes(previewModel.status);
    requestPreview.addEventListener("click", () => requestPackagingPreview(taskId, plan.plan_id, requestPreview));
    previewStage.append(requestPreview);
  }
  if (["queued", "rendering"].includes(previewModel.status)) {
    schedulePackagingPreviewRefresh(taskId, plan.plan_id);
  }
  inspector.append(previewStage);

  const planDetails = element("details", "plan-customization");
  planDetails.append(element("summary", null,
    plan.origin_type === "system_recommendation" ? "查看这套方案包含什么" : "查看与调整包装素材"));
  const slots = element("div", "pack-slots");
  const rows = slotRows(plan.items || [], { categories: state.bootstrap.packagingCatalog || [] });
  for (const item of rows) {
    const row = element("div", "pack-slot");
    row.append(
      element("b", null, item.label + (item.required ? " · 必选" : "")),
      element("span", null, item.selected?.name || item.assetRef || "未启用"),
    );
    const selectedAsset = item.selected;
    if (selectedAsset?.previewUrl && selectedAsset?.previewKind) {
      const media = element(selectedAsset.previewKind === "image" ? "img" : selectedAsset.previewKind);
      media.setAttribute("data-packaging-slot-preview", item.key);
      media.className = "packaging-slot-preview";
      if (selectedAsset.previewKind === "image") {
        media.alt = (selectedAsset.name || item.label) + "预览";
        media.loading = "lazy";
      } else {
        media.controls = true;
        media.preload = selectedAsset.previewKind === "video" ? "metadata" : "none";
        if (selectedAsset.previewKind === "video") media.setAttribute("playsinline", "");
      }
      media.src = selectedAsset.previewUrl;
      row.append(media);
    }
    slots.append(row);
  }
  planDetails.append(slots);
  if (!readOnly && plan.origin_type !== "system_recommendation") {
    const controls = element("section", "replace-panel");
    controls.append(element("h3", null, "组合包装素材"));
    const slotSelect = document.createElement("select");
    slotSelect.className = "field";
    slotSelect.setAttribute("aria-label", "包装槽位");
    SLOT_DEFINITIONS.forEach((definition) => {
      const option = document.createElement("option");
      option.value = definition.key;
      option.textContent = definition.label + (definition.required ? " · 必选" : "");
      slotSelect.append(option);
    });
    const assetSelect = document.createElement("select");
    assetSelect.className = "field";
    assetSelect.setAttribute("aria-label", "包装素材");
    const updateAssets = () => {
      const category = (state.bootstrap.packagingCatalog || []).find((item) => item.key === slotSelect.value);
      assetSelect.replaceChildren(...(category?.assets || []).map((asset) => {
        const option = document.createElement("option");
        option.value = asset.assetRef;
        option.textContent = asset.name + (asset.authorizationStatus === "approved" ? "" : " · 授权待核验");
        option.disabled = asset.authorizationStatus !== "approved";
        return option;
      }));
    };
    slotSelect.addEventListener("change", updateAssets);
    updateAssets();
    const replace = button("更新当前组合");
    replace.dataset.action = "replace-packaging-slot";
    replace.addEventListener("click", async () => {
      if (!assetSelect.value) return showToast("当前分类没有已授权的可用素材。");
      try {
        delete state.packagingPreviews[previewKey];
        await taskPost("/packaging/compose", {
          planId: plan.plan_id,
          slot: slotSelect.value,
          assetRef: assetSelect.value,
        });
        showToast("包装素材已更新，请重新生成真实预览。");
      } catch (error) { showActionError(error); }
    });
    const disable = button("关闭这个可选项", "btn ghost");
    disable.setAttribute("data-action", "disable-packaging-slot");
    const updateDisable = () => {
      const row = rows.find((item) => item.key === slotSelect.value);
      disable.hidden = !row?.canDisable;
      disable.disabled = !row?.canDisable || !row?.enabled;
    };
    slotSelect.addEventListener("change", updateDisable);
    updateDisable();
    disable.addEventListener("click", async () => {
      try {
        delete state.packagingPreviews[previewKey];
        await taskPost("/packaging/compose", {
          planId: plan.plan_id,
          slot: slotSelect.value,
          enabled: false,
        });
        showToast("可选包装已关闭，请重新生成真实预览。");
      } catch (error) { showActionError(error); }
    });
    controls.append(slotSelect, assetSelect, replace, disable);
    planDetails.append(controls);
  }
  if (!readOnly) {
    const templatePanel = element("section", "template-save-panel");
    const templateName = document.createElement("input");
    templateName.className = "field";
    templateName.value = plan.plan_name || "我的包装方案";
    templateName.setAttribute("aria-label", "模板名称");
    const saveTemplate = button("保存为我的模板");
    saveTemplate.setAttribute("data-action", "save-packaging-template");
    saveTemplate.addEventListener("click", async () => {
      const name = templateName.value.trim();
      if (!name) return showToast("请填写模板名称。", { kind: "error" });
      setButtonBusy(saveTemplate, true, "正在保存…");
      try {
        await taskPost(
          "/packaging/" + encodeURIComponent(plan.plan_id) + "/save-template",
          { templateName: name },
        );
        await loadPackagingTemplates(detail.task.task_type, { force: true });
        showToast("已保存为个人包装模板。");
      } catch (error) { showActionError(error); }
      finally { setButtonBusy(saveTemplate, false, "正在保存…"); }
    });
    templatePanel.append(templateName, saveTemplate);
    planDetails.append(templatePanel);

    const confirm = button("确认使用这个包装方案", "btn primary full");
    confirm.setAttribute("data-action", "confirm-packaging");
    confirm.disabled = !previewModel.canConfirm;
    if (!previewModel.canConfirm) confirm.title = "请先生成真实预览并播放至 90%";
    confirm.addEventListener("click", () => runButtonAction(confirm, "正在确认方案…", async () => {
        await taskPost(
          "/packaging/" + encodeURIComponent(plan.plan_id) + "/confirm",
          { previewId: preview.previewId },
        );
        showToast("包装方案已确认，真实预览记录已写入任务账本。");
    }));
    inspector.append(planDetails, confirm);
  } else {
    inspector.append(planDetails);
  }
}

function appendMaterialAuthorizationPanel(detail, primary, view, action) {
  if (view.readOnly || !action.visible) return;
  const panel = element("section", "material-authorization-action");
  panel.append(element(
    "p",
    null,
    "团队自摄素材可以一次登记并供以后任务复用；其他已获授权的素材只确认用于本片。两种确认都要求先核对当前镜头。",
  ));
  const confirmSelfProduced = button(action.selfProducedLabel, "btn primary");
  const confirmAuthorization = button(action.label, "btn");
  const submitAuthorization = async (control, basis) => {
    setButtonBusy(control, true, "正在记录素材来源…");
    try {
      await taskPost("/material-authorization", { confirmedBy: "本机同事", basis });
      showToast(basis === "self_produced"
        ? "自摄素材已登记；请继续本片制作。若已有旧候选，需重新渲染并质检。"
        : "本片授权证据已记录；若已有旧候选，需重新渲染并质检。");
    } catch (error) {
      setButtonBusy(control, false);
      showActionError(error);
    }
  };
  confirmSelfProduced.addEventListener("click", () => submitAuthorization(confirmSelfProduced, "self_produced"));
  confirmAuthorization.addEventListener("click", () => submitAuthorization(confirmAuthorization, "task_only"));
  panel.append(confirmSelfProduced, confirmAuthorization);
  primary.append(panel);
}

function renderPackaging(detail, primary, inspector, view) {
  const route = detail.task.task_type;
  if (!Object.hasOwn(state.packagingTemplates, route) && !state.loadingPackagingTemplates.has(route)) {
    void loadPackagingTemplates(route);
  }
  const templates = state.packagingTemplates[route] || [];
  const model = buildPackagingWorkspace(detail.packagingPlans || [], state.selectedPlanId, { templates });
  state.selectedPlanId = model.selected?.planId || null;
  primary.append(workspaceHeading(
    "包装方案确认",
    "选择推荐方案，或复制后自己组合",
    "包装素材库是长期资产；这里的包装方案只属于当前这条视频。",
  ));
  if (!view.readOnly) {
    const topActions = element("div", "row-actions");
    const propose = button(model.recommended.length ? "重新生成推荐方案" : "生成推荐方案", "btn primary");
    propose.addEventListener("click", () => runButtonAction(propose, "正在生成推荐方案…", async () => {
      await taskPost("/packaging/propose");
      showToast("推荐方案已生成，已选中新方案。请生成真实预览。");
    }));
    topActions.append(propose);
    const moreActions = element("details", "packaging-more-actions");
    moreActions.append(element("summary", null, "其他创建方式"));
    const moreActionsRow = element("div", "row-actions");
    const blank = button("从空白开始");
    blank.setAttribute("data-action", "create-blank-packaging");
    blank.addEventListener("click", () => runButtonAction(blank, "正在创建…", async () => {
      await taskPost("/packaging/compose", { planName: "从空白开始" });
      showToast("空白组合已创建，请在右侧选择包装素材。");
    }));
    moreActionsRow.append(blank);
    if (model.selected) {
      const clone = button("复制为自选组合");
      clone.addEventListener("click", () => runButtonAction(clone, "正在复制…", async () => {
          await taskPost("/packaging/compose", {
            cloneFrom: model.selected.planId,
            planName: "我的自选组合",
          });
          showToast("已选中新复制的组合，可在右侧编辑。");
      }));
      moreActionsRow.append(clone);
    }
    if ((detail.sceneCards || []).length) {
      const revise = button("修改已锁定画面");
      revise.addEventListener("click", () => {
        state.workspaceOverride = "rough-cut";
        state.viewingStageKey = null;
        renderTaskWorkspace(detail);
      });
      moreActionsRow.append(revise);
    }
    moreActions.append(moreActionsRow);
    topActions.append(moreActions);
    primary.append(topActions);
  }
  const groups = [
    ["系统推荐", model.recommended],
    ["我的自选组合", model.custom],
  ];
  const appendPlanCards = (target, plans) => {
    plans.forEach((plan) => {
      const control = button("", "plan-card" + (plan.planId === state.selectedPlanId ? " selected" : ""));
      control.append(
        element("b", null, plan.name),
        element("span", null, plan.itemCount + " 项包装素材"),
        element("em", null, plan.status === "confirmed" ? "已确认" : "点击预览"),
      );
      control.addEventListener("click", () => {
        state.selectedPlanId = plan.planId;
        renderTaskWorkspace(detail);
      });
      target.append(control);
    });
  };
  for (const [label, plans] of groups) {
    const section = element(label === "系统推荐" ? "section" : "details", "plan-group");
    if (section.tagName === "DETAILS") {
      section.open = plans.some((plan) => plan.planId === state.selectedPlanId);
      section.append(element("summary", null, `${label} · ${plans.length} 套`));
    } else {
      section.append(element("h3", null, label));
    }
    const cards = element("div", "plan-grid");
    const partition = label === "系统推荐"
      ? partitionRecommendedPlans(plans, state.selectedPlanId)
      : { visible: plans, history: [] };
    appendPlanCards(cards, partition.visible);
    if (!plans.length) cards.append(element("div", "empty-inline", "暂无方案"));
    section.append(cards);
    if (partition.history.length) {
      const history = element("details", "plan-history");
      history.append(element("summary", null, `其他推荐记录 · ${partition.history.length} 套`));
      const historyCards = element("div", "plan-grid");
      appendPlanCards(historyCards, partition.history);
      history.append(historyCards);
      section.append(history);
    }
    primary.append(section);
  }
  const templateSection = element("details", "plan-group");
  templateSection.append(element("summary", null, "我的模板"));
  const templateCards = element("div", "plan-grid");
  if (state.loadingPackagingTemplates.has(route)) {
    templateCards.setAttribute("aria-busy", "true");
    templateCards.append(element("div", "empty-inline", "正在读取个人模板…"));
  } else if (!templates.length) {
    templateCards.append(element("div", "empty-inline", "还没有个人模板，可从右侧把当前方案保存为模板。"));
  } else {
    templates.forEach((template) => {
      const useTemplate = button("", "plan-card");
      useTemplate.append(
        element("b", null, template.template_name),
        element("span", null, (template.items || []).length + " 项包装素材"),
        element("em", null, "点击复制到当前任务"),
      );
      useTemplate.addEventListener("click", async () => {
        try {
          const result = await taskPost("/packaging/compose", {
            cloneFrom: template.template_id,
            planName: template.template_name,
          });
          state.selectedPlanId = result.planId;
          await openTask(detail.task.task_id);
          showToast("模板已复制为当前任务的可编辑方案。");
        } catch (error) { showActionError(error); }
      });
      templateCards.append(useTemplate);
    });
  }
  templateSection.append(templateCards);
  primary.append(templateSection);
  const rawPlan = (detail.packagingPlans || []).find((item) => item.plan_id === state.selectedPlanId);
  renderPlanInspector(detail, inspector, rawPlan, view);
}

function displayCandidateLabel(value) {
  const raw = String(value || "");
  const version = /^candidate-v(\d+)$/i.exec(raw);
  return version ? `成片版本 ${version[1]}` : raw;
}

function renderReview(detail, primary, inspector, view) {
  const taskId = detail.task.task_id;
  const stageKey = view.viewingStageKey;
  const history = buildCandidateHistory(detail, state.selectedReviewCandidateId);
  const selected = history.selected;
  const current = history.current;
  if (selected) state.selectedReviewCandidateId = selected.candidateId;
  const candidateLabel = (candidate) => displayCandidateLabel(candidate?.versionLabel || candidate?.candidateId);

  primary.append(workspaceHeading(
    "精剪候选版本",
    selected ? candidateLabel(selected) : "等待生成首个候选",
    "每次改变成片内容都会创建新版本；历史版本、QA、审片和发布包只读保留。",
  ));
  const candidateTabs = element("div", "candidate-version-list");
  history.items.forEach((candidate) => {
    const control = button(candidateLabel(candidate), "candidate-version");
    control.setAttribute("aria-pressed", String(candidate.candidateId === selected?.candidateId));
    control.append(element(
      "span",
      candidate.readOnly ? "candidate-readonly" : "candidate-current",
      candidate.readOnly ? "历史只读" : "当前版本",
    ));
    control.addEventListener("click", () => {
      state.selectedReviewCandidateId = candidate.candidateId;
      renderTaskWorkspace(detail);
    });
    candidateTabs.append(control);
  });
  if (history.items.length) primary.append(candidateTabs);

  const stage = element("section", "review-stage");
  let reviewVideo = null;
  const mediaUrl = reviewMediaUrl(taskId, selected);
  if (mediaUrl) {
    reviewVideo = document.createElement("video");
    reviewVideo.className = "review-video";
    reviewVideo.controls = true;
    reviewVideo.preload = "metadata";
    reviewVideo.src = mediaUrl;
    reviewVideo.setAttribute("playsinline", "");
    stage.append(reviewVideo);
  } else {
    stage.append(element("div", "review-screen", selected ? "该候选尚未通过自动 QA" : "尚无候选成片"));
  }
  stage.append(element(
    "p", "review-version-status",
    selected
      ? `${candidateLabel(selected)} · ${selected.status === "qa_blocked" ? "质检有阻断" : selected.qa?.status === "passed" ? "自动质检已通过" : "等待质检结果"}`
      : "精剪和自动 QA 完成后显示候选成片",
  ));
  if (!selected) appendMaterialAuthorizationPanel(detail, primary, view, materialAuthorizationSetup(detail));
  primary.append(stage);

  const qaGroups = groupQaIssues(selected);
  const qaSection = element("details", "qa-panel");
  qaSection.open = stageKey === "auto_qa" || qaGroups.blocking.length > 0;
  qaSection.append(element("summary", null,
    `自动 QA · ${qaGroups.blocking.length} 阻断 / ${qaGroups.warning.length} 警告 / ${qaGroups.passed.length} 通过`));
  qaSection.append(workspaceHeading(
    "自动 QA",
    selected?.qa?.status === "passed" ? "已通过自动检查" : "先处理阻断，再复核警告",
    "质检结果按处理优先级排列；通过项和原始技术信息可展开查看。",
  ));
  const qaBoard = element("div", "qa-groups");
  [["blocking", "阻断项"], ["warning", "警告项"], ["passed", "通过项"]].forEach(([key, label]) => {
    const collapsible = key === "passed";
    const group = element(collapsible ? "details" : "section", `qa-group ${key}`);
    if (collapsible) {
      group.append(element("summary", null, `${label} ${qaGroups[key].length} · 点击查看检查证据`));
    } else {
      group.append(element("h3", null, `${label} ${qaGroups[key].length}`));
    }
    if (!qaGroups[key].length) group.append(element("p", "qa-empty", "当前没有" + label));
    qaGroups[key].forEach((issue) => {
      const presentation = describeQaIssue(issue);
      const row = element("article", "qa-issue");
      const copy = element("div", "qa-issue-copy");
      copy.append(
        element("b", null, presentation.title),
        element("p", null, presentation.summary),
      );
      row.append(copy);
      if (presentation.technical && presentation.technical !== presentation.summary) {
        const technical = element("details", "qa-technical");
        technical.append(
          element("summary", null, "查看具体素材与技术记录"),
          element("p", null, presentation.technical),
        );
        row.append(technical);
      }
      const seekSeconds = qaIssueSeekSeconds(issue);
      if (key !== "passed" && reviewVideo && seekSeconds !== null) {
        const seek = button(`定位到 ${seekSeconds.toFixed(1)} 秒`, "btn compact qa-seek");
        seek.addEventListener("click", () => {
          reviewVideo.currentTime = seekSeconds;
          reviewVideo.focus();
          reviewVideo.scrollIntoView({ behavior: "smooth", block: "center" });
        });
        row.append(seek);
      }
      group.append(row);
    });
    qaBoard.append(group);
  });
  qaSection.append(qaBoard);
  primary.append(qaSection);

  const authorizationAction = materialAuthorizationAction(detail, selected);
  appendMaterialAuthorizationPanel(detail, primary, view, authorizationAction);

  const candidateGate = reviewGate(
    selected,
    selected?.isCurrent ? detail.watchSession : null,
    detail.workflow.gates,
  );
  const renderAction = reviewRenderAction(detail, current);
  if (stageKey === "full_review" || (!view.readOnly && renderAction.visible)) {
    primary.append(workspaceHeading(
      stageKey === "full_review" ? "完整审片" : "本步操作",
      stageKey === "full_review"
        ? (selected?.readOnly ? "历史候选仅供查看" : "只批准完整观看后的当前候选")
        : "生成或重新检查候选成片",
      stageKey === "full_review"
        ? "候选变化后观看进度归零；旧观看会话和批准记录不会继承。"
        : "这一步会生成新候选，旧版本仍保留供回看。",
    ));
  }
  const actions = element("div", "row-actions review-actions");
  let progressLabel = null;
  let currentWatchSession = selected?.isCurrent ? detail.watchSession || null : null;
  let watchController = null;
  const snapshotId = selected?.qaSnapshotId;
  const qaReportId = selected?.qaReportId;
  const showWatchControls = (session) => {
    currentWatchSession = session;
    const progress = Math.round(Number(session.coverage_ratio || 0) * 100);
    progressLabel = element("span", "watch-progress", "完整观看进度 " + progress + "%");
    const approve = button("确认成片可发布", "btn primary");
    approve.disabled = Boolean(detail.workflow.gates?.human_reviewed)
      || !reviewGate(selected, session, detail.workflow.gates).canApprove;
    approve.addEventListener("click", async () => {
      setButtonBusy(approve, true, "正在确认…");
      try {
        await taskPost("/review/approve", {
          candidateId: selected.candidateId,
          snapshotId,
          qaReportId,
          watchSessionId: currentWatchSession.session_id,
          reviewedBy: "本机同事",
        });
      } catch (error) {
        setButtonBusy(approve, false);
        showActionError(error);
      }
    });
    actions.replaceChildren(progressLabel, approve);
  };
  const startWatchSession = async () => {
    const session = await api(
      "/api/tasks/" + encodeURIComponent(taskId) + "/watch/start",
      jsonRequest("POST", { candidateId: selected.candidateId, snapshotId, qaReportId }),
    );
    showWatchControls(session);
    return session;
  };
  if (!view.readOnly) {
    if (renderAction.visible) {
      const render = button(renderAction.label, "btn primary");
      render.addEventListener("click", async () => {
        try { await taskPost("/render"); } catch (error) { showActionError(error); }
      });
      actions.append(render);
    } else if (stageKey !== "full_review") {
      // Other review stages show their own result without a watch control.
    } else if (selected?.readOnly) {
      actions.append(element("span", "watch-progress", "历史候选不可重新批准"));
    } else if (candidateGate.canStartWatch) {
      const startWatchButton = button("开始完整审片", "btn primary");
      startWatchButton.addEventListener("click", async () => {
        setButtonBusy(startWatchButton, true, "正在开始…");
        try { await watchController.ensureStarted(); }
        catch (error) { setButtonBusy(startWatchButton, false); showActionError(error); }
      });
      actions.append(startWatchButton);
    } else if (detail.watchSession) {
      showWatchControls(detail.watchSession);
    } else {
      actions.append(element("span", "watch-progress", "请先处理自动 QA 阻断项"));
    }
    if (actions.childElementCount) primary.append(actions);
  }
  if (!view.readOnly && stageKey === "full_review" && reviewVideo
    && selected?.isCurrent && snapshotId && qaReportId
    && (candidateGate.canStartWatch || detail.watchSession)) {
    watchController = createPlaybackWatchController(
      currentWatchSession,
      startWatchSession,
      async (action) => {
        const tick = await api(
          "/api/tasks/" + encodeURIComponent(taskId)
            + "/watch/" + encodeURIComponent(action.sessionId) + "/tick",
          jsonRequest("POST", { positionSeconds: action.positionSeconds }),
        );
        if (progressLabel) progressLabel.textContent = "完整观看进度 "
          + Math.round(Number(tick.coverage_ratio || 0) * 100) + "%";
        if (action.completeAfterTick) {
          await api(
            "/api/tasks/" + encodeURIComponent(taskId)
              + "/watch/" + encodeURIComponent(action.sessionId) + "/complete",
            jsonRequest("POST"),
          );
          await refreshBootstrap();
          await openTask(taskId);
        }
      },
    );
    const reportPlayback = (ended = false) => {
      watchController.report(reviewVideo.currentTime, reviewVideo.duration, ended).catch(showActionError);
    };
    reviewVideo.addEventListener("play", async () => {
      if (!watchController.requiresStart()) return;
      reviewVideo.pause();
      try { await watchController.ensureStarted(); await reviewVideo.play(); }
      catch (error) { showActionError(error); }
    });
    reviewVideo.addEventListener("timeupdate", () => reportPlayback(false));
    reviewVideo.addEventListener("ended", () => reportPlayback(true));
  }

  inspector.append(workspaceHeading(
    "返工闭环",
    selected?.readOnly ? "历史版本不可发起返工" : "按问题类型回到最小安全阶段",
    "保存返工单不会回退流程；只有再次确认执行时才会失效对应阶段。",
  ));
  const existing = (detail.reworkRequests || []).filter((item) => item.sourceCandidateId === selected?.candidateId);
  const reworkList = element("div", "rework-list");
  existing.forEach((request) => {
    const card = element("article", "rework-card");
    card.append(
      element("b", null, `${request.category} · ${request.status}`),
      element("p", null, request.notes || "无说明"),
      element("span", null, `回到 ${request.targetStage}`),
    );
    if (!view.readOnly && !selected?.readOnly && request.status === "pending_confirmation") {
      const execute = button("确认返工并回到对应阶段", "btn danger");
      execute.addEventListener("click", async () => {
        const confirmCost = request.costStatus !== "confirmation_required"
          || window.confirm("该返工可能产生外部调用费用，确认继续吗？");
        if (!confirmCost) return;
        setButtonBusy(execute, true, "正在回退…");
        try {
          await taskPost(`/review/rework/${encodeURIComponent(request.reworkId)}/execute`, {
            idempotencyKey: `execute-${request.reworkId}`,
            confirmCost: request.costStatus === "confirmation_required" && confirmCost,
          });
        } catch (error) { setButtonBusy(execute, false); showActionError(error); }
      });
      card.append(execute);
    }
    reworkList.append(card);
  });
  inspector.append(reworkList);
  if (!view.readOnly && selected?.isCurrent) {
    const openRework = button("记录分类返工", "btn");
    openRework.disabled = !selected?.candidateId;
    openRework.addEventListener("click", () => openReviewReworkDialog(detail, selected, reviewVideo));
    inspector.append(openRework);
  }
}

function openReviewReworkDialog(detail, candidate, reviewVideo) {
  const taskId = detail.task.task_id;
  const overlay = element("div", "impact-modal rework-modal");
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "rework-title");
  const dialog = element("form", "impact-dialog rework-dialog");
  const title = element("h2", null, `返工 ${candidate.versionLabel}`);
  title.id = "rework-title";
  const categoryLabel = element("label", "rework-field");
  categoryLabel.append(element("span", null, "问题类型"));
  const category = document.createElement("select");
  category.className = "field";
  category.name = "category";
  [["subtitle", "字幕"], ["audio", "声音"], ["packaging", "包装"], ["rhythm", "节奏"], ["material", "素材"], ["script", "文案"], ["technical", "技术问题"]]
    .forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      category.append(option);
    });
  categoryLabel.append(category);
  const notesLabel = element("label", "rework-field");
  notesLabel.append(element("span", null, "问题说明"));
  const notes = document.createElement("textarea");
  notes.className = "field";
  notes.name = "notes";
  notes.required = true;
  notes.placeholder = "说明需要修改的内容";
  notesLabel.append(notes);
  const positionLabel = element("label", "rework-field");
  positionLabel.append(element("span", null, "视频位置（秒）"));
  const position = document.createElement("input");
  position.className = "field";
  position.type = "number";
  position.min = "0";
  position.step = "0.1";
  position.value = Number.isFinite(reviewVideo?.currentTime) ? reviewVideo.currentTime.toFixed(1) : "";
  positionLabel.append(position);
  const impact = element("div", "rework-impact", "正在预览影响范围…");
  impact.setAttribute("role", "status");
  impact.setAttribute("aria-live", "polite");
  const costLabel = element("label", "rework-cost");
  const costConfirm = document.createElement("input");
  costConfirm.type = "checkbox";
  costLabel.append(costConfirm, element("span", null, "我确认后续步骤可能产生外部调用费用"));
  costLabel.hidden = true;
  const controls = element("div", "row-actions");
  const close = button("取消", "btn");
  const save = button("保存返工单", "btn primary");
  const execute = button("确认返工并回到对应阶段", "btn danger");
  execute.disabled = true;
  controls.append(close, save, execute);
  dialog.append(title, categoryLabel, notesLabel, positionLabel, impact, costLabel, controls);
  overlay.append(dialog);
  document.body.append(overlay);
  notes.focus();

  let preview = null;
  let saved = null;
  const saveIdempotencyKey = `reject-${candidate.candidateId}-${Date.now()}`;
  const refreshImpact = async () => {
    preview = null;
    impact.textContent = "正在预览影响范围…";
    try {
      preview = await api(
        "/api/tasks/" + encodeURIComponent(taskId) + "/review/rework/preview",
        jsonRequest("POST", { category: category.value }),
      );
      impact.replaceChildren(
        element("b", null, `回到 ${preview.targetStage}`),
        element("span", null, `保留：${(preview.preservedStages || []).join("、") || "无"}`),
        element("span", null, `失效：${(preview.invalidatedStages || []).join("、") || "无"}`),
      );
      costLabel.hidden = !preview.costConfirmationRequired;
    } catch (error) { impact.textContent = "影响预览失败，请重试。"; showActionError(error); }
  };
  category.addEventListener("change", refreshImpact);
  close.addEventListener("click", () => overlay.remove());
  save.addEventListener("click", async () => {
    let draft;
    try {
      if (!preview) await refreshImpact();
      draft = buildReworkDraft(candidate, {
        category: category.value,
        notes: notes.value,
        positionSeconds: position.value,
      });
    } catch (error) { showActionError(error); notes.focus(); return; }
    setButtonBusy(save, true, "正在保存…");
    try {
      saved = await api(
        "/api/tasks/" + encodeURIComponent(taskId) + "/review/rework",
        jsonRequest("POST", {
          ...draft,
          reviewedBy: "本机同事",
          idempotencyKey: saveIdempotencyKey,
        }),
      );
      state.pendingReviewReworkId = saved.rework.reworkId;
      category.disabled = true;
      notes.disabled = true;
      position.disabled = true;
      save.disabled = true;
      execute.disabled = false;
      showToast("返工单已保存，流程尚未回退。", { kind: "success" });
    } catch (error) { showActionError(error); }
    finally { setButtonBusy(save, false); }
  });
  execute.addEventListener("click", async () => {
    if (!saved?.rework?.reworkId) return;
    const savedNeedsCost = saved.rework.costStatus === "confirmation_required"
      || saved.rework.impact?.costConfirmationRequired;
    if (savedNeedsCost && !costConfirm.checked) {
      showErrorToast("请先确认可能产生的外部调用费用。");
      costConfirm.focus();
      return;
    }
    setButtonBusy(execute, true, "正在回退…");
    try {
      await taskPost(`/review/rework/${encodeURIComponent(saved.rework.reworkId)}/execute`, {
        idempotencyKey: `execute-${saved.rework.reworkId}`,
        confirmCost: Boolean(savedNeedsCost),
      });
      overlay.remove();
    } catch (error) { setButtonBusy(execute, false); showActionError(error); }
  });
  refreshImpact();
}

function renderPublish(detail, primary, inspector, view) {
  const currentCandidate = detail.currentCandidate || null;
  const currentCandidateId = currentCandidate?.candidateId || null;
  primary.append(workspaceHeading(
    "发布包",
    view.readOnly
      ? "查看已经生成的发布包"
      : detail.publishPackages?.length ? "发布包已生成" : "填写平台信息并生成发布包",
    "发布包固定引用已经完整审片通过的版本，不会偷偷换成其他候选。",
  ));
  const canPublish = Boolean(currentCandidateId && detail.workflow.gates?.human_reviewed);
  if (!view.readOnly && canPublish) {
    const form = element("form", "publish-form");
    form.setAttribute("aria-label", "生成发布包");
    const fields = element("div", "publish-fields");
    const labeledField = (label, control, className = "") => {
      const wrapper = element("label", "publish-field " + className);
      wrapper.append(element("span", "publish-field-label", label), control);
      return wrapper;
    };
    const platform = document.createElement("select");
    platform.className = "field";
    platform.name = "platform";
    platform.setAttribute("aria-label", "发布平台");
    ["视频号", "抖音", "快手", "其他"].forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      platform.append(option);
    });
    const titleInput = document.createElement("input");
    titleInput.className = "field";
    titleInput.name = "title";
    titleInput.placeholder = "输入发布标题";
    const description = document.createElement("textarea");
    description.className = "field";
    description.name = "description";
    description.placeholder = "输入发布说明";
    const tags = document.createElement("input");
    tags.className = "field";
    tags.name = "tags";
    tags.placeholder = "招商，产业，园区";
    const tagField = labeledField("发布标签", tags, "wide");
    tagField.append(element("small", "field-help", "使用逗号或中文逗号分隔，重复标签会自动合并。"));
    fields.append(
      labeledField("发布平台", platform),
      labeledField("发布标题", titleInput),
      labeledField("发布说明", description, "wide"),
      tagField,
    );

    const binding = element("section", "publish-binding");
    binding.append(
      element("span", "section-kicker", "本次绑定候选"),
      element("b", null, displayCandidateLabel(currentCandidate.versionLabel || currentCandidateId)),
      element("span", "package-candidate",
        `自动质检：${currentCandidate.qa?.status === "passed" ? "已通过" : "状态待确认"} · 已完成完整审片`),
    );

    const coverFieldset = element("fieldset", "publish-choice-section");
    coverFieldset.append(element("legend", null, "封面选择"));
    const coverGrid = element("div", "publish-choice-grid cover-choice-grid");
    const coverCandidates = buildCoverCandidates(currentCandidate, "candidate-1");
    const initialCover = coverCandidates.length ? "candidate-1" : "pending";
    coverCandidates.forEach((item) => {
      const card = element("label", "publish-choice-card cover-choice-card");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "coverChoice";
      radio.value = item.choice;
      radio.checked = item.choice === initialCover;
      const preview = document.createElement("video");
      preview.className = "cover-preview";
      preview.src = item.mediaUrl;
      preview.muted = true;
      preview.preload = "metadata";
      preview.playsInline = true;
      preview.setAttribute("aria-hidden", "true");
      preview.addEventListener("loadedmetadata", () => {
        preview.currentTime = Math.min(item.time, Math.max(0, preview.duration - 0.05));
      }, { once: true });
      card.append(
        radio,
        preview,
        element("b", null, item.label),
        element("span", null, `${item.time.toFixed(1)} 秒画面`),
      );
      coverGrid.append(card);
    });
    if (!coverCandidates.length) {
      coverGrid.append(inlineFeedback(
        "候选画面暂不可提取",
        "当前候选缺少可靠时长信息，可选择外部封面或待补封面。",
        "partial",
      ));
    }

    const externalCard = element("label", "publish-choice-card cover-choice-card external-cover-card");
    const externalRadio = document.createElement("input");
    externalRadio.type = "radio";
    externalRadio.name = "coverChoice";
    externalRadio.value = "external";
    const externalTitle = element("b", null, "外部封面");
    const externalHint = element("span", "external-cover-name", "尚未选择图片");
    externalCard.append(externalRadio, externalTitle, externalHint);
    const externalPath = document.createElement("input");
    externalPath.type = "hidden";
    externalPath.name = "externalCoverPath";
    const pickExternal = button("选择外部封面图片", "btn compact");
    pickExternal.type = "button";
    pickExternal.addEventListener("click", async () => {
      externalRadio.checked = true;
      setButtonBusy(pickExternal, true, "正在选择…");
      try {
        const selected = await api("/api/picker/image", jsonRequest("POST"));
        if (selected.path) {
          externalPath.value = selected.path;
          externalHint.textContent = selected.path.split(/[\\/]/u).pop() || "已选择外部封面";
        }
      } catch (error) { showActionError(error); }
      finally { setButtonBusy(pickExternal, false); }
    });
    externalCard.append(pickExternal, externalPath);
    coverGrid.append(externalCard);

    const pendingCard = element("label", "publish-choice-card cover-choice-card");
    const pendingRadio = document.createElement("input");
    pendingRadio.type = "radio";
    pendingRadio.name = "coverChoice";
    pendingRadio.value = "pending";
    pendingRadio.checked = initialCover === "pending";
    pendingCard.append(
      pendingRadio,
      element("b", null, "待补封面"),
      element("span", null, "先生成交付包，并明确标记封面待补。"),
    );
    coverGrid.append(pendingCard);
    coverFieldset.append(coverGrid);

    const deliveryFieldset = element("fieldset", "publish-choice-section");
    deliveryFieldset.append(element("legend", null, "交付类型"));
    const deliveryGrid = element("div", "publish-choice-grid delivery-choice-grid");
    [
      ["standard", "标准发布包", "成片、封面、发布文案、检查单与 QA 报告。"],
      ["editable", "可编辑交付", "在标准包外加入可用的冻结组件；不存在的分轨会标记为不可用。"],
    ].forEach(([value, label, help], index) => {
      const card = element("label", "publish-choice-card delivery-choice-card");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "deliveryType";
      radio.value = value;
      radio.checked = index === 0;
      card.append(radio, element("b", null, label), element("span", null, help));
      deliveryGrid.append(card);
    });
    deliveryFieldset.append(deliveryGrid);

    const submit = button("生成完整发布包", "btn primary");
    submit.type = "submit";
    const submitStatus = element("div", "publish-submit-status");
    submitStatus.setAttribute("role", "status");
    submitStatus.setAttribute("aria-live", "polite");
    submitStatus.setAttribute("aria-atomic", "true");
    form.append(binding, fields, coverFieldset, deliveryFieldset, submit, submitStatus);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      let values;
      try {
        values = buildPublishFormValue(form);
      } catch (error) {
        submitStatus.textContent = error.message === "EXTERNAL_COVER_REQUIRED"
          ? "请选择一张外部封面图片，或改选其他封面方式。"
          : "发布设置不完整，请检查后重试。";
        (error.message === "EXTERNAL_COVER_REQUIRED" ? pickExternal : platform).focus();
        return;
      }
      setButtonBusy(submit, true, "正在生成…");
      form.setAttribute("aria-busy", "true");
      submitStatus.textContent = "正在生成发布包，请稍候。";
      try {
        await taskPost("/publish", { ...values, candidateId: currentCandidateId });
        submitStatus.textContent = "发布包已生成。";
      } catch (error) {
        submitStatus.textContent = "生成失败，请根据提示检查后重试。";
        showActionError(error);
      } finally {
        form.removeAttribute("aria-busy");
        setButtonBusy(submit, false);
      }
    });
    primary.append(form);
  } else if (!view.readOnly) {
    primary.append(inlineFeedback(
      "发布门禁尚未满足",
      currentCandidateId ? "请先完整观看并批准当前候选。" : "请先生成并通过自动 QA 的候选成片。",
      "partial",
    ));
  }
  const packages = element("div", "package-list");
  (detail.publishPackages || []).forEach((item, index) => {
    const view = buildPublishPackageView(item, currentCandidateId);
    const card = element("article", "package-card");
    const header = element("div", "package-card-head");
    header.append(
      element("b", null, "发布包 " + String(index + 1)),
      element("span", view.current && view.complete ? "status complete" : "status blocked", view.status),
    );
    const files = element("div", "package-files");
    files.append(...view.files.map((name) => {
      const file = element("span", null, String(name).split(/[\\/]/u).pop());
      file.title = String(name);
      return file;
    }));
    const deliveryLabel = view.deliveryType === "editable" ? "可编辑交付" : "标准发布包";
    const coverLabels = {
      "candidate-1": "候选 1",
      "candidate-2": "候选 2",
      "candidate-3": "候选 3",
      external: "外部封面",
      pending: "待补封面",
    };
    const technical = element("details", "package-technical");
    technical.append(
      element("summary", null, `查看 ${view.files.length} 个交付文件与保存位置`),
      element("code", "package-directory", view.directory || "目录未返回"),
      element("small", null, `发布记录：${view.id || "未返回"}`),
      files,
    );
    card.append(
      header,
      element("span", "package-candidate", displayCandidateLabel(view.candidateLabel)),
      element("span", "package-candidate", `${deliveryLabel} · 封面：${coverLabels[view.coverChoice] || view.coverChoice}`),
      technical,
    );
    packages.append(card);
  });
  primary.append(packages);
  inspector.append(
    workspaceHeading("发布前提", "当前版本已通过全部门禁"),
    element("p", null, gateSummary(detail.workflow.gates)),
    renderAiDisclosure("rewrite_brief"),
  );
}

function renderRecovery(detail, primary, inspector, recovery) {
  primary.append(workspaceHeading(
    "后台任务",
    recovery.title,
    recovery.description,
  ));
  const status = element("section", "recovery-card");
  status.setAttribute("aria-live", recovery.ariaLive || "polite");
  status.setAttribute("aria-atomic", "true");
  status.append(
    element("span", "status-chip " + taskStatusClass(recovery.runStatus),
      RUN_STATUS_LABELS[recovery.runStatus] || "状态待确认"),
    element("h3", null, recovery.phase),
    element("p", null, "处理进度：" + recovery.progressLabel),
  );
  if (recovery.queueLabel) status.append(element("p", null, recovery.queueLabel));
  if (recovery.statusDetail) status.append(element("p", "recovery-status-detail", recovery.statusDetail));
  if (recovery.runStatus === "queued" && recovery.startLabel) {
    status.append(element("p", null, recovery.startLabel));
  }
  if (recovery.remainingLabel) status.append(element("p", null, recovery.remainingLabel));
  if (recovery.observedLabel) status.append(element("p", "recovery-observed", recovery.observedLabel));
  if (recovery.problem) {
    status.append(element("p", "recovery-warning", "失败原因：" + recovery.problem));
  }
  if (recovery.needsManualCheck) {
    status.append(element("p", "recovery-warning",
      "当前结果必须人工核对，系统不会自行重试付费或生成式步骤。"));
  }
  const actions = element("div", "row-actions");
  if (recovery.mode === "offline-source" && recovery.libraryId) {
    const reconnect = button("重新连接素材目录", "btn primary");
    reconnect.addEventListener("click", async () => {
      try {
        await reconnectLibrary(recovery.libraryId, state.selectedTaskId);
      } catch (error) { showActionError(error); }
    });
    actions.append(reconnect);
  }
  if (recovery.canResume && recovery.jobId) {
    const resume = button("从安全检查点继续", "btn primary");
    resume.addEventListener("click", async () => {
      try { await runJobControl(resume, recovery.jobId, "resume"); } catch (error) { showActionError(error); }
    });
    actions.append(resume);
  }
  if (recovery.canPause && recovery.jobId) {
    const pause = button("安全暂停", "btn primary");
    pause.addEventListener("click", async () => {
      try { await runJobControl(pause, recovery.jobId, "pause"); } catch (error) { showActionError(error); }
    });
    actions.append(pause);
  }
  if (recovery.canCancel && recovery.jobId) {
    const cancel = button("取消本次后台处理");
    cancel.addEventListener("click", async () => {
      if (!window.confirm("确认取消本次后台处理吗？已完成的阶段记录会保留。")) return;
      try {
        await runJobControl(cancel, recovery.jobId, "cancel", { reason: "使用人在工作台取消" });
      } catch (error) { showActionError(error); }
    });
    actions.append(cancel);
  }
  const inspectStage = button("查看当前阶段");
  inspectStage.addEventListener("click", () => {
    state.workspaceOverride = workspaceKind(detail.workflow.current_stage);
    renderTaskWorkspace(detail);
  });
  actions.append(inspectStage);
  status.append(actions);
  primary.append(status);

  inspector.append(
    workspaceHeading(
      "恢复原则",
      recovery.mode === "offline-source"
        ? "重连原库，不换素材"
        : recovery.needsManualCheck ? "先核对，再决定" : "从账本状态继续",
      "恢复和取消权限都由本机服务决定，页面不会假定任务已经成功。",
    ),
    element("p", null, recovery.mode === "offline-source"
      ? "选择这个素材库移动后的新位置；素材库 ID 和已经确认的镜头引用保持不变，随后重新扫描核对。"
      : recovery.needsManualCheck
        ? "请先核对模型供应商后台或负责人记录，确认没有已产生但未下载的结果。"
        : "关闭页面不会取消任务；重新打开后仍读取同一 SQLite 任务账本。"),
  );
}

function renderTaskWorkspace(detail) {
  state.taskDetail = detail;
  const view = buildWorkflowViewContext(detail, state.viewingStageKey);
  state.viewingStageKey = view.readOnly ? view.viewingStageKey : null;
  renderStageSummary(detail);
  const primary = root.querySelector("#workspace-primary");
  const inspector = root.querySelector("#workspace-inspector");
  primary.replaceChildren();
  inspector.replaceChildren();
  if (view.readOnly) primary.append(renderHistoryNotice(detail, view));
  const recovery = view.readOnly ? null : buildRecoveryWorkspace(detail);
  const kind = view.readOnly
    ? view.workspace
    : state.workspaceOverride || (recovery ? "recovery" : view.workspace);
  const workspace = root.querySelector("#stage-workspace");
  if (workspace) workspace.dataset.kind = kind;
  inspector.hidden = false;
  if (kind === "recovery") renderRecovery(detail, primary, inspector, recovery);
  else if (kind === "brief-storyboard") renderBriefStoryboard(detail, primary, inspector, view);
  else if (kind === "rough-cut") renderRoughCut(detail, primary, inspector, view);
  else if (kind === "packaging") renderPackaging(detail, primary, inspector, view);
  else if (kind === "review") renderReview(detail, primary, inspector, view);
  else if (kind === "publish") renderPublish(detail, primary, inspector, view);
  else {
    primary.append(element("div", "empty-state", "当前阶段状态已保存，请返回任务中心查看。"));
  }
  if (["brief-storyboard", "publish"].includes(kind) && inspector.childElementCount) {
    const extra = element("details", "workspace-extra");
    const label = element("summary", null, kind === "publish" ? "查看发布前提" : "查看系统处理说明");
    extra.append(label);
    while (inspector.firstChild) extra.append(inspector.firstChild);
    primary.append(extra);
    inspector.hidden = true;
  }
}

async function openTask(taskId) {
  try {
    const detail = await api("/api/tasks/" + encodeURIComponent(taskId));
    clearPackagingPreviewsForTask(taskId);
    const task = detail.task;
    const workflow = detail.workflow;
    if (state.selectedTaskId !== taskId) {
      stopRoughPlayback();
      state.workspaceOverride = null;
      state.viewingStageKey = null;
      state.selectedSceneId = null;
      state.previewCandidateId = null;
      state.selectedReviewCandidateId = null;
      state.pendingReviewReworkId = null;
      state.roughPlayhead = 0;
    }
    state.selectedTaskId = taskId;
    state.taskDetail = detail;
    root.querySelector("#task-title").textContent = task.title || "未命名任务";
    root.querySelector("#task-route").textContent = ROUTE_LABELS[task.task_type] || "暂未识别路线";
    const orchestrationMode = task.orchestration_mode === "agent" ? "agent" : "standalone";
    const orchestrationBadge = root.querySelector("#task-orchestration-mode");
    orchestrationBadge.textContent = orchestrationMode === "agent"
      ? "当前 Agent 规划"
      : "CUTROOM 自己规划";
    orchestrationBadge.classList.toggle("agent", orchestrationMode === "agent");
    const frozenModels = (detail.modelBindings || [])
      .map((item) => item.displayName + "（" + item.model + "）")
      .join("、");
    const activeStep = buildStagePresentation(workflow);
    root.querySelector("#task-summary").textContent = activeStep
      ? `当前第 ${activeStep.number} 步 · ${activeStep.label}。按本步提示完成后进入下一步。`
      : "任务已完成，可查看各步结果。";
    root.querySelector("#task-models").textContent = frozenModels
      ? "本任务已冻结：" + frozenModels
      : "本任务暂无冻结的 AI 服务配置。";
    root.querySelector("#task-run-status").textContent =
      RUN_STATUS_LABELS[workflow.run_status] || "状态待确认";
    renderTaskWorkflow(detail);
    renderTaskWorkspace(detail);
    renderQuickTasks(buildTaskCenter(state.bootstrap.tasks));
    showPage("task-detail", {
      crumb: (task.title || "任务") + " / " + workflowLabel(workflow.current_stage),
      context: "tasks",
    });
  } catch (error) {
    window.location.hash = "#/tasks";
    showToast(error.message + (error.diagnosticId ? "（诊断编号 " + error.diagnosticId + "）" : ""));
  }
}

function renderCreateOptions() {
  const routeContainer = root.querySelector("#route-options");
  routeContainer.replaceChildren(...ROUTE_OPTIONS.map((route) => {
    const button = element("button", "route" + (route.available ? "" : " unavailable"));
    button.type = "button";
    button.dataset.route = route.key;
    button.setAttribute("aria-pressed", String(route.key === "digital_human"));
    button.disabled = !route.available;
    button.append(
      element("span", "route-icon", route.key === "digital_human" ? "人" : route.key === "park_video" ? "园" : "广"),
      element("h3", null, route.label),
      element("p", null, route.description),
    );
    if (!route.available) button.append(element("em", null, "后续开放"));
    return button;
  }));
  const subtype = root.querySelector("#create-subtype");
  subtype.replaceChildren(...SUBTYPE_OPTIONS.map((item) => {
    const option = document.createElement("option");
    option.value = item.key;
    option.textContent = item.label;
    return option;
  }));
  renderCreateLibraries();
  renderCreateModelProfiles();
  restoreCreateDraft();
}

function renderCreateModelProfiles() {
  const profiles = state.bootstrap.modelProfiles || [];
  for (const [id, capability, emptyLabel] of [
    ["#planning-profile", "planning", "请先在设置中添加并检测文案策划模型"],
    ["#tts-profile", "tts", "请先在设置中添加并测试配音模型"],
    ["#broll-profile", "broll", "不启用付费 B-roll 画面生成"],
  ]) {
    const select = root.querySelector(id);
    if (!select) continue;
    const current = select.value;
    const available = selectableProfiles(profiles, capability);
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = available.length ? "请选择模型配置" : emptyLabel;
    select.replaceChildren(placeholder, ...available.map((profile) => {
      const option = document.createElement("option");
      option.value = profile.profileId;
      option.textContent = profile.displayName + " · " + profile.model
        + (profile.isDefault ? "（默认）" : "");
      return option;
    }));
    const preferred = available.find((profile) => profile.profileId === current)
      || (capability === "broll" ? null : (
        available.find((profile) => profile.isDefault) || available[0]
      ));
    if (preferred) select.value = preferred.profileId;
  }
  renderCreateVoiceOptions();
}

function fallbackProfileVoices(profile) {
  const voiceId = String(
    profile?.settings?.voice || profile?.settings?.default_voice_id || "",
  ).trim();
  return {
    profileId: profile?.profileId || "",
    resourceId: profile?.model || "",
    defaultVoiceId: voiceId,
    voices: voiceId ? [{
      voiceId,
      displayName: String(profile?.settings?.voice_name || "已配置音色"),
      selectable: true,
    }] : [],
    state: { status: voiceId ? "success" : "never" },
  };
}

async function loadProfileVoices(profileId) {
  if (!profileId || state.profileVoices[profileId] || state.loadingProfileVoices.has(profileId)) {
    return;
  }
  const profile = (state.bootstrap.modelProfiles || []).find(
    (item) => item.profileId === profileId,
  );
  if (!profile) return;
  if (profile.provider !== "doubao_tts") {
    state.profileVoices[profileId] = fallbackProfileVoices(profile);
    renderCreateVoiceOptions();
    return;
  }
  state.loadingProfileVoices.add(profileId);
  try {
    state.profileVoices[profileId] = await api(
      "/api/settings/model-profiles/" + encodeURIComponent(profileId) + "/voices",
    );
  } catch (error) {
    state.profileVoices[profileId] = { voices: [], error: error.message, state: { status: "failed" } };
  } finally {
    state.loadingProfileVoices.delete(profileId);
    renderCreateVoiceOptions();
    if ((window.location.hash || "") === "#/settings") renderSettings();
  }
}

function renderCreateVoiceOptions() {
  const profileSelect = root.querySelector("#tts-profile");
  const voiceSelect = root.querySelector("#tts-voice");
  const detail = root.querySelector("#tts-voice-detail");
  const testButton = root.querySelector("[data-test-task-voice]");
  if (!profileSelect || !voiceSelect) return;
  const profileId = profileSelect.value;
  const payload = state.profileVoices[profileId];
  const current = voiceSelect.value || state.pendingCreateVoiceId;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  if (!profileId) {
    placeholder.textContent = "请先选择配音服务";
  } else if (!payload) {
    placeholder.textContent = "正在读取可用音色…";
  } else if (!payload.voices?.length) {
    placeholder.textContent = "当前没有可选音色";
  } else {
    placeholder.textContent = "请选择本条视频音色";
  }
  const voices = payload?.voices || [];
  voiceSelect.replaceChildren(placeholder, ...voices.map((voice) => {
    const presentation = voiceOptionPresentation(voice);
    const option = document.createElement("option");
    option.value = voice.voiceId;
    option.textContent = presentation.label + (presentation.detail ? " · " + presentation.detail : "");
    return option;
  }));
  const selected = voices.find((voice) => voice.voiceId === current)
    || voices.find((voice) => voice.voiceId === payload?.defaultVoiceId);
  voiceSelect.value = selected?.voiceId || "";
  if (selected?.voiceId === state.pendingCreateVoiceId) state.pendingCreateVoiceId = "";
  const presentation = selected ? voiceOptionPresentation(selected) : null;
  detail.textContent = payload?.error
    || (presentation
      ? presentation.label + (presentation.detail ? " · " + presentation.detail : "")
      : profileId ? "请选择本条视频使用的声音；不会静默改成其他音色。" : "先选择配音服务，再选择本条视频使用的声音。");
  testButton.disabled = !profileId || !voiceSelect.value || state.testingVoices.has("task");
  if (profileId && !payload) void loadProfileVoices(profileId);
}

async function testSelectedTaskVoice() {
  const profileId = root.querySelector("#tts-profile").value;
  const voiceId = root.querySelector("#tts-voice").value;
  if (!profileId || !voiceId) {
    showToast("请先选择本条视频音色。");
    return;
  }
  state.testingVoices.add("task");
  renderCreateVoiceOptions();
  try {
    const result = await api(
      "/api/settings/model-profiles/" + encodeURIComponent(profileId) + "/voice-test",
      jsonRequest("POST", { voiceId }),
    );
    const audio = root.querySelector("#tts-voice-preview");
    audio.src = result.previewUrl;
    audio.hidden = false;
    await audio.play().catch(() => {});
    showToast("试听已生成并开始播放。");
  } catch (error) {
    showErrorToast("试听失败：" + error.message);
  } finally {
    state.testingVoices.delete("task");
    renderCreateVoiceOptions();
  }
}

function renderCreateLibraries() {
  const groups = groupShotLibraries(state.bootstrap.libraries || []);
  const primary = root.querySelector("#primary-library");
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = groups.primary.length ? "请选择本次园区/项目素材目录" : "请先到镜头素材库登记主素材目录";
  primary.replaceChildren(placeholder, ...groups.primary.map((library) => {
    const option = document.createElement("option");
    option.value = library.libraryId;
    option.textContent = library.name + (library.online ? "" : "（离线）");
    return option;
  }));
  const generic = root.querySelector("#generic-library-options");
  if (!groups.generic.length) {
    generic.replaceChildren(element("span", "muted-copy", "尚未登记通用补充目录"));
    return;
  }
  generic.replaceChildren(...groups.generic.map((library) => {
    const label = element("label", "inline-check");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "selected_generic_ids";
    input.value = library.libraryId;
    label.append(input, document.createTextNode(" " + library.name + (library.isDefaultGeneric ? " · 默认" : "")));
    return label;
  }));
}

function createFormValues() {
  const form = root.querySelector("#create-form");
  const data = new FormData(form);
  return {
    route: routeContainerValue(),
    orchestration_mode: data.get("orchestration_mode"),
    subtype: data.get("subtype"),
    title: data.get("title"),
    digital_human_mp4: data.get("digital_human_mp4"),
    final_script: data.get("final_script"),
    primary_library_id: data.get("primary_library_id"),
    selected_generic_ids: data.getAll("selected_generic_ids"),
    include_default_generic: data.has("include_default_generic"),
    purpose: data.get("purpose"),
    target_duration_seconds: data.get("target_duration_seconds"),
    aspect_ratio: data.get("aspect_ratio"),
    voice_mode: data.get("voice_mode"),
    planning_profile_id: data.get("planning_profile_id"),
    tts_profile_id: data.get("tts_profile_id"),
    tts_voice_id: data.get("tts_voice_id"),
    broll_profile_id: data.get("broll_profile_id"),
  };
}

function createDraftValues() {
  const values = createFormValues();
  return {
    ...values,
    target_duration_seconds: root.querySelector(
      "#create-form [name='target_duration_seconds']",
    ).value,
  };
}

function saveCreateDraft() {
  taskDrafts.save(createDraftValues());
}

function updateCreateRouteFields(route = routeContainerValue()) {
  const ad = route === "ad_mix";
  root.querySelectorAll("[data-route-digital]").forEach((field) => {
    field.hidden = ad;
  });
  root.querySelectorAll("[data-route-ad]").forEach((field) => {
    field.hidden = !ad;
  });
  const ttsProfile = root.querySelector("#tts-profile");
  const ttsVoice = root.querySelector("#tts-voice");
  ttsProfile.disabled = !ad;
  ttsProfile.required = ad;
  ttsVoice.disabled = !ad;
  ttsVoice.required = ad;
  const title = root.querySelector("[data-script-title]");
  const script = root.querySelector("[name='final_script']");
  if (title) {
    title.textContent = ad
      ? "5 · 本次广告最终文案"
      : "4 · 禅镜实际使用的最终口播文案";
  }
  if (script) {
    script.placeholder = ad
      ? "粘贴本次广告最终文案，系统将按原文拆句、配音和逐句配片"
      : "粘贴与数字人口播完全一致的最终文案";
  }
  if (ad) renderCreateVoiceOptions();
  updateCreateModeFields();
}

function updateCreateModeFields() {
  const agentMode = root.querySelector("#orchestration-mode")?.value === "agent";
  root.querySelectorAll("[data-planning-profile]").forEach((field) => {
    field.hidden = agentMode;
  });
  const planningProfile = root.querySelector("#planning-profile");
  if (planningProfile) planningProfile.disabled = agentMode;
}

function restoreCreateDraft() {
  const draft = taskDrafts.read();
  if (!draft) {
    updateCreateRouteFields();
    return;
  }
  root.querySelectorAll(".route").forEach((item) => {
    item.setAttribute("aria-pressed", String(item.dataset.route === draft.route));
  });
  const form = root.querySelector("#create-form");
  for (const name of [
    "subtype", "title", "orchestration_mode", "digital_human_mp4", "final_script",
    "purpose", "target_duration_seconds", "aspect_ratio", "voice_mode",
    "planning_profile_id", "tts_profile_id", "broll_profile_id",
    "tts_voice_id",
  ]) {
    const field = form.elements.namedItem(name);
    if (field) field.value = draft[name];
  }
  state.pendingCreateVoiceId = draft.tts_voice_id || "";
  const primary = form.elements.namedItem("primary_library_id");
  if (primary && [...primary.options].some((option) => option.value === draft.primary_library_id)) {
    primary.value = draft.primary_library_id;
  }
  form.elements.namedItem("include_default_generic").checked = draft.include_default_generic;
  const selectedGeneric = new Set(draft.selected_generic_ids);
  form.querySelectorAll("[name='selected_generic_ids']").forEach((input) => {
    input.checked = selectedGeneric.has(input.value);
  });
  updateCreateRouteFields(draft.route);
  renderCreateVoiceOptions();
}

function clearCreateDraft() {
  taskDrafts.clear();
  state.createRequest = null;
  state.preflight = null;
  const form = root.querySelector("#create-form");
  form.reset();
  root.querySelector("#preflight-panel").hidden = true;
  form.hidden = false;
  renderCreateOptions();
  setCreateStep("route");
}

function routeContainerValue() {
  return root.querySelector(".route[aria-pressed='true']")?.dataset.route || "";
}

function renderPreflight(result) {
  setCreateStep("preflight");
  const container = root.querySelector("#preflight-results");
  const items = preflightSummary(result);
  container.replaceChildren(...items.map((item) => {
    const row = element("div", "check " + item.level);
    row.append(
      element("span", "check-icon", item.level === "passed" ? "✓" : item.level === "warning" ? "!" : "×"),
      element("b", null, item.message),
      element("em", null, item.level === "passed" ? "通过" : item.level === "warning" ? "提醒" : "需处理"),
    );
    return row;
  }));
  if (!items.length) {
    container.append(element("div", "check warning", "服务未返回逐项检查结果"));
  }
  const view = buildPreflightView(result);
  const meta = root.querySelector("#preflight-meta");
  meta.replaceChildren(
    metricBlock("阻断项", String((result.blocking || []).length)),
    metricBlock("提醒", String((result.warnings || []).length)),
    metricBlock("机器耗时", view.metrics.machineTime.value, view.metrics.machineTime.detail),
    metricBlock("人工操作", view.metrics.humanTime.value, view.metrics.humanTime.detail),
    metricBlock("新增空间", view.metrics.disk.value, view.metrics.disk.detail),
    metricBlock("API 费用", view.metrics.apiCost.value, view.metrics.apiCost.detail),
    metricBlock("重任务队列", view.metrics.queue.value, view.metrics.queue.detail),
    metricBlock("预计等待", view.metrics.queueWait.value, view.metrics.queueWait.detail),
  );
  const costScope = root.querySelector("#preflight-cost-scope");
  costScope.replaceChildren(
    element("h3", null, "费用范围"),
    element("p", null, view.costScope.included.length
      ? "已包含：" + view.costScope.included.join("、")
      : "本次没有已确认计入的远程费用。"),
    element("p", null, view.costScope.excluded.length
      ? "不包含：" + view.costScope.excluded.join("、")
      : "没有另行排除的费用项目。"),
  );
  const confirmations = root.querySelector("#preflight-confirmations");
  confirmations.replaceChildren(element("h3", null, "后续需要你的四次确认"));
  const confirmationList = element("ol", "preflight-list");
  confirmationList.append(...view.confirmations.map((item) => {
    const row = element("li");
    row.append(element("b", null, item.label), element("span", null, item.detail));
    return row;
  }));
  confirmations.append(confirmationList);
  const notices = root.querySelector("#preflight-notices");
  notices.replaceChildren(element("h3", null, "真实性、隐私与授权提醒"));
  const noticeList = element("ul", "preflight-list");
  noticeList.append(...view.notices.map((notice) => element("li", null, notice)));
  notices.append(noticeList);
  if (view.observedAt) {
    notices.append(element("p", "preflight-observed", "队列观测时间：" + formatTime(view.observedAt)));
  }
  root.querySelector("[data-create-confirm]").disabled = Boolean((result.blocking || []).length);
  root.querySelector("#create-form").hidden = true;
  root.querySelector("#preflight-panel").hidden = false;
}

function setCreateStep(step) {
  const current = createStepIndex(step);
  root.querySelectorAll(".new-step").forEach((item, index) => {
    item.classList.toggle("current", index === current);
    item.classList.toggle("done", index < current);
  });
}

function setPreflightPending(pending) {
  const button = root.querySelector("[data-run-preflight]");
  button.disabled = pending;
  button.textContent = pending ? "正在检查本机环境…" : "继续：检查环境 →";
  root.querySelector("#create-form").setAttribute("aria-busy", String(pending));
}

function metricBlock(label, value, detail = null) {
  const box = element("div", "metric");
  box.append(element("span", null, label), element("b", null, value));
  if (detail) box.append(element("small", null, detail));
  return box;
}

function publicLibraryName(library) {
  return library.purpose === "generic" ? "通用补充" : "本次主素材";
}

function renderLibraries() {
  const libraries = state.bootstrap.libraries || [];
  const query = root.querySelector("#library-search").value.trim().toLowerCase();
  const visible = libraries.filter((library) =>
    !query || (library.name + " " + library.mountPath).toLowerCase().includes(query)
  );
  const groups = groupShotLibraries(visible);
  const container = root.querySelector("#library-groups");
  const sections = [
    ["本次主素材目录", "每条任务必须明确选择一个园区或项目专属目录。", groups.primary],
    ["通用补充目录", "主目录素材不足时可自动补充，也可在创建任务时指定。", groups.generic],
  ];
  container.replaceChildren(...sections.map(([title, description, items]) => {
    const section = element("section", "library-section");
    const heading = element("div", "section-heading");
    const copy = element("div");
    copy.append(element("h2", null, title), element("p", null, description));
    heading.append(copy, element("span", "count-badge", items.length + " 个目录"));
    section.append(heading);
    const cards = element("div", "cards");
    if (!items.length) cards.append(element("div", "empty-inline", "尚未登记该类目录"));
    for (const library of items) {
      const card = element("article", "card library-card");
      const status = statusBadge(
        library.online ? "在线" : "离线",
        library.online ? "success" : "error",
      );
      status.classList.add("status", library.online ? "online" : "offline");
      const action = element("button", "btn", library.online ? "检查更新" : "重新连接");
      action.type = "button";
      action.addEventListener("click", async () => {
        if (library.online) return checkLibrary(library.libraryId);
        try { await reconnectLibrary(library.libraryId); } catch (error) { showActionError(error); }
      });
      card.append(
        status,
        element("h3", null, library.name),
        element("p", null, publicLibraryName(library) + " · " + library.assetCount.toLocaleString("zh-CN") + " 项素材"),
        element("code", null, library.mountPath),
        element("span", "sync-time", "最后同步：" + formatTime(library.lastSyncAt)),
        action,
      );
      cards.append(card);
    }
    section.append(cards);
    return section;
  }));
}

function renderMaterialLibraryOptions() {
  const select = root.querySelector("#material-library-filter");
  const selected = state.materialFilters.libraryId;
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部镜头目录";
  select.replaceChildren(all, ...(state.bootstrap.libraries || [])
    .filter((library) => library.purpose !== "packaging")
    .map((library) => {
      const option = document.createElement("option");
      option.value = library.libraryId;
      option.textContent = library.name + (library.online ? "" : "（离线）");
      return option;
    }));
  select.value = selected;
}

function materialTitle(item) {
  const parts = String(item.relativePath || item.assetId || "未命名素材").split(/[\\/]/);
  return parts.at(-1) || item.assetId;
}

function renderMaterialIndex() {
  const view = buildMaterialIndexView(state.materialIndex);
  const container = root.querySelector("#material-index-results");
  const count = root.querySelector("#material-index-count");
  count.textContent = state.materialIndexLoading ? "正在读取素材索引…" : view.countLabel;
  container.setAttribute("aria-busy", String(state.materialIndexLoading));
  if (state.materialIndexLoading && !view.items.length) {
    container.replaceChildren(element("div", "empty-inline", "正在读取镜头素材…"));
  } else if (!view.items.length) {
    container.replaceChildren(element("div", "empty-inline", view.emptyMessage));
  } else {
    container.replaceChildren(...view.items.map((item) => {
      const card = element("article", "material-card" + (item.canPreview ? "" : " unavailable"));
      const preview = element("div", "material-preview");
      if (item.canPreview && item.mediaKind === "image") {
        const image = document.createElement("img");
        image.src = item.previewUrl;
        image.alt = materialTitle(item) + " 预览";
        image.loading = "lazy";
        preview.append(image);
      } else if (item.canPreview && item.mediaKind === "video") {
        const video = document.createElement("video");
        video.src = item.previewUrl;
        video.controls = true;
        video.preload = "metadata";
        video.setAttribute("aria-label", materialTitle(item) + " 视频预览");
        preview.append(video);
      } else if (item.canPreview && item.mediaKind === "audio") {
        const audio = document.createElement("audio");
        audio.src = item.previewUrl;
        audio.controls = true;
        audio.preload = "metadata";
        audio.setAttribute("aria-label", materialTitle(item) + " 音频预览");
        preview.append(audio);
      } else {
        const blocked = element("span", "material-preview-unavailable", item.availabilityLabel);
        blocked.setAttribute("aria-label", "当前不可预览：" + item.availabilityLabel);
        preview.append(blocked);
      }
      const heading = element("div", "material-card-heading");
      heading.append(
        element("h3", null, materialTitle(item)),
        statusBadge(item.availabilityLabel, item.availability === "available" ? "success" : "error"),
      );
      const facts = element("div", "material-facts");
      facts.append(
        element("span", null, item.libraryName || "目录未知"),
        element("span", null, formatMaterialDuration(item.durationSeconds)),
        element("span", null, formatMaterialSize(item.sizeBytes)),
        element("span", null, item.fingerprintStatus === "full" ? "完整指纹" : "快速指纹"),
      );
      const authorization = element(
        "p",
        "material-authorization " + (item.authorizationStatus === "approved" ? "approved" : "unknown"),
        item.authorizationLabel,
      );
      card.append(
        preview,
        heading,
        element("code", null, item.relativePath || item.assetId),
        facts,
        authorization,
      );
      return card;
    }));
  }
  const previous = root.querySelector("[data-material-previous]");
  const next = root.querySelector("[data-material-next]");
  previous.disabled = state.materialIndexLoading || !view.hasPrevious;
  next.disabled = state.materialIndexLoading || !view.hasNext;
  root.querySelector("#material-page-label").textContent = view.total
    ? `第 ${Math.floor(view.offset / view.limit) + 1} 页`
    : "暂无结果";
  const selectedTask = (state.bootstrap.tasks || []).find(
    (task) => task.taskId === state.selectedTaskId,
  );
  root.querySelector("#material-index-scope").textContent = selectedTask
    ? `当前按任务“${selectedTask.title}”的冻结素材范围查询。`
    : "当前显示全部已登记镜头素材；进入任务后再打开素材库可按该任务范围查询。";
}

async function loadMaterialIndex({ resetOffset = false } = {}) {
  if (state.materialIndexLoading) return;
  if (resetOffset) state.materialFilters.offset = 0;
  state.materialIndexLoading = true;
  renderMaterialIndex();
  try {
    state.materialIndex = await api(buildMaterialIndexQuery(
      state.materialFilters,
      state.selectedTaskId,
    ));
  } catch (error) {
    state.materialIndex = {
      items: [],
      total: 0,
      limit: state.materialFilters.limit,
      offset: state.materialFilters.offset,
    };
    showActionError(error);
  } finally {
    state.materialIndexLoading = false;
    renderMaterialIndex();
  }
}

function applyMaterialFilters({ delayed = false } = {}) {
  state.materialFilters = {
    ...state.materialFilters,
    query: root.querySelector("#material-search").value,
    libraryId: root.querySelector("#material-library-filter").value,
    mediaKind: root.querySelector("#material-kind-filter").value,
    availability: root.querySelector("#material-availability-filter").value,
    authorization: root.querySelector("#material-authorization-filter").value,
    offset: 0,
  };
  clearTimeout(state.materialSearchTimer);
  if (delayed) {
    state.materialSearchTimer = setTimeout(() => void loadMaterialIndex(), 250);
  } else {
    void loadMaterialIndex();
  }
}

async function checkLibrary(libraryId) {
  try {
    const job = await api("/api/libraries/" + encodeURIComponent(libraryId) + "/check", jsonRequest("POST"));
    showToast("已加入检查队列，当前序号：" + (job.queuePosition || 1));
    await refreshBootstrap();
  } catch (error) {
    showToast(error.message);
  }
}

function schedulePackagingCatalogRefresh() {
  if (state.packagingCatalogCheckPoll) return;
  state.packagingCatalogCheckPoll = setTimeout(async () => {
    state.packagingCatalogCheckPoll = null;
    await loadPackagingCatalogCheck({ force: true });
  }, 2000);
}

async function loadPackagingCatalogCheck({ force = false } = {}) {
  if (state.packagingCatalogCheckLoading) return;
  if (!force && state.packagingCatalogCheck !== null) return;
  state.packagingCatalogCheckLoading = true;
  renderPackagingCatalog();
  try {
    state.packagingCatalogCheck = await api("/api/packaging-catalog/checks/latest");
    if (["queued", "running"].includes(state.packagingCatalogCheck.status)) {
      schedulePackagingCatalogRefresh();
    }
  } catch (error) {
    showActionError(error);
  } finally {
    state.packagingCatalogCheckLoading = false;
    renderPackagingCatalog();
  }
}

async function requestPackagingCatalogCheck(control) {
  setButtonBusy(control, true, "正在提交检查…");
  try {
    state.packagingCatalogCheck = await api(
      "/api/packaging-catalog/check",
      jsonRequest("POST", {}),
    );
    showToast("包装素材检查已加入队列；不会自动改写清单。 ");
    schedulePackagingCatalogRefresh();
    renderPackagingCatalog();
  } catch (error) {
    showActionError(error);
  } finally {
    setButtonBusy(control, false, "正在提交检查…");
  }
}

function renderPackagingCatalogCheck() {
  const section = element("section", "packaging-check-panel");
  const heading = element("div", "packaging-check-head");
  const copy = element("div");
  copy.append(
    element("h2", null, "目录增量检查"),
    element("p", null, "只报告新增、更新、缺失与无效文件；不会自动登记或修改授权状态。"),
  );
  const run = button("检查新增与更新", "btn primary");
  run.dataset.action = "check-packaging-catalog";
  run.disabled = state.packagingCatalogCheckLoading
    || ["queued", "running"].includes(state.packagingCatalogCheck?.status);
  run.addEventListener("click", () => requestPackagingCatalogCheck(run));
  heading.append(copy, run);
  section.append(heading);

  if (state.packagingCatalogCheckLoading && state.packagingCatalogCheck === null) {
    section.setAttribute("aria-busy", "true");
    section.append(inlineFeedback("正在读取最近一次检查…", "素材库仍可正常浏览。"));
    return section;
  }
  const model = buildCatalogCheckModel(state.packagingCatalogCheck || {});
  const labels = {
    invalid: "无效",
    missing: "缺失",
    updated: "已变化",
    added: "新增",
    unchanged: "未变化",
  };
  const statusText = model.status === "completed"
    ? (model.hasProblems ? "检查完成，请处理下列变化" : "检查完成，未发现需处理的变化")
    : model.status === "not_checked" ? "尚未执行目录检查" : "目录检查正在后台运行";
  section.append(element("b", "packaging-check-status", statusText));
  const counts = element("div", "packaging-check-counts");
  Object.entries(labels).forEach(([key, label]) => {
    counts.append(element("span", key, label + " " + model.counts[key]));
  });
  section.append(counts);
  const changed = model.items.filter((item) => item.status !== "unchanged");
  if (changed.length) {
    const list = element("div", "packaging-check-results");
    changed.forEach((item) => {
      const row = element("div", "packaging-check-row " + item.status);
      row.append(
        element("b", null, labels[item.status] || item.status),
        element("code", null, item.relativePath || "未知相对路径"),
        element("span", null, item.reasonCode || (item.selectable ? "可选择" : "需人工核验")),
      );
      list.append(row);
    });
    section.append(list);
  }
  if (["queued", "running"].includes(model.status)) schedulePackagingCatalogRefresh();
  return section;
}

function renderPackagingCatalog() {
  if (state.packagingCatalogCheck === null && !state.packagingCatalogCheckLoading) {
    void loadPackagingCatalogCheck();
  }
  const actual = new Map(
    (state.bootstrap.packagingCatalog || []).map((item) => [item.key, item]),
  );
  const container = root.querySelector("#packaging-catalog");
  const definitions = filterPackagingCategories(
    PACKAGING_CATEGORIES,
    state.packagingFilter,
  );
  container.replaceChildren(renderPackagingCatalogCheck(), ...definitions.map((definition) => {
    const category = actual.get(definition.key) || { count: 0, assets: [] };
    const card = button("", "card pack-card");
    card.dataset.category = definition.key;
    card.classList.toggle("selected", state.selectedPackagingCategory === definition.key);
    const availableCount = category.assets.filter(
      (asset) => asset.availabilityStatus === "available",
    ).length;
    const externalCount = category.assets.filter(
      (asset) => asset.availabilityStatus === "external",
    ).length;
    const countLabel = category.count + " 项登记 · " + availableCount + " 项文件可用"
      + (externalCount ? " · " + externalCount + " 项需服务配置" : "");
    card.append(
      element("span", "pack-icon", definition.label.slice(0, 1)),
      element("h3", null, definition.label),
      element("p", null, countLabel),
    );
    const summary = element("div", "asset-summary");
    if (!category.assets.length) summary.append(element("span", null, "暂无可用素材"));
    for (const asset of category.assets.slice(0, 3)) {
      const row = element("span");
      row.append(
        element("b", null, asset.name),
        element(
          "em",
          asset.availabilityStatus === "missing" ? "review" : "approved",
          packagingAvailabilityLabel(asset.availabilityStatus),
        ),
      );
      summary.append(row);
    }
    card.append(summary);
    card.addEventListener("click", () => renderPackagingDetail(definition.key));
    return card;
  }));
  if (
    state.selectedPackagingCategory
    && !definitions.some((item) => item.key === state.selectedPackagingCategory)
  ) {
    state.selectedPackagingCategory = null;
    root.querySelector("#packaging-detail").hidden = true;
  }
}

function selectPackagingFilter(filter) {
  const valid = PACKAGING_GROUPS.some((item) => item.key === filter) ? filter : "all";
  state.packagingFilter = valid;
  root.querySelectorAll("[data-packaging-filter]").forEach((control) => {
    control.classList.toggle("selected", control.dataset.packagingFilter === valid);
  });
  renderPackagingCatalog();
}

function renderPackagingDetail(categoryKey) {
  const definition = PACKAGING_CATEGORIES.find((item) => item.key === categoryKey);
  const category = (state.bootstrap.packagingCatalog || []).find(
    (item) => item.key === categoryKey,
  );
  if (!definition || !category) return;
  state.selectedPackagingCategory = categoryKey;
  renderPackagingCatalog();
  const panel = root.querySelector("#packaging-detail");
  panel.hidden = false;
  const head = element("div", "packaging-detail-head");
  const title = element("div");
  title.append(element("h2", null, definition.label), element("p", null, category.count + " 项登记"));
  const close = button("关闭", "btn");
  close.addEventListener("click", () => {
    state.selectedPackagingCategory = null;
    panel.hidden = true;
    renderPackagingCatalog();
  });
  head.append(title, close);
  const list = element("div", "packaging-asset-list");
  const directories = element("div", "packaging-directories");
  for (const directory of category.directories || []) {
    const directoryRow = element("div", "packaging-directory-row");
    directoryRow.append(
      element("b", null, directory.label),
      element("code", null, directory.path),
      element("em", directory.exists ? "available" : "missing", directory.exists ? "已建立" : "尚未建立"),
    );
    directories.append(directoryRow);
  }
  for (const asset of category.assets) {
    const row = element("article", "packaging-asset-row");
    const copy = element("div", "packaging-asset-copy");
    copy.append(
      element("b", null, asset.name),
      element("span", null, packagingAvailabilityLabel(asset.availabilityStatus) + " · " + authorizationLabel(asset.authorizationStatus)),
      element("code", null, asset.relativePath || (categoryKey === "voice" ? "音色由配音模型和音色 ID 提供" : "该预设不依赖独立文件")),
    );
    row.append(copy);
    if (asset.previewKind === "audio" && asset.previewUrl) {
      const audio = element("audio", "asset-preview-audio");
      audio.controls = true;
      audio.preload = "none";
      audio.src = asset.previewUrl;
      row.append(audio);
    } else if (asset.previewKind === "image" && asset.previewUrl) {
      const image = element("img", "asset-preview-image");
      image.loading = "lazy";
      image.alt = asset.name + "预览";
      image.src = asset.previewUrl;
      row.append(image);
    } else if (asset.previewKind === "video" && asset.previewUrl) {
      const video = element("video", "asset-preview-video");
      video.controls = true;
      video.preload = "metadata";
      video.setAttribute("playsinline", "");
      video.src = asset.previewUrl;
      row.append(video);
    }
    list.append(row);
  }
  if (!category.assets.length) list.append(element("div", "empty-inline", "该分类尚未登记素材。"));
  panel.replaceChildren(head, directories, list);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return bytes + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return size.toFixed(size >= 10 ? 1 : 2) + " " + units[index];
}

function renderStorageSettings(storage) {
  state.storage = storage;
  const personal = personalPackagingStorageView(storage.personalPackaging);
  root.querySelector("#personal-packaging-path").textContent = personal.path;
  root.querySelector("#personal-packaging-location").textContent = personal.locationLabel;
  const personalStatus = root.querySelector("#personal-packaging-status");
  personalStatus.className = personal.statusClass;
  personalStatus.textContent = personal.statusLabel;
  root.querySelector("[data-pick-personal-packaging]").textContent = personal.statusClass === "missing"
    ? "重新选择目录"
    : "选择目录";
  const summary = root.querySelector("#storage-summary");
  summary.replaceChildren(
    ...[
      ["磁盘剩余", formatBytes(storage.disk.freeBytes)],
      ["预览缓存", formatBytes(storage.cache.totalBytes)],
      ["缓存文件", storage.cache.entries + " 项"],
    ].map(([label, value]) => {
      const card = element("article", "card storage-card");
      card.append(element("span", null, label), element("b", null, value));
      return card;
    }),
  );
  root.querySelector("#storage-directories").replaceChildren(
    ...storage.directories.map((item) => {
      const row = element("div", "storage-directory-row");
      row.append(
        element("b", null, item.label),
        element("code", null, item.path),
        element("em", item.exists ? "available" : "missing", item.exists ? "已建立" : "尚未建立"),
      );
      return row;
    }),
  );
}

async function loadStorageSettings() {
  try {
    renderStorageSettings(await api("/api/settings/storage"));
  } catch (error) {
    showToast(error.message);
  }
}

async function choosePersonalPackagingRoot() {
  try {
    const selected = await api("/api/picker/directory", jsonRequest("POST"));
    if (!selected.path) return;
    await api(
      "/api/settings/storage/personal-packaging",
      jsonRequest("PUT", { path: selected.path }),
    );
    await refreshBootstrap();
    await loadStorageSettings();
    showToast("个人包装素材目录已切换；原目录文件没有移动。");
  } catch (error) {
    showToast(error.message);
  }
}

async function previewCacheCleanup() {
  try {
    const result = await api("/api/settings/storage/cache/preview", jsonRequest("POST"));
    const panel = root.querySelector("#cache-clean-preview");
    panel.hidden = false;
    panel.textContent = result.deleted.length
      ? "将清理 " + result.deleted.length + " 项，预计释放 " + formatBytes(result.freedBytes) + "。"
      : "当前没有需要清理的预览缓存。";
    state.cacheCleanupToken = result.impactToken;
    root.querySelector("[data-confirm-cache-cleanup]").disabled = !result.deleted.length;
  } catch (error) {
    showToast(error.message);
  }
}

async function cleanPreviewCache() {
  try {
    if (!state.cacheCleanupToken) {
      showToast("请先预览本次清理范围。");
      return;
    }
    const result = await api(
      "/api/settings/storage/cache/clean",
      jsonRequest("POST", { impactToken: state.cacheCleanupToken }),
    );
    showToast("缓存清理完成，释放 " + formatBytes(result.freedBytes) + "。");
    root.querySelector("[data-confirm-cache-cleanup]").disabled = true;
    state.cacheCleanupToken = null;
    root.querySelector("#cache-clean-preview").hidden = true;
    await loadStorageSettings();
  } catch (error) {
    if (error.code === "CACHE_PREVIEW_STALE") {
      state.cacheCleanupToken = null;
      root.querySelector("[data-confirm-cache-cleanup]").disabled = true;
      const preview = root.querySelector("#cache-clean-preview");
      preview.hidden = false;
      preview.textContent = "缓存内容已变化，请重新预览清理范围后再确认。";
    }
    showToast(error.message);
  }
}

function buildProviderCredentialsForm(account) {
  const form = element("form", "provider-credentials-form");
  form.dataset.accountId = account.accountId;
  const settings = account.settings || {};
  const fields = [
    ["ttsApiKey", "豆包 API Key（新版控制台；留空则保持不变）", "", "password"],
    ["accessKeyId", "Access Key ID（仅刷新音色目录）", settings.access_key_id || "", "text"],
    ["secretAccessKey", "Secret Access Key（仅刷新音色目录；留空则保持不变）", "", "password"],
  ];
  form.append(element(
    "p",
    "provider-tts-guide",
    "API Key 用于合成、试听和连接检测；AK/SK 用于自动获取音色目录。保存并检测会生成一段很短的测试语音。",
  ));
  for (const [name, labelText, value, type] of fields) {
    const label = element("label", null, labelText);
    const input = document.createElement("input");
    input.className = "field";
    input.name = name;
    input.type = type;
    input.value = value;
    input.autocomplete = type === "password" ? "new-password" : "off";
    label.append(input);
    form.append(label);
  }
  const legacy = element("details", "legacy-tts-credentials");
  legacy.append(
    element("summary", null, "旧版兼容设置"),
    element("p", null, "仅旧版豆包控制台账号需要填写；留空会保持已保存值不变。"),
  );
  for (const [name, labelText, value, type] of [
    ["appId", "豆包 AppID（可选）", settings.app_id || "", "text"],
    ["secret", "Access Token（可选；留空则保持不变）", "", "password"],
  ]) {
    const label = element("label", null, labelText);
    const input = document.createElement("input");
    input.className = "field";
    input.name = name;
    input.type = type;
    input.value = value;
    input.autocomplete = type === "password" ? "new-password" : "off";
    label.append(input);
    legacy.append(label);
  }
  const resourceLabel = element("label", null, "配音服务版本");
  const resource = document.createElement("select");
  resource.className = "field";
  resource.name = "ttsResourceId";
  const preset = providerPresetDefinition("doubao_tts");
  resource.replaceChildren(...preset.ttsResources.map((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label;
    return option;
  }));
  resource.value = settings.tts_resource_id || "seed-tts-1.0";
  resourceLabel.append(resource);
  const save = button("保存并检测", "btn primary");
  save.type = "submit";
  const check = state.providerCredentialChecks[account.accountId];
  setButtonBusy(
    save,
    state.checkingProviderCredentials.has(account.accountId),
    check?.title || "正在检测…",
  );
  form.append(resourceLabel, legacy, save);
  form.addEventListener("submit", updateProviderCredentials);
  return form;
}

function providerProfileForCredentialCheck(accountId, resourceId) {
  const profiles = (state.bootstrap.modelProfiles || []).filter(
    (profile) => profile.capability === "tts" && profile.accountId === accountId,
  );
  const matching = profiles.filter(
    (profile) => profile.settings?.resource_id === resourceId,
  );
  return matching.find(
    (profile) => profile.isDefault && profile.availabilityStatus !== "unavailable",
  )
    || matching.find((profile) => profile.availabilityStatus === "available")
    || matching.find((profile) => profile.isDefault)
    || matching[0]
    || profiles.find(
      (profile) => profile.isDefault && profile.availabilityStatus !== "unavailable",
    )
    || profiles.find((profile) => profile.availabilityStatus === "available")
    || profiles.find((profile) => profile.isDefault)
    || profiles[0]
    || null;
}

function renderProviderCredentialCheck(accountId) {
  const check = state.providerCredentialChecks[accountId];
  if (!check) return null;
  return inlineFeedback(check.title, check.detail, check.kind);
}

function setProviderCredentialCheck(accountId, kind, title, detail) {
  state.providerCredentialChecks[accountId] = { kind, title, detail };
  renderSettings();
}

async function loadProviderVoiceCatalog(accountId) {
  if (!accountId || state.providerVoices[accountId] || state.loadingProviderVoices.has(accountId)) return;
  state.loadingProviderVoices.add(accountId);
  try {
    state.providerVoices[accountId] = await api(
      "/api/settings/provider-accounts/" + encodeURIComponent(accountId) + "/voices",
    );
  } catch (error) {
    state.providerVoices[accountId] = {
      voices: [],
      state: { status: "failed", lastErrorMessage: error.message },
    };
  } finally {
    state.loadingProviderVoices.delete(accountId);
    if ((window.location.hash || "") === "#/settings") renderSettings();
  }
}

async function refreshProviderVoiceCatalog(accountId) {
  state.refreshingVoiceCatalogs.add(accountId);
  renderSettings();
  try {
    state.providerVoices[accountId] = await api(
      "/api/settings/provider-accounts/" + encodeURIComponent(accountId) + "/voices/refresh",
      jsonRequest("POST"),
    );
    for (const profile of state.bootstrap.modelProfiles || []) {
      if (profile.accountId === accountId) delete state.profileVoices[profile.profileId];
    }
    showToast("音色目录已刷新，共 " + (state.providerVoices[accountId].voices || []).filter((voice) => voice.selectable).length + " 个可选音色。");
  } catch (error) {
    showErrorToast("音色目录刷新失败：" + error.message);
    delete state.providerVoices[accountId];
  } finally {
    state.refreshingVoiceCatalogs.delete(accountId);
    renderSettings();
  }
}

async function updateProviderCredentials(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const accountId = form.dataset.accountId;
  if (state.checkingProviderCredentials.has(accountId)) return;
  const values = Object.fromEntries(new FormData(form));
  state.checkingProviderCredentials.add(accountId);
  let saved = false;
  let connected = false;
  setButtonBusy(form.querySelector("[type='submit']"), true, "正在保存…");
  setProviderCredentialCheck(
    accountId,
    "progress",
    "1/3 正在保存凭据",
    "密钥只写入 Windows 凭据管理器，不会写入项目文件。",
  );
  try {
    await api(
      "/api/settings/provider-accounts/" + encodeURIComponent(accountId) + "/credentials",
      jsonRequest("PUT", values),
    );
    saved = true;
    await refreshBootstrap();
    const profile = providerProfileForCredentialCheck(accountId, values.ttsResourceId);
    if (!profile) {
      setProviderCredentialCheck(
        accountId,
        "partial",
        "凭据已保存，等待配音配置",
        "请先在“配音与试听”中建立一个配音配置，再运行连接检测。",
      );
      showToast("豆包凭据已保存；建立配音配置后即可检测。");
      return;
    }
    setProviderCredentialCheck(
      accountId,
      "progress",
      "2/3 正在检测连接",
      "系统会生成一段很短的测试语音，用于确认所选服务版本可用。",
    );
    await api(
      "/api/settings/model-profiles/" + encodeURIComponent(profile.profileId) + "/test",
      jsonRequest("POST"),
    );
    connected = true;
    await refreshBootstrap();
    const account = (state.bootstrap.providerAccounts || []).find(
      (item) => item.accountId === accountId,
    );
    if (!account?.voiceCatalogConfigured) {
      setProviderCredentialCheck(
        accountId,
        "partial",
        "连接检测通过，音色目录未刷新",
        "补充 Access Key ID 和 Secret Access Key 后，可自动获取全部可选音色。",
      );
      showToast("豆包连接检测通过；补充 AK/SK 后可刷新音色目录。");
      return;
    }
    setProviderCredentialCheck(
      accountId,
      "progress",
      "3/3 正在刷新音色",
      "正在从火山引擎读取当前账号可选择的音色。",
    );
    state.providerVoices[accountId] = await api(
      "/api/settings/provider-accounts/" + encodeURIComponent(accountId) + "/voices/refresh",
      jsonRequest("POST"),
    );
    for (const item of state.bootstrap.modelProfiles || []) {
      if (item.accountId === accountId) delete state.profileVoices[item.profileId];
    }
    const selectable = (state.providerVoices[accountId].voices || []).filter(
      (voice) => voice.selectable,
    ).length;
    state.editingProviderCredentials.delete(accountId);
    setProviderCredentialCheck(
      accountId,
      "success",
      "保存并检测通过",
      "连接可用，音色目录已刷新，共 " + selectable + " 个可选音色。",
    );
    showToast("豆包凭据已保存并检测通过。");
  } catch (error) {
    if (!saved) {
      setProviderCredentialCheck(
        accountId,
        "error",
        "凭据保存失败",
        error.message,
      );
      showErrorToast("豆包凭据保存失败：" + error.message);
    } else {
      setProviderCredentialCheck(
        accountId,
        "partial",
        connected ? "连接通过，音色目录刷新失败" : "凭据已保存，连接检测未通过",
        error.message,
      );
      showErrorToast(
        (connected ? "豆包连接通过，但音色目录刷新失败：" : "豆包凭据已保存，但连接检测未通过：")
          + error.message,
      );
      await refreshBootstrap().catch(() => {});
    }
  } finally {
    state.checkingProviderCredentials.delete(accountId);
    renderSettings();
  }
}

function ttsResourceLabel(resourceId) {
  return providerPresetDefinition("doubao_tts").ttsResources.find(
    (resource) => resource.id === resourceId,
  )?.label || "配音服务版本待选择";
}

function renderSettings() {
  const container = root.querySelector("#settings-capabilities");
  const capabilities = state.bootstrap.settingsCapabilities || [];
  const accounts = state.bootstrap.providerAccounts || [];
  const profiles = state.bootstrap.modelProfiles || [];
  const grouped = groupModelProfiles(profiles);
  const accountList = root.querySelector("#provider-account-list");
  accountList.replaceChildren(...accounts.map((account) => {
    const preset = providerPresetDefinition(account.preset || account.provider);
    const presentation = providerAccountPresentation(account);
    const row = element("article", "provider-account");
    const identity = element("div", "provider-account-identity");
    identity.append(
      element("b", null, presentation.name),
      element(
        "span",
        null,
        account.provider === "doubao_tts"
          ? "豆包配音服务 · " + ttsResourceLabel(account.settings?.tts_resource_id)
          : presentation.detail,
      ),
    );
    if (account.provider === "doubao_tts") {
      const catalog = state.providerVoices[account.accountId];
      const badges = element("span", "provider-config-badges");
      badges.append(
        element("em", account.ttsConfigured ? "available" : "missing",
          account.ttsApiKeyConfigured
            ? "新版 API Key 已配置"
            : account.legacyTtsConfigured
              ? "旧版合成凭据已配置"
              : "缺少豆包 API Key"),
        element("em", account.voiceCatalogConfigured ? "available" : "missing",
          account.voiceCatalogConfigured ? "音色目录凭据已配置" : "缺少 AK / SK"),
      );
      identity.append(badges);
      identity.append(element(
        "small",
        "profile-checked",
        catalog?.state?.lastSuccessAt
          ? "音色最近刷新：" + catalog.state.lastSuccessAt + " · " + (catalog.voices || []).filter((voice) => voice.selectable).length + " 个可选"
          : catalog?.state?.lastErrorMessage || "音色目录尚未刷新",
      ));
      const credentialCheck = renderProviderCredentialCheck(account.accountId);
      if (credentialCheck) identity.append(credentialCheck);
      if (!catalog && !state.loadingProviderVoices.has(account.accountId)) {
        void loadProviderVoiceCatalog(account.accountId);
      }
    }
    const actions = element("div", "service-actions");
    if (preset.supportsModelCatalog) {
      const models = button("获取模型", "btn");
      models.addEventListener("click", () => fetchProviderModels(account.accountId));
      actions.append(models);
    }
    if (preset.supportsVoiceCatalog) {
      const editing = state.editingProviderCredentials.has(account.accountId);
      const credentials = button(editing ? "收起凭据" : "补充/更新凭据", "btn");
      credentials.addEventListener("click", () => {
        if (editing) state.editingProviderCredentials.delete(account.accountId);
        else state.editingProviderCredentials.add(account.accountId);
        renderSettings();
      });
      const refreshing = state.refreshingVoiceCatalogs.has(account.accountId);
      const voices = button("获取/刷新音色", "btn");
      setButtonBusy(voices, refreshing, "正在刷新…");
      if (!account.voiceCatalogConfigured) voices.disabled = true;
      voices.addEventListener("click", () => refreshProviderVoiceCatalog(account.accountId));
      actions.append(credentials, voices);
    }
    const remove = button("删除", "btn danger");
    remove.addEventListener("click", () => deleteProviderAccount(account));
    actions.append(remove);
    if (account.channelId) {
      actions.append(element("em", null, "固定渠道 " + account.channelId));
    }
    row.append(identity, actions);
    if (preset.supportsVoiceCatalog && state.editingProviderCredentials.has(account.accountId)) {
      row.append(buildProviderCredentialsForm(account));
    }
    return row;
  }));
  if (!accounts.length) {
    accountList.append(element("p", "muted-copy", "尚未添加供应商账号。"));
  }

  container.replaceChildren(...capabilities.map((capability) => {
    const card = element("section", "service model-service");
    const copy = element("div", "service-copy");
    const capabilityProfiles = grouped[capability.capability] || [];
    copy.append(
      element("b", null, capability.label),
      element("span", null, CAPABILITY_HELP[capability.capability] || ""),
      element(
        "em",
        capabilityProfiles.some((item) => item.availabilityStatus === "available")
          ? "configured" : "not-configured",
        capabilityProfiles.length + " 个配置",
      ),
    );
    const list = element("div", "model-profile-list");
    capabilityProfiles.forEach((profile) => {
      const row = element("article", "model-profile-row");
      const identity = element("div", "model-profile-identity");
      const voicePayload = state.profileVoices[profile.profileId];
      const commonVoiceId = String(
        profile.settings?.default_voice_id || profile.settings?.voice || "",
      );
      const commonVoice = voicePayload?.voices?.find(
        (voice) => voice.voiceId === commonVoiceId,
      );
      identity.append(
        element("b", null, profile.displayName),
        element(
          "span",
          null,
          profile.provider === "doubao_tts"
            ? profile.accountName + " · 豆包配音服务"
            : profile.accountName + " · " + profile.model,
        ),
      );
      if (profile.provider === "doubao_tts") {
        identity.append(element(
          "span",
          "profile-common-voice",
          "常用音色：" + (
            commonVoice
              ? voiceOptionPresentation(commonVoice).label
              : voicePayload ? "当前目录中不可用" : "正在读取…"
          ),
        ));
        if (!voicePayload && !state.loadingProfileVoices.has(profile.profileId)) {
          void loadProfileVoices(profile.profileId);
        }
      }
      if (profile.lastErrorMessage) {
        const error = element(
          "span",
          "profile-error",
          profile.lastErrorMessage + (profile.lastErrorCode ? "（" + profile.lastErrorCode + "）" : ""),
        );
        if (profile.lastErrorCode === "MODEL_RESOURCE_NOT_GRANTED") {
          const recovery = element("a", "profile-error-action");
          recovery.href = "https://console.volcengine.com/speech/service/10";
          recovery.target = "_blank";
          recovery.rel = "noopener noreferrer";
          recovery.textContent = "去火山控制台开通服务";
          error.append(document.createElement("br"), recovery);
        }
        identity.append(error);
      }
      if (profile.lastCheckedAt) {
        identity.append(element("small", "profile-checked", "最近检测：" + profile.lastCheckedAt));
      }
      const status = statusBadge(
        availabilityLabel(profile.availabilityStatus) + (profile.isDefault ? " · 默认" : ""),
        profile.availabilityStatus === "available"
          ? "success"
          : profile.availabilityStatus === "unavailable" ? "error" : "neutral",
      );
      status.classList.add("profile-status", profile.availabilityStatus);
      const actions = element("div", "service-actions");
      const isTesting = state.testingProfiles.has(profile.profileId);
      const testButton = button("测试连接", "btn");
      setButtonBusy(testButton, isTesting, "检测中…");
      testButton.addEventListener("click", () => testModelProfile(profile.profileId));
      actions.append(testButton);
      if (profile.provider === "doubao_tts" && commonVoiceId) {
        const voiceTestKey = "profile:" + profile.profileId;
        const testingVoice = state.testingVoices.has(voiceTestKey);
        const voiceTest = button(testingVoice ? "试听生成中…" : "试听常用音色", "btn");
        voiceTest.disabled = testingVoice;
        voiceTest.addEventListener("click", () => testProfileVoice(profile.profileId, commonVoiceId));
        actions.append(voiceTest);
      }
      if (profile.provider === "doubao_tts") {
        const voiceEditor = element("div", "profile-voice-editor");
        const voiceSelect = document.createElement("select");
        voiceSelect.className = "field";
        const voices = voicePayload?.voices || [];
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = voicePayload
          ? voices.length ? "请选择新的常用音色" : "当前没有可选音色"
          : "正在读取音色目录…";
        voiceSelect.replaceChildren(placeholder, ...voices.map((voice) => {
          const presentation = voiceOptionPresentation(voice);
          const option = document.createElement("option");
          option.value = voice.voiceId;
          option.textContent = presentation.label
            + (presentation.detail ? " · " + presentation.detail : "");
          return option;
        }));
        voiceSelect.value = voices.some((voice) => voice.voiceId === commonVoiceId)
          ? commonVoiceId
          : "";
        const saveVoice = button("修改常用音色", "btn");
        const updateDisabled = () => {
          saveVoice.disabled = !voiceSelect.value
            || voiceSelect.value === commonVoiceId;
        };
        voiceSelect.addEventListener("change", updateDisabled);
        saveVoice.addEventListener("click", () => updateProfileDefaultVoice(
          profile.profileId,
          voiceSelect.value,
        ));
        updateDisabled();
        voiceEditor.append(voiceSelect, saveVoice);
        actions.append(voiceEditor);
      }
      if (!profile.isDefault) {
        const defaultButton = button("设为默认", "btn");
        defaultButton.addEventListener("click", () => setDefaultModelProfile(profile.profileId));
        actions.append(defaultButton);
      }
      const remove = button("删除", "btn danger");
      remove.addEventListener("click", () => deleteModelProfile(profile));
      actions.append(remove);
      row.append(identity, status, actions);
      if (state.voicePreviewUrls[profile.profileId]) {
        const audio = document.createElement("audio");
        audio.className = "voice-preview-audio profile-voice-preview";
        audio.controls = true;
        audio.src = state.voicePreviewUrls[profile.profileId];
        row.append(audio);
      }
      list.append(row);
    });
    if (!capabilityProfiles.length) {
      list.append(element("p", "muted-copy", "尚未添加模型配置。"));
    }
    card.append(copy, list);
    {
      const form = element("form", "model-profile-form");
      form.dataset.capability = capability.capability;
      const compatibleAccounts = accounts.filter(
        (item) => providerSupportsCapability(item, capability.capability),
      );
      const account = document.createElement("select");
      account.className = "field";
      account.name = "accountId";
      account.required = true;
      account.replaceChildren(...compatibleAccounts.map((item) => {
        const option = document.createElement("option");
        option.value = item.accountId;
        option.textContent = item.name;
        return option;
      }));
      const displayName = document.createElement("input");
      displayName.className = "field";
      displayName.name = "displayName";
      displayName.placeholder = "配置名称";
      displayName.required = true;
      const model = document.createElement("input");
      model.className = "field";
      model.name = "model";
      model.placeholder = "精确模型 ID";
      model.required = true;
      const modelOptions = document.createElement("datalist");
      modelOptions.id = "provider-models-" + capability.capability;
      model.setAttribute("list", modelOptions.id);
      const manualVoice = document.createElement("input");
      manualVoice.className = "field";
      manualVoice.name = "voice";
      manualVoice.placeholder = "高级兼容音色 ID";
      const catalogVoice = document.createElement("select");
      catalogVoice.className = "field";
      catalogVoice.name = "defaultVoiceId";
      const resource = document.createElement("input");
      resource.type = "hidden";
      resource.name = "resourceId";
      const voiceHelp = element("small", "field-help");
      const applySuggestedName = () => {
        if (displayName.dataset.userEdited === "true") return;
        const selected = compatibleAccounts.find((item) => item.accountId === account.value);
        displayName.value = selected?.provider === "doubao_tts"
          ? suggestedModelProfileName(selected?.name, capability.capability, "")
          : suggestedModelProfileName(selected?.name, capability.capability, model.value);
      };
      const applyAccountDefaults = () => {
        const selected = compatibleAccounts.find((item) => item.accountId === account.value);
        const catalogMode = capability.capability === "tts" && selected?.provider === "doubao_tts";
        const models = state.providerModels[account.value] || [];
        modelOptions.replaceChildren(...models.map((modelId) => {
          const option = document.createElement("option");
          option.value = modelId;
          return option;
        }));
        if (!catalogMode &&
          selected?.defaultModel
          && (!model.value || model.dataset.userEdited !== "true")
        ) {
          model.value = selected.defaultModel;
        }
        model.hidden = catalogMode;
        model.required = !catalogMode;
        manualVoice.hidden = capability.capability !== "tts" || catalogMode;
        manualVoice.required = capability.capability === "tts" && !catalogMode;
        catalogVoice.hidden = !catalogMode;
        catalogVoice.required = catalogMode;
        voiceHelp.hidden = !catalogMode;
        resource.value = catalogMode ? String(selected?.settings?.tts_resource_id || "") : "";
        if (catalogMode) {
          const payload = state.providerVoices[account.value];
          const choices = (payload?.voices || []).filter((voice) => voice.selectable);
          const placeholder = document.createElement("option");
          placeholder.value = "";
          placeholder.textContent = payload
            ? choices.length ? "请选择常用音色" : "请先获取/刷新音色"
            : "正在读取音色目录…";
          catalogVoice.replaceChildren(placeholder, ...choices.map((voice) => {
            const presentation = voiceOptionPresentation(voice);
            const option = document.createElement("option");
            option.value = voice.voiceId;
            option.textContent = presentation.label + (presentation.detail ? " · " + presentation.detail : "");
            return option;
          }));
          voiceHelp.textContent = "这里选择的是以后新任务预选的常用音色；每条广告仍可单独更改。";
          if (!payload) void loadProviderVoiceCatalog(account.value);
        }
        applySuggestedName();
      };
      account.addEventListener("change", applyAccountDefaults);
      displayName.addEventListener("input", () => { displayName.dataset.userEdited = "true"; });
      model.addEventListener("input", () => {
        model.dataset.userEdited = "true";
        applySuggestedName();
      });
      form.append(
        account,
        displayName,
        model,
        modelOptions,
        resource,
        manualVoice,
        catalogVoice,
        voiceHelp,
      );
      applyAccountDefaults();
      const add = button("保存并检测", "btn primary");
      add.type = "submit";
      add.disabled = !compatibleAccounts.length;
      form.append(add);
      form.addEventListener("submit", saveModelProfile);
      card.append(form);
    }
    return card;
  }));
}

async function saveProviderAccount(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const values = Object.fromEntries(new FormData(form));
    if (values.preset !== "doubao_tts") {
      values.secret = values.genericSecret;
    }
    delete values.genericSecret;
    await api("/api/settings/provider-accounts", jsonRequest("POST", values));
    form.reset();
    delete form.elements.namedItem("baseUrl").dataset.userEdited;
    applyProviderPresetForm(form, { force: true });
    await refreshBootstrap();
    renderSettings();
    showToast("供应商账号已保存，密钥只在 Windows 凭据管理器中保存。");
  } catch (error) { showErrorToast(error.message); }
}

function applyProviderPresetForm(form, { force = false } = {}) {
  const preset = providerPresetDefinition(form.elements.namedItem("preset").value);
  const baseUrl = form.elements.namedItem("baseUrl");
  if (force || !baseUrl.value || baseUrl.dataset.userEdited !== "true") {
    baseUrl.value = preset.defaultBaseUrl;
  }
  form.querySelector("[data-provider-channel]").hidden = !preset.showChannel;
  form.querySelectorAll("[data-provider-generic]").forEach((container) => {
    container.hidden = preset.showTtsFields;
    container.querySelectorAll("input, select").forEach((field) => {
      field.disabled = preset.showTtsFields;
    });
  });
  form.querySelectorAll("[data-provider-tts]").forEach((container) => {
    container.hidden = !preset.showTtsFields;
    container.querySelectorAll("input, select").forEach((field) => {
      field.disabled = !preset.showTtsFields;
      field.required = preset.showTtsFields && field.dataset.ttsRequired === "true";
    });
  });
  form.querySelectorAll("[data-provider-tts-legacy]").forEach((container) => {
    container.hidden = !preset.showTtsFields;
    container.querySelectorAll("input, select").forEach((field) => {
      field.disabled = !preset.showTtsFields;
      field.required = false;
    });
  });
}

async function fetchProviderModels(accountId) {
  try {
    const result = await api(
      "/api/settings/provider-accounts/" + encodeURIComponent(accountId) + "/models",
      jsonRequest("POST"),
    );
    state.providerModels[accountId] = result.models || [];
    renderSettings();
    showToast("已获取 " + state.providerModels[accountId].length + " 个模型。");
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteProviderAccount(account) {
  if (!confirm("删除供应商“" + account.name + "”？已被模型配置使用的账号不会删除。")) {
    return;
  }
  try {
    await api(
      "/api/settings/provider-accounts/" + encodeURIComponent(account.accountId),
      jsonRequest("DELETE"),
    );
    delete state.providerModels[account.accountId];
    await refreshBootstrap();
    renderSettings();
    showToast("供应商账号及其本机凭据已删除。");
  } catch (error) {
    showToast(error.message);
  }
}

async function saveModelProfile(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  values.capability = form.dataset.capability;
  try {
    const created = await api("/api/settings/model-profiles", jsonRequest("POST", values));
    form.reset();
    await testModelProfile(created.profileId, { created: true });
  } catch (error) {
    await refreshBootstrap();
    renderSettings();
    showToast(error.message);
  }
}

async function testModelProfile(profileId, { created = false } = {}) {
  state.testingProfiles.add(profileId);
  renderSettings();
  try {
    await api(
      "/api/settings/model-profiles/" + encodeURIComponent(profileId) + "/test",
      jsonRequest("POST"),
    );
    await refreshBootstrap();
    renderSettings();
    showToast(created ? "模型已保存并检测通过，可以用于新任务。" : "连接测试通过，该配置可以用于新任务。");
  } catch (error) {
    await refreshBootstrap();
    renderSettings();
    showErrorToast((created ? "模型已保存，但检测未通过：" : "检测未通过：") + error.message);
  } finally {
    state.testingProfiles.delete(profileId);
    renderSettings();
  }
}

async function testProfileVoice(profileId, voiceId) {
  const key = "profile:" + profileId;
  state.testingVoices.add(key);
  renderSettings();
  try {
    const result = await api(
      "/api/settings/model-profiles/" + encodeURIComponent(profileId) + "/voice-test",
      jsonRequest("POST", { voiceId }),
    );
    state.voicePreviewUrls[profileId] = result.previewUrl;
    renderSettings();
    const audio = root.querySelector(
      ".profile-voice-preview[src='" + CSS.escape(result.previewUrl) + "']",
    );
    await audio?.play().catch(() => {});
    showToast("常用音色试听已生成。");
  } catch (error) {
    showErrorToast("音色试听失败：" + error.message);
  } finally {
    state.testingVoices.delete(key);
    renderSettings();
  }
}

async function updateProfileDefaultVoice(profileId, voiceId) {
  if (!voiceId) {
    showToast("请先选择新的常用音色。");
    return;
  }
  try {
    await api(
      "/api/settings/model-profiles/" + encodeURIComponent(profileId) + "/default-voice",
      jsonRequest("PUT", { voiceId }),
    );
    delete state.profileVoices[profileId];
    await refreshBootstrap();
    await loadProfileVoices(profileId);
    renderSettings();
    showToast("常用音色已更新；已有任务仍使用创建时冻结的音色。");
  } catch (error) {
    showErrorToast(error.message);
  }
}

async function deleteModelProfile(profile) {
  if (!confirm("删除模型配置“" + profile.displayName + "”？已被任务使用的配置不会删除。")) {
    return;
  }
  try {
    await api(
      "/api/settings/model-profiles/" + encodeURIComponent(profile.profileId),
      jsonRequest("DELETE"),
    );
    await refreshBootstrap();
    renderSettings();
    showToast("模型配置已删除。供应商账号和 API Key 未受影响。");
  } catch (error) {
    showToast(error.message);
  }
}

async function setDefaultModelProfile(profileId) {
  try {
    await api(
      "/api/settings/model-profiles/" + encodeURIComponent(profileId) + "/default",
      jsonRequest("POST"),
    );
    await refreshBootstrap();
    renderSettings();
    showToast("默认模型配置已更新。");
  } catch (error) { showToast(error.message); }
}

async function saveCapability(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  try {
    await api(
      "/api/settings/capabilities/" + encodeURIComponent(form.dataset.capability),
      jsonRequest("PUT", values),
    );
    showToast("该能力配置已保存到本机。");
    await refreshBootstrap();
    renderSettings();
  } catch (error) {
    showToast(error.message);
  }
}

async function clearCapability(capability) {
  try {
    await api(
      "/api/settings/capabilities/" + encodeURIComponent(capability),
      jsonRequest("DELETE"),
    );
    showToast("本机密钥已清除。");
    await refreshBootstrap();
    renderSettings();
  } catch (error) {
    showToast(error.message);
  }
}

async function refreshBootstrap() {
  state.bootstrap = await api("/api/bootstrap");
  renderTaskCenter();
  renderServiceState();
  renderCreateLibraries();
  renderCreateModelProfiles();
  renderLibraries();
  renderMaterialLibraryOptions();
  renderPackagingCatalog();
  restoreCreateDraft();
}

function routeFromHash() {
  const match = window.location.hash.match(/^#\/tasks\/([^/]+)$/);
  if (match) {
    openTask(decodeURIComponent(match[1]));
    return;
  }
  const route = window.location.hash || "#/tasks";
  if (route === "#/create") {
    if (root.querySelector("#preflight-panel").hidden) setCreateStep("route");
    showPage("create", { crumb: "任务中心 / 新建视频", context: "tasks" });
    return;
  }
  if (route === "#/library") {
    renderLibraries();
    renderMaterialLibraryOptions();
    renderMaterialIndex();
    showPage("library", { crumb: "镜头素材库", context: "library" });
    void loadMaterialIndex();
    return;
  }
  const packagingMatch = route.match(/^#\/packlib(?:\/([^/]+))?$/);
  if (packagingMatch) {
    selectPackagingFilter(packagingMatch[1] || "all");
    renderPackagingCatalog();
    showPage("packlib", { crumb: "包装素材库", context: "packlib" });
    return;
  }
  if (route === "#/settings") {
    root.querySelectorAll("[data-settings-section]").forEach((control) => control.classList.toggle("selected", control.dataset.settingsSection === "ai"));
    renderSettings();
    showPage("settings", { crumb: "设置 / AI 服务连接", context: "settings" });
    return;
  }
  if (route === "#/settings/storage") {
    root.querySelectorAll("[data-settings-section]").forEach((control) => control.classList.toggle("selected", control.dataset.settingsSection === "storage"));
    showPage("settings-storage", { crumb: "设置 / 存储与缓存", context: "settings" });
    void loadStorageSettings();
    return;
  }
  state.selectedTaskId = null;
  renderTaskCenter();
  showPage("tasks", { crumb: "任务中心", context: "tasks" });
}

function bindEvents() {
  const providerForm = root.querySelector("#provider-account-form");
  providerForm.addEventListener("submit", saveProviderAccount);
  providerForm.elements.namedItem("preset").addEventListener("change", () => {
    applyProviderPresetForm(providerForm);
  });
  providerForm.elements.namedItem("baseUrl").addEventListener("input", () => {
    providerForm.elements.namedItem("baseUrl").dataset.userEdited = "true";
  });
  root.querySelectorAll("[data-packaging-filter]").forEach((control) => {
    control.addEventListener("click", () => {
      const filter = control.dataset.packagingFilter;
      window.location.hash = filter === "all" ? "#/packlib" : "#/packlib/" + filter;
    });
  });
  root.querySelectorAll("[data-settings-section]").forEach((control) => {
    control.addEventListener("click", () => {
      window.location.hash = control.dataset.settingsSection === "storage"
        ? "#/settings/storage"
        : "#/settings";
    });
  });
  root.querySelector("[data-pick-personal-packaging]").addEventListener("click", choosePersonalPackagingRoot);
  root.querySelector("[data-preview-cache-cleanup]").addEventListener("click", previewCacheCleanup);
  root.querySelector("[data-confirm-cache-cleanup]").addEventListener("click", cleanPreviewCache);
  applyProviderPresetForm(providerForm);
  root.querySelector("[data-toggle-rail]").addEventListener("click", (event) => {
    const collapsed = app.classList.toggle("rail-off");
    event.currentTarget.setAttribute("aria-expanded", String(!collapsed));
    preferences.setRailCollapsed(collapsed);
  });
  themeButtons.forEach((button) => {
    button.addEventListener("click", () => setTheme(button.dataset.theme));
  });
  taskViewButtons.forEach((button) => {
    button.addEventListener("click", () => setTaskView(button.dataset.taskView));
  });
  ["#task-search", "#task-route-filter", "#task-status-filter", "#task-sort"].forEach((selector) => {
    const control = root.querySelector(selector);
    control.addEventListener(control.tagName === "INPUT" ? "input" : "change", updateTaskControls);
  });
  root.querySelector("#task-filter-reset").addEventListener("click", () => {
    root.querySelector("#task-search").value = "";
    root.querySelector("#task-route-filter").value = "";
    root.querySelector("#task-status-filter").value = "";
    root.querySelector("#task-sort").value = "updated_desc";
    updateTaskControls();
    root.querySelector("#task-search").focus();
  });
  navButtons.forEach((button) => {
    button.addEventListener("click", () => {
      window.location.hash = "#/" + button.dataset.page;
    });
  });
  root.querySelectorAll("[data-back-tasks]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.closest("[data-view='create']")) clearCreateDraft();
      window.location.hash = "#/tasks";
    });
  });
  root.querySelectorAll("[data-create-task]").forEach((button) => {
    button.addEventListener("click", () => {
      window.location.hash = "#/create";
    });
  });
  root.querySelector("[data-cancel-impact]").addEventListener("click", () => {
    state.pendingImpact = null;
    root.querySelector("#impact-modal").hidden = true;
  });
  root.querySelector("[data-confirm-impact]").addEventListener("click", async (event) => {
    if (!state.pendingImpact) return;
    const control = event.currentTarget;
    control.disabled = true;
    try {
      const pending = state.pendingImpact;
      if (pending.applyCandidate) {
        await taskPost("/storyboard/apply-candidate", {
          ...pending.applyCandidate,
          impactToken: pending.impact.impact_token,
        });
        delete state.storyboardFeedback[pending.applyCandidate.cardId];
        if (state.taskDetail) renderTaskWorkspace(state.taskDetail);
      } else {
        await taskPost("/rough-cut/apply", {
          edit: pending.edit,
          impactToken: pending.impact.impact_token,
        });
      }
      state.pendingImpact = null;
      root.querySelector("#impact-modal").hidden = true;
      showToast(
        pending.applyCandidate
          ? "所选重荐候选已应用，页面已从任务账本刷新。"
          : "镜头调整已保存，页面已从任务账本刷新。",
        { kind: "success" },
      );
    } catch (error) {
      showActionError(error);
    } finally {
      control.disabled = false;
    }
  });
  root.querySelector("#route-options").addEventListener("click", (event) => {
    const button = event.target.closest(".route:not(:disabled)");
    if (!button) return;
    root.querySelectorAll(".route").forEach((item) => {
      item.setAttribute("aria-pressed", String(item === button));
    });
    updateCreateRouteFields(button.dataset.route);
    saveCreateDraft();
  });
  root.querySelector("#tts-profile").addEventListener("change", (event) => {
    state.pendingCreateVoiceId = "";
    root.querySelector("#tts-voice").value = "";
    root.querySelector("#tts-voice-preview").hidden = true;
    renderCreateVoiceOptions();
    void loadProfileVoices(event.currentTarget.value);
    saveCreateDraft();
  });
  root.querySelector("#orchestration-mode").addEventListener("change", () => {
    updateCreateModeFields();
    saveCreateDraft();
  });
  root.querySelector("#tts-voice").addEventListener("change", () => {
    root.querySelector("#tts-voice-preview").hidden = true;
    renderCreateVoiceOptions();
    saveCreateDraft();
  });
  root.querySelector("[data-test-task-voice]").addEventListener("click", testSelectedTaskVoice);
  root.querySelector("[data-pick-video]").addEventListener("click", async () => {
    try {
      const result = await api("/api/picker/video", jsonRequest("POST"));
      root.querySelector("[name='digital_human_mp4']").value = result.path || "";
      saveCreateDraft();
    } catch (error) {
      showToast(error.message);
    }
  });
  root.querySelector("#create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    setPreflightPending(true);
    try {
      state.createRequest = buildCreateRequest(createFormValues());
      state.preflight = await api(
        "/api/tasks/preflight",
        jsonRequest("POST", state.createRequest),
      );
      renderPreflight(state.preflight);
    } catch (error) {
      showToast(error.message);
    } finally {
      setPreflightPending(false);
    }
  });
  root.querySelector("#create-form").addEventListener("input", () => {
    saveCreateDraft();
    if (!root.querySelector("#create-form").hidden) setCreateStep("input");
  });
  root.querySelector("#create-form").addEventListener("change", saveCreateDraft);
  root.querySelector("[data-edit-create]").addEventListener("click", () => {
    root.querySelector("#preflight-panel").hidden = true;
    root.querySelector("#create-form").hidden = false;
    setCreateStep("input");
  });
  root.querySelector("[data-create-confirm]").addEventListener("click", async () => {
    if (!state.createRequest) return;
    setCreateStep("create");
    try {
      const created = await api("/api/tasks", jsonRequest("POST", state.createRequest));
      taskDrafts.clear();
      await refreshBootstrap();
      window.location.hash = "#/tasks/" + encodeURIComponent(created.task_id);
    } catch (error) {
      setCreateStep("preflight");
      showToast(error.message);
    }
  });
  root.querySelector("[data-show-library-form]").addEventListener("click", () => {
    root.querySelector("#library-form").hidden = false;
  });
  root.querySelector("[data-cancel-library]").addEventListener("click", () => {
    root.querySelector("#library-form").hidden = true;
  });
  root.querySelector("[data-pick-directory]").addEventListener("click", async () => {
    try {
      const result = await api("/api/picker/directory", jsonRequest("POST"));
      root.querySelector("#library-form [name='mountPath']").value = result.path || "";
    } catch (error) {
      showToast(error.message);
    }
  });
  root.querySelector("#library-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    values.isDefaultGeneric = new FormData(form).has("isDefaultGeneric");
    try {
      await api("/api/libraries", jsonRequest("POST", buildLibraryRegistration(values)));
      form.reset();
      form.hidden = true;
      await refreshBootstrap();
      showToast("素材目录已登记；原始文件保持不变。");
    } catch (error) {
      showToast(error.message);
    }
  });
  root.querySelector("#library-search").addEventListener("input", renderLibraries);
  root.querySelector("#material-search").addEventListener("input", () => {
    applyMaterialFilters({ delayed: true });
  });
  ["#material-library-filter", "#material-kind-filter", "#material-availability-filter", "#material-authorization-filter"].forEach((selector) => {
    root.querySelector(selector).addEventListener("change", () => applyMaterialFilters());
  });
  root.querySelector("#material-filter-reset").addEventListener("click", () => {
    root.querySelector("#material-search").value = "";
    root.querySelector("#material-library-filter").value = "";
    root.querySelector("#material-kind-filter").value = "";
    root.querySelector("#material-availability-filter").value = "";
    root.querySelector("#material-authorization-filter").value = "";
    applyMaterialFilters();
    root.querySelector("#material-search").focus();
  });
  root.querySelector("[data-material-previous]").addEventListener("click", () => {
    state.materialFilters.offset = Math.max(
      0,
      state.materialFilters.offset - state.materialFilters.limit,
    );
    void loadMaterialIndex();
  });
  root.querySelector("[data-material-next]").addEventListener("click", () => {
    state.materialFilters.offset += state.materialFilters.limit;
    void loadMaterialIndex();
  });
  root.querySelector("[data-check-all-libraries]").addEventListener("click", async () => {
    const libraries = state.bootstrap.libraries || [];
    if (!libraries.length) {
      showToast("请先登记素材目录。");
      return;
    }
    for (const library of libraries) {
      await checkLibrary(library.libraryId);
    }
  });
  window.addEventListener("hashchange", routeFromHash);
}

async function start() {
  const saved = preferences.read();
  state.taskView = saved.taskView;
  app.classList.toggle("rail-off", saved.railCollapsed);
  root.querySelector("[data-toggle-rail]").setAttribute("aria-expanded", String(!saved.railCollapsed));
  setTheme(saved.theme);
  bindEvents();
  await initializeWorkbenchPage({
    renderStaticOptions: renderCreateOptions,
    loadBootstrap: () => api("/api/bootstrap"),
    renderConnected: (bootstrap) => {
      state.bootstrap = bootstrap;
      renderTaskCenter();
      renderServiceState();
      renderCreateOptions();
      renderLibraries();
      renderMaterialLibraryOptions();
      renderPackagingCatalog();
      renderSettings();
      routeFromHash();
    },
    renderFailure: (failure, error) => {
      const service = root.querySelector("#service-status");
      service.classList.add("error");
      service.lastElementChild.textContent = failure.statusLabel;
      root.querySelector("#heavy-status").textContent = "重任务 · 状态未知";
      state.bootstrap = { tasks: [], jobs: [] };
      renderTaskCenter();
      routeFromHash();
      showToast(
        failure.actionLabel +
        (error.diagnosticId ? "（诊断编号 " + error.diagnosticId + "）" : ""),
      );
    },
  });
}

start();
