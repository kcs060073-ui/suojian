"""Measured local BGM timing, independent of speech segmentation."""
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

DURATION = 1067 / 30
LIBRARY = Path.home() / "CUTROOM/asset-packs/bgm/open-commercial-2026-09"
NAMES = {"machina": "Machina · 科技推进", "clear-skies": "Clear Skies · 清爽明亮",
         "computations-no-cello": "Computations · 理性科技", "inbound": "Inbound · 氛围铺陈",
         "machinery-of-the-stars": "Machinery of the Stars · 产业向上"}
ANCHORS = [("opening", 0), ("identity", 8.79), ("space", 14.916),
           ("area", 21.549), ("rent", 26.737)]
SOURCE_CUTS = [0, 209, 346, 399, 446, 588, 1067]


def options():
    return [{"id": key, "name": name} for key, name in NAMES.items() if (LIBRARY / (key + ".mp3")).is_file()]


def align_events(beats, max_shift=.18):
    events = []
    for name, anchor in ANCHORS:
        nearest = min(beats, key=lambda b: abs(b - anchor)) if beats else anchor
        use_beat = name != "opening" and bool(beats) and abs(nearest - anchor) <= max_shift
        target = nearest if use_beat else anchor
        events.append({"name": name, "speechAnchor": anchor, "frame": round(target * 30),
                       "offsetSeconds": round(target - anchor, 4), "synced": use_beat})
    return events


def align_shots(beat_frames):
    cuts = [0]
    for original in SOURCE_CUTS[1:-1]:
        nearest = min(beat_frames, key=lambda f: abs(f - original)) if beat_frames else original
        chosen = nearest if abs(nearest - original) <= 6 else original
        cuts.append(max(cuts[-1] + 24, min(chosen, original + 6)))
    cuts.append(1067)
    return [{"from": cuts[i], "duration": cuts[i + 1] - cuts[i],
             "sourceStart": start, "sourceEnd": SOURCE_CUTS[i + 1],
             "playbackRate": (SOURCE_CUTS[i + 1] - start) / (cuts[i + 1] - cuts[i]),
             "shiftFrames": cuts[i] - start} for i, start in enumerate(SOURCE_CUTS[:-1])]


def analyze_beats(wav):
    import librosa
    import numpy as np
    y, sr = librosa.load(str(wav), sr=22050, mono=True)
    if len(y) == 0 or float(np.max(np.abs(y))) < .0001:
        raise ValueError("音乐没有可检测的声音，请换一个音频文件")
    tempo, frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=256, trim=False)
    seconds = librosa.frames_to_time(frames, sr=sr, hop_length=256).tolist()
    seconds = [float(x) for x in seconds if 0 <= x < DURATION]
    if len(seconds) < 3:
        raise ValueError("没有检测到足够稳定的节拍，请换一首节奏更清楚的 BGM")
    intervals = np.diff(seconds)
    regularity = max(0, min(1, 1 - float(np.std(intervals) / np.mean(intervals))))
    beat_frames = sorted(set(round(x * 30) for x in seconds if round(x * 30) < 1067))
    return {"bpm": round(float(np.asarray(tempo).reshape(-1)[0]), 1),
            "beatFrames": beat_frames, "shots": align_shots(beat_frames),
            "beatSeconds": seconds, "regularity": round(regularity, 3),
            "events": align_events(seconds), "method": "librosa.beat_track; estimated beats, no downbeat claim"}


def prepare_music(source, name, work, public, offset=0, identity="upload"):
    source = Path(source)
    if not source.is_file() or source.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("BGM 文件不存在或超过 100 MB")
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
        "stream=codec_type:format=duration,format_name", "-of", "json", str(source)], timeout=30))
    if probe.get("format", {}).get("format_name") not in {"mp3", "wav", "mov,mp4,m4a,3gp,3g2,mj2", "ogg", "flac", "aac"}:
        raise ValueError("请上传普通音频文件，不支持播放列表")
    if not any(s.get("codec_type") == "audio" for s in probe.get("streams", [])):
        raise ValueError("所选文件没有音频轨道")
    length = float(probe.get("format", {}).get("duration", 0))
    if not 0 < length <= 1800:
        raise ValueError("请使用 30 分钟以内的 BGM")
    offset = offset if offset + DURATION <= length else 0
    key = uuid.uuid4().hex[:12]
    work.mkdir(parents=True, exist_ok=True)
    wav = work / (key + ".wav")
    asset = "music-" + key + ".m4a"
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-stream_loop", "-1", "-ss", str(offset),
            "-i", str(source), "-t", str(DURATION), "-vn", "-ac", "1", "-ar", "22050", str(wav)],
            check=True, capture_output=True, timeout=120)
        result = analyze_beats(wav)
        # Return an already ducked music stem so Player and Hypit hear the same mix.
        filters = ("[0:a]loudnorm=I=-18:TP=-2:LRA=7,volume=0.35[m];"
                   "[m][1:a]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=250,"
                   "afade=t=in:st=0:d=0.35,afade=t=out:st=34.2:d=1.36[out]")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-i", str(public / "narration.mp3"),
            "-filter_complex", filters, "-map", "[out]", "-t", str(DURATION), "-ar", "48000", "-ac", "2",
            "-c:a", "aac", "-b:a", "192k", str(public / asset)], check=True, capture_output=True, timeout=120)
        result.update(id=identity, name=name, asset=asset, sourceOffset=offset,
                      sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                      looped=length < DURATION, visualStyle="gold", gain=1.8,
                      source="uploaded" if identity == "upload" else "cutroom")
        return result
    finally:
        wav.unlink(missing_ok=True)
