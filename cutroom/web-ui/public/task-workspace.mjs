import {
  WORKFLOW_STAGES,
  resolveWorkflowViewStage,
  workflowLabel,
} from "./workflow-view.mjs";
import { buildJobStatus } from "./job-status.mjs";

const GATE_LABELS = Object.freeze([
  ["brief_locked", "文案已锁定"],
  ["picture_lock", "画面已锁定"],
  ["packaging_confirmed", "包装已确认"],
  ["human_reviewed", "成片已审完"],
]);

const NEXT_LABELS = Object.freeze({
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

export const AI_ACTIONS = Object.freeze({
  rewrite_brief: {
    label: "用 AI 提议改写",
    capability: "文案分析与改写模型",
    inputs: ["最终口播文案", "视频用途", "目标时长"],
    impact: "只生成改写候选；人工采用后才会改变文案，采用后需重新锁定。",
    cost: "可能产生模型调用费用",
    candidateOnly: true,
    enabled: true,
  },
  recommend_storyboard: {
    label: "用 AI 推荐分镜",
    capability: "分镜规划模型",
    inputs: ["已锁定文案", "可用素材摘要", "视频类型"],
    impact: "只生成分镜候选；不会自动替换已确认画面。",
    cost: "可能产生模型调用费用",
    candidateOnly: true,
    enabled: true,
  },
  match_shot: {
    label: "重新匹配当前镜头",
    capability: "本地镜头匹配模型",
    inputs: ["当前旁白", "本次主素材", "允许使用的通用素材"],
    impact: "只返回当前镜头候选；不会改动其他镜头。",
    cost: "本机执行，不产生外部 API 费用",
    candidateOnly: true,
    enabled: true,
  },
  transcribe: {
    label: "重新识别口播",
    capability: "本地 ASR",
    inputs: ["禅镜最终 MP4"],
    impact: "生成带词级时间戳的识别结果；不直接覆盖最终口播文案。",
    cost: "本机执行，不产生外部 API 费用",
    candidateOnly: true,
    enabled: true,
  },
  synthesize_voice: {
    label: "试听合成声音",
    capability: "TTS",
    inputs: ["已确认文案", "选中的音色"],
    impact: "生成试听候选；确认包装前不会用于成片。",
    cost: "可能产生语音合成费用",
    candidateOnly: true,
    enabled: true,
  },
  generate_video: {
    label: "生成缺口画面",
    capability: "视频生成模型",
    inputs: ["缺口镜头描述", "时长", "画幅"],
    impact: "只生成待选素材；不会自动插入时间线。",
    cost: "付费生成，当前阶段禁用",
    candidateOnly: true,
    enabled: false,
  },
});

export function buildStoryboardActionState(detail) {
  const agentMode = detail?.task?.orchestration_mode === "agent";
  const waitingForAgent = agentMode && detail?.workflow?.current_stage === "ai_storyboard";
  return {
    label: waitingForAgent ? "等待 Agent 提交规划" : "用 AI 推荐分镜候选",
    allowPaidTextRecommendation: !agentMode,
    sourceLabel: agentMode ? "当前 Agent" : "CUTROOM 模型",
    taskId: detail?.task?.task_id || null,
  };
}

export function buildWorkspaceSummary(detail) {
  const workflow = detail?.workflow || {};
  const gates = workflow.gates || {};
  const nextAction = workflow.next_action || null;
  return {
    currentStage: workflow.current_stage || null,
    currentLabel: workflowLabel(workflow.current_stage),
    runStatus: workflow.run_status || null,
    completed: GATE_LABELS.filter(([key]) => Boolean(gates[key])).map(([, label]) => label),
    nextAction,
    nextLabel: NEXT_LABELS[nextAction] || "查看当前阶段结果",
  };
}

export function workspaceKind(stage) {
  if (["create_preflight", "brief_lock", "material_sync", "ai_storyboard"].includes(stage)) return "brief-storyboard";
  if (["rough_cut", "picture_order", "picture_lock"].includes(stage)) return "rough-cut";
  if (stage === "packaging_confirm") return "packaging";
  if (["fine_render", "auto_qa", "full_review"].includes(stage)) return "review";
  if (stage === "publish_package") return "publish";
  return "overview";
}

export function roughCutDescription(taskType) {
  if (taskType === "digital_human") {
    return "只能调整镜头级画面，不会切断或删除连续的数字人口播主轴。";
  }
  if (taskType === "ad_mix") {
    return "可以调整逐句镜头的画面、顺序和时间边界，不会改写已锁定的广告文案和配音。";
  }
  return "可以调整镜头级画面、顺序和时间边界，不会改写已锁定的任务文案。";
}

export function buildWorkflowViewContext(detail, requestedStageKey = null) {
  const workflow = detail?.workflow || {};
  const actualStageKey = workflow.current_stage || null;
  const viewingStageKey = resolveWorkflowViewStage(
    WORKFLOW_STAGES,
    workflow,
    requestedStageKey,
  );
  return {
    actualStageKey,
    actualStageLabel: workflowLabel(actualStageKey),
    viewingStageKey,
    viewingStageLabel: workflowLabel(viewingStageKey),
    workspace: workspaceKind(viewingStageKey),
    readOnly: Boolean(viewingStageKey && viewingStageKey !== actualStageKey),
  };
}

const RECOVERY_COPY = Object.freeze({
  queued: [
    "任务正在排队",
    "后台会按顺序处理；关闭浏览器不会丢失任务。",
  ],
  running: [
    "系统正在自动处理",
    "进度由本机服务持续保存；关闭浏览器后任务仍会继续。",
  ],
  paused: [
    "任务已暂停",
    "任务停在已保存的安全检查点，可以继续处理或取消本次任务。",
  ],
  interrupted: [
    "处理在中途停止",
    "可以从服务端保存的安全检查点继续，不会从头覆盖已完成结果。",
  ],
  failed: [
    "本次处理没有完成",
    "先查看服务端给出的下一动作；允许恢复时才会显示继续按钮。",
  ],
  cancelled: [
    "本次处理已取消",
    "已完成的阶段仍然保留，可以回到当前阶段核对后重新发起。",
  ],
});

export function buildRecoveryWorkspace(detail) {
  const runStatus = detail?.workflow?.run_status || null;
  const sources = [
    detail?.sources?.primary,
    ...(detail?.sources?.selected_generic || []),
    ...(detail?.sources?.default_generic || []),
  ].filter(Boolean);
  const offlineSource = sources.find((item) => item.online === false);
  if (offlineSource) {
    return {
      mode: "offline-source",
      runStatus: "blocked",
      title: "任务已安全暂停",
      description: "素材目录当前不可访问；已完成的决定和记录会保留，也不会静默替换成其他园区素材。",
      libraryId: offlineSource.library_id,
      libraryName: offlineSource.name,
      mountPath: offlineSource.mount_path,
      jobId: null,
      phase: "找不到素材目录：" + offlineSource.mount_path,
      progressLabel: "等待重新连接",
      queueLabel: null,
      canResume: false,
      canCancel: false,
      needsManualCheck: false,
    };
  }
  if (!Object.hasOwn(RECOVERY_COPY, runStatus)) return null;
  const jobs = Array.isArray(detail?.jobs) ? detail.jobs : [];
  const job = jobs.find((item) => item?.status === runStatus) || jobs[0] || null;
  const needsManualCheck = Boolean(job?.needsManualCheck);
  const jobStatus = buildJobStatus(job || {});
  const copy = RECOVERY_COPY[runStatus];
  const total = Number(job?.progress?.total || 0);
  const current = Number(job?.progress?.current || 0);
  return {
    runStatus,
    title: copy[0],
    description: needsManualCheck
      ? "供应商结果或付费状态未知时不会自动重试，先人工核对，避免重复扣费或覆盖结果。"
      : copy[1],
    jobId: job?.jobId || null,
    phase: job?.phase || "等待本机服务更新阶段",
    progressLabel: total > 0 ? String(current) + " / " + String(total) : "进度待服务端更新",
    queueLabel: job?.queuePosition ? "队列第 " + String(job.queuePosition) + " 位" : null,
    statusDetail: jobStatus.statusDetail,
    remainingLabel: jobStatus.remainingLabel,
    startLabel: jobStatus.startLabel,
    observedLabel: jobStatus.observedLabel,
    ariaLive: jobStatus.ariaLive,
    canPause: Boolean(job?.canPause),
    pauseRequested: Boolean(job?.pauseRequested),
    canResume: Boolean(job?.canResume),
    canCancel: Boolean(job?.canCancel),
    needsManualCheck,
    problem: job?.problem || null,
  };
}
