function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function cardNumber(card, fallbackIndex) {
  const explicit = finite(card?.index);
  if (explicit !== null && explicit > 0) return Math.floor(explicit);
  const stored = finite(card?.idx);
  if (stored !== null) return Math.floor(stored) + 1;
  return fallbackIndex + 1;
}

function safeProblem(value) {
  if (typeof value !== "string") return "素材无法预览";
  const text = value.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
  if (!text) return "素材无法预览";
  if (/(?:[A-Za-z]:[\\/]|\\\\|file:\/\/|\/(?:Users|home|Volumes)\/)/i.test(text)) {
    return "素材无法预览";
  }
  return text.slice(0, 160);
}

export function sceneAtPlayhead(cards, playhead) {
  const time = finite(playhead);
  if (time === null) return null;
  return (cards || []).find((card, index, all) => {
    const start = finite(card?.narrative_start);
    const end = finite(card?.narrative_end);
    if (start === null || end === null || end < start) return false;
    return time >= start && (time < end || (index === all.length - 1 && time === end));
  }) || null;
}

export function sourceTimeAtPlayhead(card, playhead) {
  const narrativeStart = finite(card?.narrative_start) ?? 0;
  const narrativeEnd = finite(card?.narrative_end) ?? narrativeStart;
  const time = finite(playhead) ?? narrativeStart;
  const offset = Math.max(0, time - narrativeStart);
  const resolvedStart = finite(card?.resolved_source_start);
  const resolvedEnd = finite(card?.resolved_source_end);
  const start = resolvedStart ?? finite(card?.enter_tc) ?? narrativeStart;
  const explicitEnd = resolvedEnd ?? finite(card?.exit_tc);
  const end = explicitEnd !== null && explicitEnd > start
    ? explicitEnd
    : narrativeEnd;
  const length = Math.max(0, end - start);
  if (!length) return start;
  if (card?.playback_policy === "loop") return start + (offset % length);
  return Math.min(start + offset, end);
}

export function advancePlayhead(cards, playhead, elapsedSeconds) {
  const list = Array.isArray(cards) ? cards : [];
  const duration = list.reduce((latest, card) => {
    const end = finite(card?.narrative_end);
    return end === null ? latest : Math.max(latest, end);
  }, 0);
  const current = finite(playhead) ?? 0;
  const elapsed = finite(elapsedSeconds) ?? 0;
  const next = Math.min(duration, Math.max(0, current + Math.max(0, elapsed)));
  const normalized = Math.round(next * 1e6) / 1e6;
  const scene = sceneAtPlayhead(list, normalized);
  return {
    playhead: normalized,
    sceneId: scene?.card_id || null,
    ended: normalized >= duration,
  };
}

export function moveCardId(cards, cardId, targetIndex) {
  const list = Array.isArray(cards) ? cards : [];
  const ids = list.map((card) => card?.card_id);
  if (!cardId || !ids.includes(cardId)) throw new Error("找不到要移动的镜头");
  const moving = list.find((card) => card?.card_id === cardId);
  if (moving?.fixed_anchor) throw new Error("固定主轴镜头不能移动");

  const visualIds = list
    .filter((card) => !card?.fixed_anchor)
    .map((card) => card.card_id)
    .filter((id) => id !== cardId);
  const requested = finite(targetIndex);
  if (requested === null) throw new Error("请指定有效的排序位置");
  const nonAnchorSlotsBefore = list
    .slice(0, Math.max(0, Math.min(list.length, Math.floor(requested))))
    .filter((card) => !card?.fixed_anchor).length;
  visualIds.splice(Math.min(nonAnchorSlotsBefore, visualIds.length), 0, cardId);

  let visualIndex = 0;
  return list.map((card) => (
    card?.fixed_anchor ? card.card_id : visualIds[visualIndex++]
  ));
}

export function trimBounds(card, enterTc, exitTc) {
  const enter = finite(enterTc);
  const exit = finite(exitTc);
  if (enter === null || exit === null) throw new Error("请输入有效的来源入点和出点");
  if (exit <= enter) throw new Error("来源出点必须晚于入点");

  const allowedStart = finite(card?.source_range_start)
    ?? finite(card?.segment_start)
    ?? finite(card?.available_start);
  const allowedEnd = finite(card?.source_range_end)
    ?? finite(card?.segment_end)
    ?? finite(card?.available_end);
  if (allowedStart !== null && enter < allowedStart) throw new Error("来源入点超出允许范围");
  if (allowedEnd !== null && exit > allowedEnd) throw new Error("来源出点超出允许范围");

  const narrativeStart = finite(card?.narrative_start) ?? finite(card?.narrativeStart);
  const narrativeEnd = finite(card?.narrative_end) ?? finite(card?.narrativeEnd);
  const narrativeDuration = narrativeStart !== null && narrativeEnd !== null
    ? Math.max(0, narrativeEnd - narrativeStart)
    : 0;
  const policy = card?.playback_policy || card?.playbackPolicy || "once";
  if (policy !== "loop" && exit - enter < narrativeDuration) {
    throw new Error("来源窗口不能短于镜头时长");
  }
  return { enterTc: enter, exitTc: exit };
}

export function pictureLockBlockers(cards, browserFailures = new Set()) {
  const supportedKinds = new Set(["image", "video"]);
  return (cards || []).flatMap((card, index) => {
    const label = `第 ${cardNumber(card, index)} 镜头`;
    if (!card?.asset_id) return [`${label}：尚未绑定画面素材`];
    if (!supportedKinds.has(card?.media_kind)) return [`${label}：不支持的媒体类型`];
    if (browserFailures?.has?.(card?.card_id)) {
      return [`${label}：浏览器无法加载当前素材`];
    }
    if (card?.preview_available !== true) {
      return [`${label}：${safeProblem(card?.preview_problem)}`];
    }
    return [];
  });
}

export function mediaVersionsForCards(cards) {
  return Object.fromEntries((cards || []).map((card) => [
    String(card?.card_id || ""),
    String(card?.preview_version || ""),
  ]).filter(([cardId, version]) => cardId && version));
}
