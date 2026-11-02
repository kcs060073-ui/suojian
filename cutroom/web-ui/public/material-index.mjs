const AVAILABILITY_LABELS = Object.freeze({
  available: "可用",
  offline: "目录离线",
  missing: "文件缺失",
});

const AUTHORIZATION_LABELS = Object.freeze({
  approved: "授权已核验",
  review_required: "授权待核验",
  unknown: "授权状态未知",
});

export function buildMaterialIndexQuery(filters = {}, taskId = null) {
  const params = new URLSearchParams();
  if (taskId) params.set("taskId", taskId);
  for (const [key, value] of [
    ["query", filters.query],
    ["libraryId", filters.libraryId],
    ["mediaKind", filters.mediaKind],
    ["availability", filters.availability],
    ["authorization", filters.authorization],
  ]) {
    if (String(value ?? "").trim()) params.set(key, String(value).trim());
  }
  params.set("limit", String(Number.isInteger(filters.limit) ? filters.limit : 24));
  params.set("offset", String(Number.isInteger(filters.offset) ? filters.offset : 0));
  return "/api/assets?" + params.toString();
}

export function buildMaterialIndexView(payload = {}) {
  const total = Number.isFinite(payload.total) ? payload.total : 0;
  const limit = Number.isFinite(payload.limit) && payload.limit > 0 ? payload.limit : 24;
  const offset = Number.isFinite(payload.offset) && payload.offset >= 0 ? payload.offset : 0;
  const items = (payload.items || []).map((item) => ({
    ...item,
    availabilityLabel: AVAILABILITY_LABELS[item.availability] || "状态未知",
    authorizationLabel: AUTHORIZATION_LABELS[item.authorizationStatus] || "授权状态未知",
    canPreview: Boolean(
      item.online
      && item.availability === "available"
      && item.previewUrl,
    ),
  }));
  return {
    items,
    total,
    limit,
    offset,
    countLabel: `共 ${total.toLocaleString("zh-CN")} 项素材`,
    hasPrevious: offset > 0,
    hasNext: offset + items.length < total,
    emptyMessage: total
      ? "当前页没有素材，请返回上一页。"
      : "没有符合条件的素材。可清除筛选，或先检查素材目录更新。",
  };
}

export function formatMaterialDuration(value) {
  if (!Number.isFinite(value) || value < 0) return "时长待探测";
  const minutes = Math.floor(value / 60);
  const seconds = Math.round(value % 60);
  return minutes ? `${minutes}分${String(seconds).padStart(2, "0")}秒` : `${seconds}秒`;
}

export function formatMaterialSize(value) {
  if (!Number.isFinite(value) || value < 0) return "大小未知";
  if (value >= 1073741824) return `${(value / 1073741824).toFixed(1)} GB`;
  if (value >= 1048576) return `${(value / 1048576).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}
