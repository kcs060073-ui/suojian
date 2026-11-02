from __future__ import annotations

import array
import math
import statistics
import subprocess
import tempfile
import wave
from pathlib import Path


def _estimate_pitch(video: Path) -> tuple[float | None, float]:
    """Estimate median fundamental frequency using dependency-free autocorrelation."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        wav_path = Path(handle.name)
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)], check=True)
        with wave.open(str(wav_path), "rb") as stream:
            rate = stream.getframerate()
            samples = array.array("h", stream.readframes(stream.getnframes()))
        frame_size, step = int(rate * 0.04), int(rate * 0.12)
        low_lag, high_lag = int(rate / 350), int(rate / 75)
        pitches: list[float] = []
        voiced_scores: list[float] = []
        for start in range(0, len(samples) - frame_size, step):
            frame = samples[start:start + frame_size]
            mean = sum(frame) / frame_size
            centered = [value - mean for value in frame]
            rms = math.sqrt(sum(value * value for value in centered) / frame_size)
            if rms < 450:
                continue
            energy = sum(value * value for value in centered) or 1
            best_score, best_lag = 0.0, 0
            for lag in range(low_lag, high_lag + 1, 2):
                score = sum(centered[i] * centered[i - lag] for i in range(lag, frame_size)) / energy
                if score > best_score:
                    best_score, best_lag = score, lag
            if best_lag and best_score > 0.32:
                pitches.append(rate / best_lag)
                voiced_scores.append(best_score)
        if not pitches:
            return None, 0.0
        return round(statistics.median(pitches), 1), round(min(0.7, statistics.mean(voiced_scores)), 2)
    finally:
        wav_path.unlink(missing_ok=True)


def analyze_tts(video: Path, captions: list[dict], duration: float) -> dict:
    """Return an explainable Jianying voice candidate and playback-speed estimate.

    This is acoustic/category matching, not voice-ID fingerprinting. Jianying voice
    labels vary by version, so candidates are deliberately returned as ranked labels.
    """
    try:
        pitch_hz, acoustic_confidence = _estimate_pitch(video)
    except Exception as exc:
        return {"available": False, "detected": None, "reason": str(exc), "requires_approval": True}
    chars = sum(len("".join(ch for ch in item.get("text", "") if ch.strip())) for item in captions)
    covered = sum(max(0.0, item.get("end", 0) - item.get("start", 0)) for item in captions) or duration
    chars_per_second = chars / covered if chars else None
    speed = round(max(0.8, min(1.2, (chars_per_second or 3.6) / 3.6)) * 10) / 10
    if pitch_hz is None:
        candidates = ["待试听确认", "沉稳解说", "自然旁白"]
        profile = "音高不可稳定提取，不能判断性别"
    elif pitch_hz < 215:
        candidates = ["待试听确认", "沉稳解说", "自然旁白"]
        profile = "混音与背景音乐会干扰性别判断，需在剪映试听确认"
    else:
        candidates = ["待试听确认", "自然旁白", "活力解说"]
        profile = "中高音候选，不能据此断定具体性别或音色"
    return {
        "available": True, "detected": acoustic_confidence >= 0.35,
        "match_type": "nearest_category_not_exact_voice_id",
        "recommended_voice": candidates[0], "alternative_voices": candidates[1:],
        "speed_estimate": speed, "pitch_hz_estimate": pitch_hz,
        "voice_profile": profile, "chars_per_second": round(chars_per_second, 2) if chars_per_second else None,
        "confidence": round(min(0.82, 0.35 + acoustic_confidence * 0.5 + (0.1 if chars else 0)), 2),
        "version_note": "剪映音色名称会随版本变化，请在当前音色库试听候选。",
        "requires_approval": True,
    }
