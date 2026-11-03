# -*- coding: utf-8 -*-
"""
从视频/音频识别出带时间戳的字幕，输出 SRT 到 public/scripts/<name>.srt。
用 faster-whisper（无需 torch，内存占用小）。中文口播默认 language=zh。

用法:
  python scripts/transcribe.py public/digital_human/xinjiang.mp4 [模型大小]
  模型大小可选: tiny / base / small / medium（默认 small；内存紧就用 base 或 tiny）
"""
import sys
import os
import argparse

def fmt_ts(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="识别视频语音并输出 SRT 与词级时间戳"
    )
    parser.add_argument("video", help="输入视频或音频路径")
    parser.add_argument("model_size", nargs="?", default="small")
    parser.add_argument(
        "--srt-output", "--output-srt", dest="srt_output"
    )
    parser.add_argument(
        "--words-output", "--output-words", dest="words_output"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    video = args.video
    model_size = args.model_size
    if not os.path.exists(video):
        print(f"找不到文件: {video}")
        return 1

    base = os.path.splitext(os.path.basename(video))[0]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_srt = args.srt_output or os.path.join(
        root, "public", "scripts", f"{base}.srt"
    )
    out_words = args.words_output or os.path.join(
        root, "public", "scripts", f"{base}.words.json"
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_srt)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(out_words)), exist_ok=True)

    from faster_whisper import WhisperModel

    print(f"加载模型 {model_size} (int8) ...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    print(f"识别 {video} ...")
    segments, info = model.transcribe(
        video,
        language="zh",
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=300),
        beam_size=5,
        word_timestamps=True,
    )

    import json
    lines = []
    words = []  # 词级时间戳，供拆短句对齐用
    n = 0
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        n += 1
        lines.append(f"{n}\n{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}\n{text}\n")
        if seg.words:
            for w in seg.words:
                words.append({"w": w.word, "start": round(w.start, 3), "end": round(w.end, 3)})
        print(f"  [{fmt_ts(seg.start)} -> {fmt_ts(seg.end)}] {text}")

    with open(out_srt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(out_words, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=0)
    print(f"\n完成：{n} 段字幕 → {out_srt}")
    print(f"      {len(words)} 个词级时间戳 → {out_words}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
