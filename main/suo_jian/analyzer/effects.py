from __future__ import annotations

from pathlib import Path


def _label_for_event(centroid_hz: float, cut_distance: float | None) -> tuple[str, str]:
    if cut_distance is not None and cut_distance <= 0.12:
        return "transition_impact_candidate", "转场冲击音候选"
    if centroid_hz >= 3500:
        return "click_prompt_candidate", "点击/提示音候选"
    return "transient_effect_candidate", "瞬态音效候选"


def analyze_sound_effects(video: Path, cuts: list[float], max_events: int = 20) -> tuple[dict, str | None]:
    """Find likely sound-effect transients without claiming an exact CapCut asset name."""
    caveat = "成片音轨已混音，只能定位疑似音效并粗分类，无法可靠恢复剪映素材名称。"
    try:
        import librosa
        import numpy as np
    except ImportError:
        return {"available": False, "events": [], "method": "audio_onset_transient_heuristic", "exact_asset_names": False, "caveat": caveat}, "librosa 未安装，音效分析不可用。"
    try:
        waveform, sample_rate = librosa.load(str(video), sr=None, mono=True)
        onset_envelope = librosa.onset.onset_strength(y=waveform, sr=sample_rate)
        frames = librosa.onset.onset_detect(onset_envelope=onset_envelope, sr=sample_rate, backtrack=False)
        times = librosa.frames_to_time(frames, sr=sample_rate)
        if len(frames) == 0:
            return {"available": True, "events": [], "method": "audio_onset_transient_heuristic", "exact_asset_names": False, "caveat": caveat}, None
        threshold = float(np.percentile(onset_envelope[frames], 70))
        centroid = librosa.feature.spectral_centroid(y=waveform, sr=sample_rate)[0]
        candidates = []
        for frame, timestamp in zip(frames, times):
            strength = float(onset_envelope[min(int(frame), len(onset_envelope) - 1)])
            if strength < threshold:
                continue
            nearest = min((abs(float(timestamp) - cut) for cut in cuts), default=None)
            centroid_hz = float(centroid[min(int(frame), len(centroid) - 1)])
            kind, label = _label_for_event(centroid_hz, nearest)
            confidence = min(0.78, 0.38 + 0.22 * min(strength / max(threshold, 0.001), 1.5) + (0.12 if nearest is not None and nearest <= 0.12 else 0))
            candidates.append({
                "start": round(float(timestamp), 3),
                "end": round(float(timestamp) + 0.18, 3),
                "duration": 0.18,
                "type": kind,
                "label": label,
                "confidence": round(confidence, 3),
                "features": {
                    "onset_strength": round(strength, 3),
                    "spectral_centroid_hz": round(centroid_hz, 1),
                    "cut_distance_seconds": round(nearest, 3) if nearest is not None else None,
                },
                "caveat": caveat,
            })
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        candidates = sorted(candidates[:max_events], key=lambda item: item["start"])
        for index, item in enumerate(candidates, start=1):
            item["id"] = f"FX{index:02d}"
        return {"available": True, "events": candidates, "method": "audio_onset_transient_heuristic", "exact_asset_names": False, "caveat": caveat}, None
    except Exception as exc:
        return {"available": False, "events": [], "method": "audio_onset_transient_heuristic", "exact_asset_names": False, "caveat": caveat}, f"音效分析不可用：{exc}"
