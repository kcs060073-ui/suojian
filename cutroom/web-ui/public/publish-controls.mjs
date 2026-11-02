const COVER_CHOICES = new Set([
  "candidate-1", "candidate-2", "candidate-3", "external", "pending",
]);
const DELIVERY_TYPES = new Set(["standard", "editable"]);

function clean(value) {
  return String(value ?? "").trim();
}

function normalizeTags(value) {
  const source = Array.isArray(value) ? value : String(value || "").split(/[,，]/u);
  return [...new Set(source.map(clean).filter(Boolean))];
}

export function normalizePublishDraft(value = {}) {
  const coverChoice = clean(value.coverChoice) || "candidate-1";
  const deliveryType = clean(value.deliveryType) || "standard";
  if (!COVER_CHOICES.has(coverChoice)) throw new Error("INVALID_COVER_CHOICE");
  if (!DELIVERY_TYPES.has(deliveryType)) throw new Error("INVALID_DELIVERY_TYPE");
  const result = {
    platform: clean(value.platform) || "视频号",
    title: clean(value.title),
    description: clean(value.description),
    tags: normalizeTags(value.tags),
    coverChoice,
    deliveryType,
  };
  if (coverChoice === "external") {
    const externalCoverPath = clean(value.externalCoverPath);
    if (!externalCoverPath) throw new Error("EXTERNAL_COVER_REQUIRED");
    result.externalCoverPath = externalCoverPath;
  }
  return result;
}

export function buildPublishFormValue(form) {
  const values = form instanceof FormData ? form : new FormData(form);
  return normalizePublishDraft(Object.fromEntries(values.entries()));
}

export function buildCoverCandidates(candidate = {}, selectedChoice = "candidate-1") {
  const duration = Number(candidate.duration);
  const mediaUrl = clean(candidate.mediaUrl);
  if (!Number.isFinite(duration) || duration <= 0 || !mediaUrl) return [];
  return [0.15, 0.5, 0.85].map((fraction, index) => {
    const choice = `candidate-${index + 1}`;
    return {
      choice,
      label: `候选 ${index + 1}`,
      fraction,
      time: Number((duration * fraction).toFixed(3)),
      mediaUrl,
      selected: choice === selectedChoice,
    };
  });
}
