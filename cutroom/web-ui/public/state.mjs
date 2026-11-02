const STORAGE_KEYS = Object.freeze({
  theme: "cutroom-theme",
  taskView: "cutroom-task-view",
  railCollapsed: "cutroom-rail-collapsed",
});

const VALID_THEMES = new Set(["system", "light", "dark"]);
const VALID_TASK_VIEWS = new Set(["board", "list"]);

export const BOARD_LANES = Object.freeze([
  { key: "awaiting", label: "等待我处理" },
  { key: "automatic", label: "自动处理中" },
  { key: "queued", label: "排队" },
  { key: "problem", label: "阻塞 / 失败" },
  { key: "completed", label: "最近完成" },
]);

export const ROUTE_LABELS = Object.freeze({
  digital_human: "数字人混剪",
  park_video: "园区宣传片",
  ad_mix: "信息流广告混剪",
});

export const RUN_STATUS_LABELS = Object.freeze({
  draft: "待开始",
  ready: "准备就绪",
  awaiting_user: "等待我处理",
  queued: "排队中",
  running: "自动处理中",
  paused: "已暂停",
  blocked: "已阻塞",
  failed: "处理失败",
  interrupted: "等待恢复",
  completed: "已完成",
  cancelled: "已取消",
});

export function normalizeTask(task) {
  if (!task || typeof task !== "object" || !String(task.taskId || "").trim()) {
    throw new TypeError("taskId is required");
  }
  return Object.freeze({
    taskId: String(task.taskId),
    title: String(task.title || "未命名任务"),
    route: String(task.route || "digital_human"),
    currentStage: task.currentStage == null ? null : String(task.currentStage),
    runStatus: String(task.runStatus || "draft"),
    nextAction: task.nextAction == null ? null : String(task.nextAction),
    updatedAt: task.updatedAt == null ? null : String(task.updatedAt),
    gates: Object.freeze({ ...(task.gates || {}) }),
  });
}

export function laneForTask(task) {
  switch (task.runStatus) {
    case "running":
      return "automatic";
    case "queued":
      return "queued";
    case "blocked":
    case "failed":
    case "interrupted":
    case "cancelled":
      return "problem";
    case "completed":
      return "completed";
    default:
      return "awaiting";
  }
}

export function buildTaskCenter(tasks) {
  const list = (tasks || []).map(normalizeTask);
  const lanes = BOARD_LANES.map((lane) => ({
    ...lane,
    tasks: list.filter((task) => laneForTask(task) === lane.key),
  }));
  return { list, lanes };
}

export function taskDestination(task, _sourceView = "board") {
  return "#/tasks/" + encodeURIComponent(normalizeTask(task).taskId);
}

export function createPreferenceStore(storage) {
  if (!storage || typeof storage.getItem !== "function" || typeof storage.setItem !== "function") {
    throw new TypeError("storage is required");
  }
  const read = () => ({
    theme: VALID_THEMES.has(storage.getItem(STORAGE_KEYS.theme))
      ? storage.getItem(STORAGE_KEYS.theme)
      : "system",
    taskView: VALID_TASK_VIEWS.has(storage.getItem(STORAGE_KEYS.taskView))
      ? storage.getItem(STORAGE_KEYS.taskView)
      : "board",
    railCollapsed: storage.getItem(STORAGE_KEYS.railCollapsed) === "true",
  });
  return {
    read,
    setTheme(theme) {
      if (!VALID_THEMES.has(theme)) throw new RangeError("invalid theme");
      storage.setItem(STORAGE_KEYS.theme, theme);
    },
    setTaskView(view) {
      if (!VALID_TASK_VIEWS.has(view)) throw new RangeError("invalid task view");
      storage.setItem(STORAGE_KEYS.taskView, view);
    },
    setRailCollapsed(collapsed) {
      storage.setItem(STORAGE_KEYS.railCollapsed, String(Boolean(collapsed)));
    },
  };
}
