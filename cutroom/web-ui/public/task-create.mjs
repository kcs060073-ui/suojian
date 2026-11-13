export const CREATE_FIELD_ORDER = Object.freeze([
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

export const ROUTE_OPTIONS = Object.freeze([
  {
    key: "digital_human",
    label: "数字人混剪",
    description: "以禅镜最终 MP4 和对应口播文案为主轴，自动匹配本地镜头。",
    available: true,
  },
  {
    key: "ad_mix",
    label: "信息流广告混剪",
    description: "文案拆句、配音、逐句配片和快节奏转场。",
    available: true,
  },
  {
    key: "park_video",
    label: "项目宣传片",
    description: "从项目实拍素材中选镜头并组织解说。",
    available: false,
  },
]);

export const SUBTYPE_OPTIONS = Object.freeze([
  { key: "zhaoshang", label: "城市矩阵 / 招商业务" },
  { key: "caishui", label: "财税业务" },
]);

const CREATE_STEPS = Object.freeze(["route", "input", "preflight", "create"]);
const TASK_DRAFT_STORAGE_KEY = "cutroom-create-task-draft-v1";

function normalizeDraft(value) {
  if (!value || typeof value !== "object") return null;
  const route = String(value.route || "digital_human");
  const shared = {
    route,
    orchestration_mode: value.orchestration_mode === "agent" ? "agent" : "standalone",
    title: String(value.title || ""),
    final_script: String(value.final_script || ""),
    primary_library_id: String(value.primary_library_id || ""),
    selected_generic_ids: Array.isArray(value.selected_generic_ids)
      ? value.selected_generic_ids.map(String)
      : [],
    include_default_generic: value.include_default_generic !== false,
    purpose: String(value.purpose || ""),
    target_duration_seconds: String(value.target_duration_seconds || ""),
    planning_profile_id: String(
      value.planning_profile_id
      || value.storyboard_profile_id
      || value.copywriting_profile_id
      || value.packaging_profile_id
      || "",
    ),
    broll_profile_id: String(value.broll_profile_id || value.video_generation_profile_id || ""),
  };
  if (route === "ad_mix") {
    return {
      ...shared,
      aspect_ratio: String(value.aspect_ratio || "9:16"),
      voice_mode: String(value.voice_mode || "ai_tts"),
      tts_profile_id: String(value.tts_profile_id || ""),
      tts_voice_id: String(value.tts_voice_id || ""),
    };
  }
  return {
    ...shared,
    subtype: String(value.subtype || "zhaoshang"),
    digital_human_mp4: String(value.digital_human_mp4 || ""),
  };
}

export function createTaskDraftStore(storage) {
  if (
    !storage ||
    typeof storage.getItem !== "function" ||
    typeof storage.setItem !== "function" ||
    typeof storage.removeItem !== "function"
  ) {
    throw new TypeError("storage is required");
  }
  return {
    read() {
      try {
        return normalizeDraft(JSON.parse(storage.getItem(TASK_DRAFT_STORAGE_KEY)));
      } catch {
        return null;
      }
    },
    save(value) {
      const draft = normalizeDraft(value);
      if (!draft) throw new TypeError("draft is required");
      storage.setItem(TASK_DRAFT_STORAGE_KEY, JSON.stringify(draft));
      return draft;
    },
    clear() {
      storage.removeItem(TASK_DRAFT_STORAGE_KEY);
    },
  };
}

function startupFailure(error) {
  if (["AUTH_REQUIRED", "AUTH_INVALID"].includes(error?.code)) {
    return {
      statusLabel: "本机授权已失效",
      actionLabel: "请从启动入口重新打开工作台",
    };
  }
  return {
    statusLabel: "本机服务连接失败",
    actionLabel: "请确认工作台仍在运行后刷新页面",
  };
}

export async function initializeWorkbenchPage({
  renderStaticOptions,
  loadBootstrap,
  renderConnected,
  renderFailure,
}) {
  renderStaticOptions();
  try {
    const bootstrap = await loadBootstrap();
    renderConnected(bootstrap);
    return { connected: true, bootstrap };
  } catch (error) {
    const failure = startupFailure(error);
    renderFailure(failure, error);
    return { connected: false, failure };
  }
}

export function createStepIndex(step) {
  const index = CREATE_STEPS.indexOf(step);
  if (index < 0) throw new Error("未知创建步骤");
  return index;
}

export function buildCreateRequest(values) {
  const route = String(values?.route || "");
  const orchestrationMode = values?.orchestration_mode === "agent" ? "agent" : "standalone";
  const routeOption = ROUTE_OPTIONS.find((item) => item.key === route);
  if (!routeOption?.available) {
    throw new Error("该视频类型尚未开放");
  }
  const modelProfileIds = {};
  if (orchestrationMode === "standalone") {
    modelProfileIds.planning = String(values.planning_profile_id || "");
  }
  if (values.broll_profile_id) {
    modelProfileIds.broll = String(values.broll_profile_id);
  }
  const shared = {
    route,
    orchestration_mode: orchestrationMode,
    title: String(values.title || "").trim(),
    final_script: String(values.final_script || "").trim(),
    primary_library_id: String(values.primary_library_id || ""),
    selected_generic_ids: [...(values.selected_generic_ids || [])],
    include_default_generic: Boolean(values.include_default_generic),
    purpose: String(values.purpose || "").trim(),
    target_duration_seconds: Number(values.target_duration_seconds || 0),
    model_profile_ids: modelProfileIds,
  };
  if (route === "ad_mix") {
    const ttsVoiceId = String(values.tts_voice_id || "").trim();
    if (!ttsVoiceId) {
      throw new Error("请选择本条视频音色");
    }
    return {
      ...shared,
      aspect_ratio: String(values.aspect_ratio || "9:16"),
      voice_mode: String(values.voice_mode || "ai_tts"),
      tts_voice_id: ttsVoiceId,
      model_profile_ids: {
        ...shared.model_profile_ids,
        tts: String(values.tts_profile_id || ""),
      },
    };
  }
  return {
    ...shared,
    subtype: String(values.subtype || ""),
    digital_human_mp4: String(values.digital_human_mp4 || "").trim(),
  };
}

export function preflightSummary(result) {
  const checks = Array.isArray(result?.checks) ? result.checks : [];
  return checks.map((check) => ({
    code: String(check.code || ""),
    message: String(check.message || check.label || "检查项"),
    level:
      check.status === "passed"
        ? "passed"
        : check.status === "warning" || check.severity === "warning"
          ? "warning"
          : "blocking",
  }));
}
