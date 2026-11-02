const PUBLIC_KEYS = Object.freeze([
  "capability",
  "provider",
  "model",
  "configured",
  "lastCheckedAt",
]);

export function toastDuration(kind = "info") {
  return kind === "error" ? 7000 : 4000;
}

const PROVIDER_PRESETS = Object.freeze({
  openai_compatible: Object.freeze({
    label: "OpenAI 兼容",
    defaultBaseUrl: "",
    showChannel: false,
    showTtsFields: false,
    supportsModelCatalog: true,
  }),
  volcengine_ark: Object.freeze({
    label: "火山方舟文本",
    defaultBaseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    showChannel: false,
    showTtsFields: false,
    supportsModelCatalog: true,
  }),
  volcengine_seedance: Object.freeze({
    label: "火山方舟视频生成",
    defaultBaseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    showChannel: false,
    showTtsFields: false,
    supportsModelCatalog: true,
  }),
  doubao_tts: Object.freeze({
    label: "豆包 TTS",
    defaultBaseUrl: "https://openspeech.bytedance.com/api/v1/tts",
    showChannel: false,
    showTtsFields: true,
    supportsModelCatalog: false,
    supportsVoiceCatalog: true,
    ttsResources: Object.freeze([
      Object.freeze({ id: "seed-tts-1.0", label: "豆包语音合成 1.0" }),
      Object.freeze({ id: "seed-tts-2.0", label: "豆包语音合成 2.0" }),
    ]),
  }),
  new_api: Object.freeze({
    label: "New API（高级兼容）",
    defaultBaseUrl: "http://127.0.0.1:3000",
    showChannel: true,
    showTtsFields: false,
    supportsModelCatalog: true,
  }),
});

export function providerPresetDefinition(presetId) {
  return PROVIDER_PRESETS[presetId] || PROVIDER_PRESETS.openai_compatible;
}

const PROFILE_NAME_CAPABILITIES = Object.freeze({
  planning: "文案策划",
  broll: "B-roll",
  tts: "配音",
});

const MODEL_ROLE_BY_CAPABILITY = Object.freeze({
  planning: "planning",
  copywriting: "planning",
  storyboard: "planning",
  packaging: "planning",
  general_text: "planning",
  broll: "broll",
  video_generation: "broll",
  tts: "tts",
});

export function modelRoleForCapability(capability) {
  const value = String(capability || "");
  return MODEL_ROLE_BY_CAPABILITY[value] || value;
}

export function providerAccountPresentation(account) {
  const preset = providerPresetDefinition(account?.preset || account?.provider);
  const name = String(account?.name || preset.label || "未命名供应商").trim();
  const detail = [
    preset.label,
    account?.baseUrl,
    account?.defaultModel ? "默认 " + account.defaultModel : null,
  ].filter(Boolean).join(" · ");
  return { name, detail };
}

export function providerSupportsCapability(account, capability) {
  const provider = String(account?.preset || account?.provider || "");
  const role = modelRoleForCapability(capability);
  if (provider === "doubao_tts") return role === "tts";
  if (provider === "volcengine_ark") return role === "planning";
  if (provider === "volcengine_seedance") return role === "broll";
  if (provider === "openai_compatible") {
    return ["planning", "broll", "tts"].includes(role);
  }
  return provider === "new_api" && ["planning", "broll", "tts"].includes(role);
}

export function voiceOptionPresentation(voice) {
  const label = String(voice?.displayName || "未命名音色").trim();
  const languages = Array.isArray(voice?.languages) ? voice.languages.filter(Boolean) : [];
  const styles = Array.isArray(voice?.styles) ? voice.styles.filter(Boolean) : [];
  const emotions = Array.isArray(voice?.emotions) ? voice.emotions.filter(Boolean) : [];
  const detail = [
    languages.join(" / "),
    String(voice?.gender || "").trim(),
    [...styles, ...emotions].join(" / "),
  ].filter(Boolean).join(" · ");
  return { label, detail };
}

export function suggestedModelProfileName(accountName, capability, model) {
  return [
    String(accountName || "").trim(),
    PROFILE_NAME_CAPABILITIES[capability] || String(capability || "").trim(),
    String(model || "").trim(),
  ].filter(Boolean).join("-");
}

export function normalizeCapabilities(capabilities) {
  return (capabilities || []).map((item) =>
    Object.fromEntries(PUBLIC_KEYS.map((key) => [key, item[key] ?? null])),
  );
}

export function groupModelProfiles(profiles) {
  return (profiles || []).reduce((groups, profile) => {
    const capability = modelRoleForCapability(profile?.capability);
    if (!capability) return groups;
    if (!groups[capability]) groups[capability] = [];
    groups[capability].push(profile);
    return groups;
  }, {});
}

export function selectableProfiles(profiles, capability) {
  return (profiles || []).filter((profile) =>
    modelRoleForCapability(profile?.capability) === modelRoleForCapability(capability)
      && profile?.enabled !== false
      && profile?.availabilityStatus === "available",
  );
}

export function availabilityLabel(status) {
  return ({
    available: "可用",
    unavailable: "不可用",
    untested: "未检测",
  })[status] || "未知";
}

export const CAPABILITY_HELP = Object.freeze({
  planning: "统一用于文案分析改写、分镜规划和包装推荐；一次配置，整条策划链路复用。",
  broll: "仅在本地素材确实缺少画面时生成 B-roll；本地素材匹配无需配置模型。",
  tts: "信息流广告路线的配音、音色试听和正式合成。",
  copywriting: "分析或改写任务文案，只生成候选，采用前不修改锁定文案。",
  storyboard: "根据锁定文案规划镜头、画面搜索词和生成提示词。",
  packaging: "推荐标题、重点词、金句、字幕与其他包装组合。",
  general_text: "旧任务兼容能力，新任务不再使用。",
  video_generation: "只在确认本地素材确实缺少画面后按需生成 B-roll。",
  tts: "信息流广告路线的配音与音色试听；当前数字人路线不调用。",
  visual_matching: "用本地索引和可选视觉理解能力匹配镜头。",
});

export function personalPackagingStorageView(storage = {}) {
  const exists = storage?.exists === true;
  return {
    path: String(storage?.path || ""),
    locationLabel: storage?.isDefault === true ? "默认位置" : "自定义位置",
    statusLabel: exists ? "当前可用" : "当前离线，请重新选择目录",
    statusClass: exists ? "available" : "missing",
  };
}
