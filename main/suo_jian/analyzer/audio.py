from __future__ import annotations

from pathlib import Path


def analyze_beats(video: Path) -> tuple[dict, str | None]:
    """Optional librosa analysis. It deliberately never makes the core demo fail."""
    try:
        import librosa
        import numpy as np
    except ImportError:
        return {"available": False, "bpm": None, "timestamps": []}, "librosa 未安装，节拍分析为可选功能。"
    try:
        waveform, sample_rate = librosa.load(str(video), sr=None, mono=True)
        tempo, beats = librosa.beat.beat_track(y=waveform, sr=sample_rate)
        timestamps = librosa.frames_to_time(beats, sr=sample_rate).tolist()
        tempo_value = float(np.asarray(tempo).reshape(-1)[0])
        return {"available": True, "bpm": round(tempo_value, 1), "timestamps": [round(x, 3) for x in timestamps]}, None
    except Exception as exc:
        return {"available": False, "bpm": None, "timestamps": []}, f"节拍分析不可用：{exc}"
