const STATUS_LABELS = Object.freeze({
  queued: "排队中",
  running: "处理中",
  paused: "已暂停",
  interrupted: "已中断",
  failed: "失败",
  cancelled: "已取消",
  completed: "已完成",
});

function durationLabel(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `约 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `约 ${hours} 小时 ${remainder} 分钟` : `约 ${hours} 小时`;
}

export function buildJobStatus(job = {}) {
  const status = String(job.status || "unknown");
  const pauseRequested = Boolean(job.pauseRequested);
  const estimated = job.estimateStatus === "estimated";
  const remaining = estimated ? durationLabel(job.estimatedRemainingSeconds) : null;
  const start = estimated && job.estimatedStartAt
    ? String(job.estimatedStartAt)
    : null;
  return {
    status,
    statusLabel: STATUS_LABELS[status] || "状态待确认",
    statusDetail: pauseRequested
      ? "已申请暂停，当前安全步骤结束后暂停。"
      : status === "paused"
        ? "任务停在已保存的安全检查点，可继续或取消。"
        : null,
    remainingLabel: remaining ? `预计剩余 ${remaining}` : "剩余时间待估算",
    startLabel: start ? `预计开始：${start}` : "开始时间待估算",
    observedLabel: job.observedAt ? `估算观测时间：${job.observedAt}` : null,
    ariaLive: "polite",
  };
}

export function buildServiceJobSummary(jobs = []) {
  const active = (Array.isArray(jobs) ? jobs : []).filter((job) =>
    ["queued", "running", "paused"].includes(String(job?.status || "")),
  );
  if (!active.length) return { label: "重任务 · 当前空闲", job: null, activeCount: 0 };
  const priority = { running: 0, paused: 1, queued: 2 };
  const job = [...active].sort((left, right) =>
    (priority[left.status] ?? 9) - (priority[right.status] ?? 9),
  )[0];
  const detail = buildJobStatus(job);
  const parts = ["重任务", detail.statusLabel, String(job.phase || "等待阶段更新")];
  if (job.status === "queued" && job.queuePosition) {
    parts.push(`队列第 ${job.queuePosition} 位`);
  }
  if (job.pauseRequested) parts.push("安全步骤结束后暂停");
  if (job.status === "queued") parts.push(detail.startLabel.replace("开始时间", "预计开始"));
  else parts.push(detail.remainingLabel);
  if (active.length > 1) parts.push(`另有 ${active.length - 1} 项`);
  return { label: parts.join(" · "), job, activeCount: active.length };
}
