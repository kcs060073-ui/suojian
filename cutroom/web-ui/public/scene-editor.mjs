import { trimBounds } from "./rough-preview.mjs";

function pad(value) {
  return String(Math.max(0, Math.floor(Number(value) || 0))).padStart(2, "0");
}

export function formatTimecode(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(total / 60);
  return pad(minutes) + ":" + pad(total % 60);
}

export function selectScene(cards, cardId) {
  const card = (cards || []).find((item) => item.card_id === cardId);
  if (!card) return null;
  const zeroBased = (cards || []).some((item) => Number(item.idx) === 0);
  const index = Number(card.idx) + (zeroBased ? 1 : 0);
  const narrativeStart = Number(card.narrative_start);
  const narrativeEnd = Number(card.narrative_end);
  const explicitSourceStart = Number(card.enter_tc);
  const explicitSourceEnd = Number(card.exit_tc);
  const sourceStart = Number.isFinite(explicitSourceStart)
    ? explicitSourceStart
    : narrativeStart;
  const sourceEnd = Number.isFinite(explicitSourceEnd) && explicitSourceEnd > sourceStart
    ? explicitSourceEnd
    : narrativeEnd;
  return {
    id: card.card_id,
    index,
    narration: card.narrative_text || "暂无旁白文本",
    time: formatTimecode(card.narrative_start) + "–" + formatTimecode(card.narrative_end),
    source: card.relative_path || "尚未绑定画面素材",
    sourceType: card.source_type || "来源待确认",
    truthfulness: card.final_decision || "尚未人工确认真实性",
    suggestion: card.system_suggestion || "暂无系统建议",
    mixForm: card.mix_form || "未设置",
    mediaKind: card.media_kind || null,
    previewUrl: card.preview_url || null,
    thumbnailUrl: card.thumbnail_url || null,
    previewAvailable: card.preview_available === true,
    previewProblem: card.preview_problem || null,
    narrativeStart,
    narrativeEnd,
    sourceStart,
    sourceEnd,
    playbackPolicy: card.playback_policy || "once",
    replaceScope: "只替换第 " + index + " 镜头，不影响其他镜头",
  };
}

export function buildSceneEdit(action, selected, values = {}) {
  if (!selected?.id) throw new Error("请先选择一个镜头");
  if (!["replace", "reorder", "trim", "delete", "split"].includes(action)) {
    throw new Error("不支持的镜头操作");
  }
  const edit = { action };
  if (action !== "reorder") edit.card_id = selected.id;
  if (action === "split") {
    const playhead = Number(values.playhead);
    if (!Number.isFinite(playhead)) throw new Error("请指定有效的拆分位置");
    if (
      Number.isFinite(selected.narrativeStart)
      && Number.isFinite(selected.narrativeEnd)
      && (playhead <= selected.narrativeStart || playhead >= selected.narrativeEnd)
    ) {
      throw new Error("拆分位置必须位于当前镜头内部");
    }
    edit.playhead = playhead;
  }
  if (action === "trim") {
    const bounds = trimBounds(selected, values.enter_tc, values.exit_tc);
    edit.enter_tc = bounds.enterTc;
    edit.exit_tc = bounds.exitTc;
  }
  if (action === "replace") {
    edit.asset_id = values.asset_id;
    edit.segment_id = values.segment_id || null;
    edit.mix_form = values.mix_form || "broll全屏";
  }
  if (action === "reorder") edit.card_ids = [...(values.card_ids || [])];
  return edit;
}
