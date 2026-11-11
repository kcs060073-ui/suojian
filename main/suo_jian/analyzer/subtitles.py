from __future__ import annotations

import os
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path


def _normalized(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text or "").lower()


def _speech_event(text):
    tags = {tag.lower() for tag in re.findall(r'<\|([^|]+)\|>', text)}
    if 'speech' in tags:return 'speech'
    if tags & {'nospeech', 'bgm', 'music'}:return 'non_speech'
    return None


def _normalize_asr_segments(raw: list[dict], *, milliseconds: bool = False) -> list[dict]:
    segments = []
    for item in raw:
        text = str(item.get("text") or item.get("sentence") or "").strip()
        event = _speech_event(text)
        # FunASR models such as SenseVoice prefix transcript text with task,
        # language, and emotion tags (for example, <|zh|><|NEUTRAL|>).
        text = re.sub(r"<\|[^|]*\|>", "", text).strip()
        if not text:
            continue
        start = item.get("start", item.get("start_time", 0))
        end = item.get("end", item.get("end_time", start))
        scale = 1000.0 if milliseconds else 1.0
        try:
            start, end = float(start) / scale, float(end) / scale
        except (TypeError, ValueError):
            continue
        segments.append({"start": round(max(0, start), 3), "end": round(max(start, end), 3),
                         "text": text, "source": "asr", "confidence": item.get("confidence"),
                         "speech_event": event, "no_speech_prob": item.get('no_speech_prob')})
    return segments


@lru_cache(maxsize=1)
def _funasr_model():
    # Keep both model weights and ModelScope's SDK config under this project.
    project_root = Path(__file__).resolve().parents[2]
    model_home = project_root / "model_cache" / "home"
    model_cache = project_root / "model_cache" / "hub"
    model_home.mkdir(parents=True, exist_ok=True)
    model_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MODELSCOPE_HOME", str(model_home))
    os.environ.setdefault("MODELSCOPE_CACHE", str(model_cache))

    from funasr import AutoModel

    device = os.getenv("SUOJIAN_ASR_DEVICE", "cpu")
    model_name = os.getenv("SUOJIAN_FUNASR_MODEL", "iic/SenseVoiceSmall")
    options = {"model": model_name, "device": device, "disable_update": True, "disable_log": True}
    vad_model = os.getenv("SUOJIAN_FUNASR_VAD", "fsmn-vad").strip()
    if vad_model:
        options["vad_model"] = vad_model
    # SenseVoice already emits punctuation; don't instantiate Paraformer's punc model.
    punc_model = os.getenv("SUOJIAN_FUNASR_PUNC", "").strip()
    if punc_model and "sensevoice" not in model_name.lower():
        options["punc_model"] = punc_model
    return AutoModel(**options)


def _transcribe_funasr(video: Path, output_dir: Path | None) -> list[dict]:
    ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
    temp_context = tempfile.TemporaryDirectory(prefix="suojian_asr_")
    try:
        wav = Path(temp_context.name) / "speech.wav"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                        "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                       check=True, capture_output=True, timeout=180)
        model = _funasr_model()
        hotwords = os.getenv("SUOJIAN_ASR_HOTWORDS", "").strip()
        result = model.generate(input=str(wav), batch_size_s=300, **({"hotword": hotwords} if hotwords else {}))
        if not result:
            return []
        first = result[0] if isinstance(result, list) else result
        info = first.get("sentence_info") or first.get("segments") or []
        if info:
            return _normalize_asr_segments(info, milliseconds=True)
        raw = str(first.get("text") or "")
        text = re.sub(r"<\|[^|]*\|>", "", raw).strip()
        return [{"start": 0.0, "end": 0.0, "text": text, "source": "asr", "confidence": None,
                 "speech_event": _speech_event(raw)}] if text else []
    finally:
        temp_context.cleanup()


def _transcribe_whisper(video: Path) -> list[dict]:
    import whisper

    model_name = os.getenv("SUOJIAN_WHISPER_MODEL", "base")
    model = whisper.load_model(model_name)
    result = model.transcribe(str(video), fp16=False, verbose=False, language="zh")
    return _normalize_asr_segments(result.get("segments", []))


def transcribe_if_available(video: Path, output_dir: Path | None = None) -> tuple[list[dict], str | None]:
    """Optional ASR with a switchable FunASR Chinese path and Whisper fallback."""
    backend = os.getenv("SUOJIAN_ASR_BACKEND", "funasr").lower()
    errors = []
    candidates = ["funasr", "whisper"] if backend == "auto" else [backend]
    if any(name not in {"funasr", "whisper"} for name in candidates):
        return [], f"未知 ASR 后端：{backend}。可选 auto / funasr / whisper。"
    for name in candidates:
        try:
            segments = _transcribe_funasr(video, output_dir) if name == "funasr" else _transcribe_whisper(video)
            if segments:
                return segments, None
            errors.append(f"{name} 未返回转写内容")
        except ImportError:
            errors.append(f"{name} 未安装")
        except Exception as exc:
            errors.append(f"{name} 运行失败：{exc}")
            if backend != "auto":
                break
    return [], "语音识别不可用：" + "；".join(errors)


