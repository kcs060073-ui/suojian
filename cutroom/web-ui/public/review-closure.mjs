const REWORK_CATEGORIES = new Set([
  "script", "material", "rhythm", "subtitle", "audio", "packaging", "technical",
]);

function candidateId(value) {
  return String(value?.candidateId || value?.candidate_id || "");
}

export function buildCandidateHistory(detail, selectedId = null) {
  const source = Array.isArray(detail?.candidateVersions) ? detail.candidateVersions : [];
  const currentId = candidateId(detail?.currentCandidate);
  const items = source.map((value) => {
    const id = candidateId(value);
    return { ...value, candidateId: id, readOnly: id !== currentId, isCurrent: id === currentId };
  });
  const selected = items.find((item) => item.candidateId === String(selectedId || ""))
    || items.find((item) => item.isCurrent) || items[0] || null;
  return { items, selected, current: items.find((item) => item.isCurrent) || null };
}

export function groupQaIssues(candidate) {
  const result = { blocking: [], warning: [], passed: [] };
  const issues = Array.isArray(candidate?.qa?.issues) ? candidate.qa.issues : [];
  for (const issue of issues) {
    if (issue?.ok === true) result.passed.push(issue);
    else if (issue?.severity === "blocking") result.blocking.push(issue);
    else result.warning.push(issue);
  }
  return result;
}

export function materialAuthorizationSetup(detail) {
  const authorization = detail?.materialAuthorization || {};
  const assets = Array.isArray(authorization.assetIds) ? authorization.assetIds : [];
  const visible = authorization.approved !== true && assets.length > 0;
  return {
    visible,
    label: visible ? `仅确认本片 ${assets.length} 个镜头素材可商用` : "",
    selfProducedLabel: visible ? `确认 ${assets.length} 个镜头素材为团队自摄并复用` : "",
  };
}

export function materialAuthorizationAction(detail, candidate) {
  const setup = materialAuthorizationSetup(detail);
  const blocked = Array.isArray(candidate?.qa?.issues) && candidate.qa.issues.some((issue) => (
    issue?.check === "material_authorization" && issue?.ok !== true
  ));
  const visible = Boolean(candidate?.isCurrent !== false && blocked && setup.visible);
  return {
    visible,
    label: visible ? setup.label : "",
    selfProducedLabel: visible ? setup.selfProducedLabel : "",
  };
}

export function buildReworkDraft(candidate, values = {}) {
  const id = candidateId(candidate);
  if (!id) throw new Error("REWORK_CANDIDATE_REQUIRED");
  const category = String(values.category || "").trim();
  if (!REWORK_CATEGORIES.has(category)) throw new Error("INVALID_REWORK_CATEGORY");
  const notes = String(values.notes || "").trim();
  if (!notes) throw new Error("REWORK_NOTES_REQUIRED");
  const draft = { candidateId: id, category, notes };
  if (values.positionSeconds !== undefined && values.positionSeconds !== null && values.positionSeconds !== "") {
    const position = Number(values.positionSeconds);
    if (!Number.isFinite(position) || position < 0) throw new Error("INVALID_REWORK_POSITION");
    draft.positionSeconds = position;
  }
  if (values.qaIssueRef) draft.qaIssueRef = String(values.qaIssueRef);
  if (category === "technical" && values.technicalScope) draft.technicalScope = String(values.technicalScope);
  return draft;
}

export function reviewGate(candidate, watchSession, workflow = {}) {
  if (!candidate) return { canStartWatch: false, canApprove: false, blockingReason: "NO_CANDIDATE" };
  if (candidate.qa?.blockingCount > 0 || candidate.status === "qa_blocked") {
    return { canStartWatch: false, canApprove: false, blockingReason: "QA_BLOCKED" };
  }
  if (candidate.readOnly || candidate.isCurrent === false) {
    return { canStartWatch: false, canApprove: false, blockingReason: "HISTORY_READ_ONLY" };
  }
  const completed = Boolean(
    (watchSession?.completedAt || watchSession?.completed_at)
    && (watchSession?.canApprove || watchSession?.can_approve),
  );
  const reviewable = !candidate.status || candidate.status === "awaiting_review";
  return {
    canStartWatch: reviewable && !watchSession,
    canApprove: reviewable && completed,
    blockingReason: completed ? null : "FULL_WATCH_REQUIRED",
  };
}

export function reviewRenderAction(detail, currentCandidate) {
  const stage = detail?.workflow?.current_stage;
  if (stage === "fine_render") {
    return {
      visible: true,
      label: currentCandidate ? "生成新候选并自动质检" : "开始精剪并自动质检",
    };
  }
  if (stage === "auto_qa" && currentCandidate?.status === "qa_blocked") {
    return { visible: true, label: "重试自动 QA" };
  }
  return { visible: false, label: "" };
}

export function buildPublishPackageView(value, currentCandidateId = null) {
  const item = value && typeof value === "object" ? value : {};
  const files = Array.isArray(item.files) ? item.files.map(String) : [];
  const complete = (item.packageComplete === true || item.package_complete === true) && files.length > 0;
  const packageStatus = String(item.packageStatus || item.package_status || "current");
  const packageCandidateId = String(item.candidateId || item.candidate_id || "");
  const current = packageStatus === "current"
    && (!currentCandidateId || packageCandidateId === String(currentCandidateId));
  const expired = packageStatus === "expired" || !current;
  return {
    id: String(item.recordId || item.record_id || item.package_id || ""),
    directory: String(item.packageDir || item.package_dir || item.package_path || ""),
    candidateId: packageCandidateId,
    candidateLabel: String(item.candidateLabel || item.candidate_label || "未绑定候选"),
    packageStatus,
    status: `${expired ? "已过期" : complete ? "完整" : "不完整"} · ${files.length} 个文件`,
    complete,
    current,
    expired,
    files,
    coverChoice: String(item.coverChoice || item.cover_choice || "candidate-1"),
    deliveryType: String(item.deliveryType || item.delivery_type || "standard"),
  };
}
