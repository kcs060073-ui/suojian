export const WORKFLOW_STAGES = Object.freeze([
  { key: "create_preflight", label: "创建与预检" },
  { key: "brief_lock", label: "文案锁定" },
  { key: "material_sync", label: "素材增量同步" },
  { key: "ai_storyboard", label: "AI分镜推荐", ai: true },
  { key: "rough_cut", label: "粗剪" },
  { key: "picture_order", label: "调整画面顺序" },
  { key: "picture_lock", label: "画面锁定" },
  { key: "packaging_confirm", label: "包装方案确认", ai: true },
  { key: "fine_render", label: "精剪成片" },
  { key: "auto_qa", label: "自动质检" },
  { key: "full_review", label: "完整审片" },
  { key: "publish_package", label: "发布包", ai: true },
]);

export const WORKFLOW_PHASES = Object.freeze([
  { key: "prepare", label: "准备输入", start: 1, end: 4 },
  { key: "edit", label: "调整画面", start: 5, end: 7 },
  { key: "finish", label: "成片检查", start: 8, end: 11 },
  { key: "deliver", label: "交付发布", start: 12, end: 12 },
]);

const STAGE_GUIDANCE = Object.freeze({
  create_preflight: ["核对视频路线、文案和素材输入是否齐全。", "任务创建成功，输入已保存。"],
  brief_lock: ["通读本次最终文案，确认无误后锁定。", "文案锁定后才会继续分析。"],
  material_sync: ["核对本次主素材与补充目录，再同步新增或变化的素材。", "可用素材范围完成同步。"],
  ai_storyboard: ["查看分镜推荐，确认每段旁白准备使用什么画面。", "分镜候选生成，可进入粗剪检查。"],
  rough_cut: ["逐镜预览粗剪，先检查画面与旁白是否匹配。", "粗剪镜头已检查。"],
  picture_order: ["按视频叙事顺序调整镜头，必要时替换或裁剪。", "镜头顺序与时间点已确认。"],
  picture_lock: ["完整检查画面后锁定，后续修改会重新打开相关步骤。", "画面顺序已锁定。"],
  packaging_confirm: ["挑选本片包装方案，生成真实预览并完整观看。", "包装方案经观看后确认。"],
  fine_render: ["生成精剪候选片，确认成片版本与预览内容。", "候选片已生成并进入自动质检。"],
  auto_qa: ["先处理阻断项，再按时间点复核警告项。", "自动质检没有阻断项。"],
  full_review: ["从头到尾观看当前成片，确认声音、字幕与画面。", "当前版本已完成审片确认。"],
  publish_package: ["填写发布信息、选择封面并生成发布包。", "发布文件齐全，可下载交付。"],
});

export function buildStagePresentation(workflow, viewingStageKey = null) {
  const key = viewingStageKey || workflow?.current_stage;
  const number = WORKFLOW_STAGES.findIndex((stage) => stage.key === key) + 1;
  if (!number) return null;
  const phase = WORKFLOW_PHASES.find((item) => number >= item.start && number <= item.end);
  const [instruction, outcome] = STAGE_GUIDANCE[key];
  return { key, number, label: workflowLabel(key), phase: phase.label, instruction, outcome };
}

export function groupWorkflowSteps(steps) {
  return WORKFLOW_PHASES.map((phase) => {
    const members = steps.filter((step) => step.number >= phase.start && step.number <= phase.end);
    return {
      ...phase,
      steps: members,
      completed: members.filter((step) => step.stageStatus === "completed").length,
      active: members.some((step) => step.current),
      viewing: members.some((step) => step.viewing),
    };
  });
}

function stageMap(stages) {
  if (Array.isArray(stages)) {
    return Object.fromEntries(
      stages.map((stage) => [stage.stage_key || stage.key, stage]),
    );
  }
  return stages && typeof stages === "object" ? stages : {};
}