def _rapid_ocr_frame(engine, frame) -> list[dict]:
    result = engine(frame)
    if result is None:
        return []
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None and isinstance(result, (tuple, list)) and len(result) >= 2:
        boxes, recs = result[0], result[1]
        if recs and isinstance(recs[0], (tuple, list)):
            texts = [entry[0] for entry in recs]
            scores = [entry[1] for entry in recs]
        else:
            texts = recs
            scores = result[2] if len(result) >= 3 else None
    if boxes is None or texts is None:
        return []
    entries = []
    for index, text in enumerate(texts):
        text = str(text or "").strip()
        if len(_normalized(text)) < 2:
            continue
        box = boxes[index]
        try:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            score = float(scores[index]) if scores is not None else 0.5
        except (TypeError, ValueError, IndexError):
            continue
        entries.append({"text": text, "confidence": max(0.0, min(score, 1.0)),
                        "bbox": {"x": round(min(xs), 4), "y": round(min(ys), 4),
                                 "width": round(max(xs) - min(xs), 4),
                                 "height": round(max(ys) - min(ys), 4)}})
    return entries


def _tesseract_frame(frame) -> list[dict]:
    import cv2
    import pytesseract

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    try:
        data = pytesseract.image_to_data(gray, lang="chi_sim+eng", config="--psm 11",
                                         output_type=pytesseract.Output.DICT)
    except Exception:
        # Many Windows installations have Tesseract but lack the Chinese traineddata.
        data = pytesseract.image_to_data(gray, lang="eng", config="--psm 11",
                                         output_type=pytesseract.Output.DICT)
    groups: dict[tuple, list[int]] = {}
    for i, word in enumerate(data.get("text", [])):
        if str(word).strip():
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            groups.setdefault(key, []).append(i)
    output = []
    for indices in groups.values():
        text = "".join(str(data["text"][i]).strip() for i in indices)
        if len(_normalized(text)) < 2:
            continue
        output.append({"text": text, "confidence": 0.35, "bbox": None})
    return output


def _merge_ocr_observations(observations: list[dict], duration: float) -> list[dict]:
    clusters: list[dict] = []
    for item in sorted(observations, key=lambda entry: (entry["start"], entry["text"])):
        norm = _normalized(item["text"])
        match = next((entry for entry in reversed(clusters)
                      if entry["norm"] == norm and item["start"] <= entry["end"] + 1.1
                      and item["start"] >= entry["start"] - 0.05), None)
        if match:
            match["end"] = min(duration, max(match["end"], item["end"]))
            match["confidence_sum"] += item["confidence"]
            match["samples"] += 1
            if item.get("bbox"):
                match["bbox"] = item["bbox"]
        else:
            clusters.append({"start": item["start"], "end": item["end"], "text": item["text"],
                             "norm": norm, "bbox": item.get("bbox"), "confidence_sum": item["confidence"],
                             "samples": 1})
    return [{"id": f"OCR{index:03d}", "start": round(item["start"], 3),
             "end": round(max(item["start"] + 0.2, item["end"]), 3), "text": item["text"],
             "kind": "onscreen_text", "source": "ocr",
             "confidence": round(item["confidence_sum"] / item["samples"], 3),
             "bbox": item["bbox"], "sample_count": item["samples"]}
            for index, item in enumerate(clusters, start=1)]


def detect_hard_subtitles(video: Path, duration: float, output_dir: Path | None = None,
                          *, sample_times=None, progress=None) -> tuple[list[dict], str | None]:
    """Read visible text from sampled frames; prefer RapidOCR, retain Tesseract fallback."""
    try:
        import cv2
    except ImportError:
        return [], "未安装 OpenCV，画面文字识别未执行。"
    engine_name = "rapidocr"
    engine = None
    try:
        from rapidocr import RapidOCR
        engine = RapidOCR()
    except ImportError:
        engine_name = "tesseract"
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            return [], "未安装 RapidOCR 或 Tesseract，画面文字识别为可选功能。"
    except Exception as exc:
        return [], f"RapidOCR 初始化失败：{exc}"

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return [], "视频无法打开，画面文字识别未执行。"
    observations = []
    # Full-resolution OCR on every 0.4 s frame is too expensive on CPU. A
    # 0.75 s cadence still captures typical captions while keeping analysis usable.
    interval = 0.75
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = min(int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or round(duration * fps)),
                           max(1, round(duration * fps)))
        frame_step = max(1, round(fps * interval))
        frame_indices = range(total_frames) if sample_times is None else sorted({max(0,min(total_frames-1,round(t*fps))) for t in sample_times})
        processed = 0
        for frame_index in frame_indices:
            if sample_times is not None:cap.set(cv2.CAP_PROP_POS_FRAMES,frame_index)
            ok, frame = cap.read()
            if not ok:
                break
            timestamp = frame_index / fps
            if sample_times is None and frame_index % frame_step:
                continue
            if ok:
                processed += 1
                if progress and (processed % 8 == 0 or frame_index + frame_step >= total_frames):
                    progress(min(1.0, (frame_index + frame_step) / max(1,total_frames)))
                height, width = frame.shape[:2]
                scale = min(1.0, 1280 / max(width, height))
                if scale < 1:
                    frame = cv2.resize(frame, (round(width * scale), round(height * scale)))
                try:
                    entries = _rapid_ocr_frame(engine, frame) if engine_name == "rapidocr" else _tesseract_frame(frame)
                except Exception:
                    entries = []
                frame_height, frame_width = frame.shape[:2]
                for entry in entries:
                    box = entry.get("bbox")
                    if box:
                        box = {"x": box["x"] / frame_width, "y": box["y"] / frame_height,
                               "width": box["width"] / frame_width, "height": box["height"] / frame_height}
                    observations.append({**entry, "bbox": box, "start": timestamp,
                                         "end": min(duration, timestamp + interval)})
    finally:
        cap.release()
    detected = _merge_ocr_observations(observations, duration)
    if not detected:
        return [], f"{engine_name} 未找到可信画面文字。"
    return detected, None
