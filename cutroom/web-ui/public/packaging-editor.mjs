function normalize(plan) {
  return {
    planId: plan.plan_id,
    name: plan.plan_name || "未命名方案",
    status: plan.status || "draft",
    itemCount: Number(plan.item_count || plan.items?.length || 0),
    source: plan.origin_type === "system_recommendation" ? "recommended" : "custom",
  };
}

export function buildPackagingWorkspace(plans, selectedPlanId = null, context = {}) {
  const normalized = (plans || []).map(normalize);
  const recommended = normalized.filter((item) => item.source === "recommended");
  const custom = normalized.filter((item) => item.source === "custom");
  const selected = normalized.find((item) => item.planId === selectedPlanId) || normalized[0] || null;
  return {
    recommended,
    custom,
    templates: [...(context.templates || [])],
    selected,
    preview: buildPackagingPreviewModel(context.previewContext || {}),
    slots: slotRows(context.items || [], context.catalog || {}),
    previewAction: "preview",
    confirmAction: "confirm",
  };
}

export function partitionRecommendedPlans(plans, selectedPlanId = null) {
  const visiblePlan = plans.find((plan) => plan.planId === selectedPlanId) || plans[0] || null;
  return {
    visible: visiblePlan ? [visiblePlan] : [],
    history: plans.filter((plan) => plan.planId !== visiblePlan?.planId),
  };
}

import {
  buildPackagingPreviewModel,
  slotRows,
} from "./packaging-preview.mjs";
