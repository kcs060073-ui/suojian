"""Build a longer draft timeline without changing the approved copy."""

from __future__ import annotations

from copy import deepcopy


def extend_for_speech(draft: dict, *, target_seconds: float = 36.0) -> dict:
    result = deepcopy(draft)
    items = result.get("timeline") or []
    if not items:
        raise ValueError("草稿没有字幕时间线。")
    if target_seconds <= 0 or target_seconds > 600:
        raise ValueError("目标时长必须在 0 到 600 秒之间。")
    original_duration = max(float(i["time"]["end"]) for i in items)
    if target_seconds <= original_duration:
        raise ValueError("延长后的时长必须大于参考视频。")
    estimates = [max(0.6, float(i.get("fit", {}).get("estimated_speech_duration", 0.6))) for i in items]
    pause = 0.4
    minimum = sum(estimates) + pause * (len(items) - 1)
    if target_seconds < minimum:
        raise ValueError(f"自然语速至少需要约 {minimum:.1f} 秒。")
    extra = (target_seconds - minimum) / len(items)
    position = 0.0
    for index, (item, estimate) in enumerate(zip(items, estimates)):
        start = round(position, 3)
        end = round(target_seconds if index == len(items) - 1 else position + estimate + extra, 3)
        item["time"] = {"start": start, "end": end, "duration": round(end - start, 3)}
        item["retimed_from_reference"] = True
        fit = item.get("fit")
        if isinstance(fit, dict):
            fit["warnings"] = [w for w in fit.get("warnings", []) if not w.startswith("预计需要")]
            fit["status"] = "warning" if fit["warnings"] else "good"
        position = end + pause
    result["retiming"] = {
        "method": "speech_estimate_with_pauses",
        "original_duration_seconds": original_duration,
        "target_duration_seconds": target_seconds,
        "inter_caption_pause_seconds": pause,
        "note": "按估计口播时长重排；实际 TTS 生成后仍须逐句复核。",
    }
    return result