export function resolveWorkflowViewStage(definitions, workflow, requestedKey = null) {
  const states = stageMap(workflow?.stages);
  const currentKey = workflow?.current_stage || null;
  const requested = definitions.find((definition) => definition.key === requestedKey);
  if (
    requested
    && (requested.key === currentKey || states[requested.key]?.status === "completed")
  ) {
    return requested.key;
  }
  return definitions.some((definition) => definition.key === currentKey)
    ? currentKey
    : definitions.findLast((definition) => states[definition.key]?.status === "completed")?.key || null;
}

export function buildWorkflowSteps(definitions, workflow, viewingStageKey = null) {
  const states = stageMap(workflow?.stages);
  const resolvedView = resolveWorkflowViewStage(definitions, workflow, viewingStageKey);
  return definitions.map((definition, index) => ({
    ...definition,
    number: index + 1,
    stageStatus: states[definition.key]?.status || "not_started",
    runStatus:
      workflow?.current_stage === definition.key
        ? workflow?.run_status || null
        : null,
    current: workflow?.current_stage === definition.key,
    viewable:
      workflow?.current_stage === definition.key
      || states[definition.key]?.status === "completed",
    viewing: resolvedView === definition.key,
  }));
}

export function workflowLabel(key) {
  return WORKFLOW_STAGES.find((stage) => stage.key === key)?.label || "流程已完成";
}

export function renderWorkflow(
  container,
  workflow,
  { viewingStageKey = null, onSelect = null } = {},
) {
  const steps = buildWorkflowSteps(WORKFLOW_STAGES, workflow, viewingStageKey);
  const phases = groupWorkflowSteps(steps);
  const fragment = document.createDocumentFragment();
  const phaseBar = document.createElement("div");
  phaseBar.className = "flowphase-bar";
  const stepPanel = document.createElement("div");
  stepPanel.className = "flowphase-steps";
  const phaseControls = [];
  const showPhase = (phase) => {
    phaseControls.forEach((control) => {
      control.setAttribute("aria-pressed", String(control.dataset.phaseKey === phase.key));
    });
    const items = document.createDocumentFragment();
    for (const step of phase.steps) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = [
        "flowstep",
        step.stageStatus === "completed" ? "done" : "",
        step.current ? "current" : "",
        step.viewing ? "viewing" : "",
        step.ai ? "ai" : "",
      ].filter(Boolean).join(" ");
      item.dataset.stageKey = step.key;
      item.dataset.stageStatus = step.stageStatus;
      if (step.runStatus) item.dataset.runStatus = step.runStatus;
      item.disabled = !step.viewable;
      item.setAttribute("aria-pressed", String(step.viewing));
      if (step.current) item.setAttribute("aria-current", "step");
      item.setAttribute(
        "title",
        step.viewable
          ? (step.current ? "查看当前步骤" : "只读回看已完成步骤")
          : "完成前置步骤后开放",
      );
      if (step.viewable && typeof onSelect === "function") {
        item.addEventListener("click", () => onSelect(step.key));
      }
      const number = document.createElement("span");
      number.className = "flowstep-number";
      number.textContent = String(step.number);
      const text = document.createElement("span");
      text.textContent = step.label;
      item.append(number, text);
      items.append(item);
    }
    stepPanel.replaceChildren(items);
  };
  for (const phase of phases) {
    const control = document.createElement("button");
    control.type = "button";
    control.className = "flowphase";
    control.dataset.phaseKey = phase.key;
    const label = document.createElement("strong");
    label.textContent = phase.label;
    const progress = document.createElement("span");
    progress.textContent = `${phase.completed}/${phase.steps.length} 已完成`;
    control.append(label, progress);
    control.addEventListener("click", () => showPhase(phase));
    phaseControls.push(control);
    phaseBar.append(control);
  }
  showPhase(phases.find((phase) => phase.viewing) || phases.find((phase) => phase.active) || phases[0]);
  fragment.append(phaseBar, stepPanel);
  container.replaceChildren(fragment);
}
