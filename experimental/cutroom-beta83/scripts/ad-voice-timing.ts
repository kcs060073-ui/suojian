/** Keep the ad output timeline tied to the measured, re-encoded voice file. */
export function reconcileVoiceTimeline<T extends { start: number; end: number; dur: number }>(
  segments: readonly T[], measuredSeconds: number,
): { total: number; segments: T[] } {
  const last = segments.at(-1);
  if (!last || !Number.isFinite(measuredSeconds) || measuredSeconds <= 0) {
    throw new Error("AD_VOICE_DURATION_INVALID: 无法测量整条配音实际时长");
  }
  // An encoder frame can shift the tail slightly. A larger gap means the
  // audio and sentence timing are different artifacts and must be rechecked.
  if (!Number.isFinite(last.end) || Math.abs(measuredSeconds - last.end) > 0.1) {
    throw new Error("AD_VOICE_TIMING_MISMATCH: 整条配音与逐句时长不一致，不能截断或补时长");
  }
  const total = Number(measuredSeconds.toFixed(3));
  if (total <= last.start) {
    throw new Error("AD_VOICE_DURATION_INVALID: 整条配音未覆盖最后一句");
  }
  const final = { ...last, end: total, dur: Number((total - last.start).toFixed(3)) };
  return { total, segments: [...segments.slice(0, -1), final] };
}
