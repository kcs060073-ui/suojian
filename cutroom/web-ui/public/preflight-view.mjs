const DEFAULT_NOTICES = Object.freeze([
  "真实性：具体地点、项目、企业和数据必须与本次资料一致。",
  "隐私：人脸、车牌、二维码和联系方式需要按风险提示确认。",
  "授权：镜头、字体、音乐、音色和品牌素材必须具备可用授权。",
]);

function finite(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatSeconds(value) {
  const seconds = finite(value);
  if (seconds == null) return null;
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)} 分钟`;
  const hours = minutes / 60;
  return Number.isInteger(hours) ? `${hours} 小时` : `${hours.toFixed(1)} 小时`;
}

function estimateMetric(estimate, formatter = formatSeconds) {
  const value = estimate && typeof estimate === "object" ? estimate : {};
  const minimum = formatter(value.min);
  const maximum = formatter(value.max);
  if (value.status !== "estimated" && value.status !== "exact") {
    return {
      status: "unknown",
      value: "暂无法估算",
      detail: String(value.reason || "当前没有可靠依据"),
    };
  }
  const display = minimum && maximum && minimum !== maximum
    ? `${minimum}–${maximum}`
    : minimum || maximum;
  if (!display) {
    return { status: "unknown", value: "暂无法估算", detail: "估算范围无效" };
  }
  return {
    status: "estimated",
    value: display,
    detail: String(value.basis || "基于当前任务输入估算"),
  };
}

function formatBytes(value) {
  const bytes = finite(value);
  if (bytes == null || bytes < 0) return null;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = bytes;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  const digits = amount >= 10 || Number.isInteger(amount) ? 0 : 1;
  return `${amount.toFixed(digits)} ${units[index]}`;
}

function costMetric(estimate) {
  const value = estimate && typeof estimate === "object" ? estimate : {};
  const minimum = finite(value.min);
  const maximum = finite(value.max);
  if (value.status !== "estimated" && value.status !== "exact") {
    return {
      status: "unknown",
      value: "付费调用前确认",
      detail: String(value.reason || "当前费用依据不完整"),
    };
  }
  const unit = value.unit || "CNY";
  const prefix = unit === "CNY" || unit === "元" ? "¥" : `${unit} `;
  const range = minimum != null && maximum != null && minimum !== maximum
    ? `${minimum.toFixed(2)}–${maximum.toFixed(2)}`
    : (minimum ?? maximum)?.toFixed(2);
  return range == null
    ? { status: "unknown", value: "付费调用前确认", detail: "费用范围无效" }
    : { status: "estimated", value: `${prefix}${range}`, detail: String(value.basis || "基于已选远程步骤") };
}

function queueMetric(queue) {
  const value = queue && typeof queue === "object" ? queue : {};
  if (value.heavy_slot === "available") {
    return { status: "ready", value: "当前空闲", detail: "提交时仍会重新检查队列" };
  }
  if (value.heavy_slot === "busy") {
    const count = Number.isInteger(value.waiting_count) ? value.waiting_count : null;
    return {
      status: "busy",
      value: count == null ? "正在占用" : `正在占用 · 等待 ${count} 项`,
      detail: String(value.reason || "单机重任务按顺序执行"),
    };
  }
  return { status: "unknown", value: "队列状态待确认", detail: String(value.reason || "队列观测暂不可用") };
}

export function buildPreflightView(result = {}) {
  const estimates = result.estimates && typeof result.estimates === "object"
    ? result.estimates
    : {};
  const queue = result.queue && typeof result.queue === "object" ? result.queue : {};
  const diskValue = formatBytes(result.estimated_space_bytes);
  const confirmations = Array.isArray(result.confirmations)
    ? result.confirmations.map((item) => ({
      key: String(item?.key || ""),
      label: String(item?.label || item?.message || "人工确认"),
      detail: String(item?.description || item?.detail || "到达该阶段时由使用人确认"),
    }))
    : [];
  const notices = Array.isArray(result.safety_notices) && result.safety_notices.length
    ? result.safety_notices.map(String)
    : [...DEFAULT_NOTICES];
  const cost = estimates.api_cost || {};
  return {
    metrics: {
      machineTime: estimateMetric(estimates.machine_seconds),
      humanTime: estimateMetric(estimates.human_seconds),
      disk: diskValue == null
        ? { status: "unknown", value: "暂无法估算", detail: "没有可靠的文件大小依据" }
        : { status: "estimated", value: diskValue, detail: "预计新增本地空间" },
      apiCost: costMetric(cost),
      queue: queueMetric(queue),
      queueWait: estimateMetric(queue.estimated_wait_seconds),
    },
    confirmations,
    notices,
    costScope: {
      included: Array.isArray(cost.included_costs) ? cost.included_costs.map(String) : [],
      excluded: Array.isArray(cost.excluded_costs) ? cost.excluded_costs.map(String) : [],
    },
    observedAt: typeof queue.observed_at === "string" && queue.observed_at
      ? queue.observed_at
      : null,
  };
}
