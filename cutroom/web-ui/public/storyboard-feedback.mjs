export function buildStoryboardFeedbackView(value = {}) {
  const selectedAssetId = value.selectedAssetId || null;
  const candidates = (value.candidates || []).map((candidate) => ({
    ...candidate,
    reason: candidate.reason || "系统未返回匹配理由",
    sourceDirectory: candidate.sourceDirectory || "来源目录未知",
    warnings: Array.isArray(candidate.warnings) ? candidate.warnings : [],
    selected: candidate.assetId === selectedAssetId,
    canPreview: Boolean(candidate.previewUrl && ["video", "image"].includes(candidate.mediaKind)),
  }));
  return {
    feedbackId: value.feedbackId || null,
    selectedAssetId,
    candidates,
    canApply: Boolean(
      value.feedbackId
      && selectedAssetId
      && candidates.some((candidate) => candidate.assetId === selectedAssetId),
    ),
    actions: [
      { key: "reject", label: "拒绝当前推荐" },
      { key: "rerank", label: "按要求重荐" },
      { key: "apply", label: "应用所选候选" },
    ],
    emptyMessage: value.feedbackId
      ? "没有找到符合纠正要求的本地在线素材。可修改要求后重试。"
      : "写明纠正要求后，系统只在本任务素材范围内本地重排。",
  };
}

export function requireStoryboardInstruction(value) {
  const instruction = String(value || "").trim();
  if (!instruction) throw new Error("请先写明拒绝原因或纠正要求。");
  return instruction;
}
