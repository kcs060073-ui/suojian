from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _track_kind(name: str) -> tuple[str, str]:
    lowered = name.lower()
    if "vocals" in lowered or "voice" in lowered:
        return "voice", "人声"
    if "instrumental" in lowered or "no_vocals" in lowered:
        return "background_music", "背景音乐"
    if "other" in lowered:
        return "sound_effect_candidate", "其他／音效候选"
    if "drums" in lowered or "bass" in lowered or "guitar" in lowered or "piano" in lowered:
        return "music_component", "背景音乐分量"
    return "unknown", "未分类音轨"


def separate_audio_stems(video: Path, output: Path, enabled: bool = False) -> tuple[dict, str | None]:
    """Optionally separate audio without making heavyweight model downloads implicit."""
    result = {
        "available": False,
        "status": "not_requested" if not enabled else "dependency_missing",
        "engine": "python-audio-separator",
        "model": os.getenv("SUOJIAN_SEPARATOR_MODEL", "UVR-MDX-NET-Inst_HQ_3.onnx"),
        "tracks": [],
        "exact_sound_effect_separation": False,
        "caveat": "通用分轨可提取人声和音乐分量；other 只能作为音效候选，不能视为纯音效。",
        "install_hint": "pip install audio-separator[cpu]",
    }
    if not enabled:
        return result, "音频分轨未启用（可用 --separate-audio 开启；首次运行会下载模型）。"
    try:
        from audio_separator.separator import Separator
    except Exception:
        return result, "未安装 audio-separator，暂不能生成人声／背景音乐分轨。"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        result["status"] = "ffmpeg_missing"
        return result, "找不到 FFmpeg，无法提取待分离音频。"
    stem_dir = output / "audio_stems"
    stem_dir.mkdir(parents=True, exist_ok=True)
    source = stem_dir / "source.wav"
    subprocess.run(
        [ffmpeg, "-y", "-i", str(video), "-vn", "-ac", "2", "-ar", "44100", str(source)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Reuse the already downloaded model and metadata across analysis jobs.
        shared_model_dir = Path(__file__).resolve().parents[3] / "outputs" / "suo_jian_1_analysis" / "audio_models"
        model_dir = shared_model_dir if (shared_model_dir / result["model"]).exists() else output / "audio_models"
        model_dir.mkdir(parents=True, exist_ok=True)
        separator = Separator(output_dir=str(stem_dir), output_format="WAV", model_file_dir=str(model_dir))
        separator.load_model(result["model"])
        generated = separator.separate(str(source))
    except Exception as exc:
        result["status"] = "separation_failed"
        return result, f"音频分轨失败：{exc}"
    tracks = []
    for item in generated or []:
        path = Path(item)
        if not path.is_absolute():
            path = stem_dir / path
        kind, label = _track_kind(path.name)
        tracks.append({"kind": kind, "label": label, "path": str(path.resolve()), "filename": path.name})
    result.update({"available": bool(tracks), "status": "completed" if tracks else "empty", "tracks": tracks})
    return result, None if tracks else "分轨模型没有返回可试听文件。"
