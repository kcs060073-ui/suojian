from __future__ import annotations

from pathlib import Path
from suo_jian.analyzer import analyze_beats, analyze_creative, analyze_sound_effects, analyze_stickers, analyze_tts, detect_hard_subtitles, detect_scenes, read_metadata, separate_audio_stems, transcribe_if_available
from suo_jian.blueprint import build_blueprint, save_blueprint
from suo_jian.fusion import build_shots
from suo_jian.jianying import export_draft_skeleton
from suo_jian.ui.preview import write_preview
from suo_jian.analyzer.speech_text_policy import usable_speech, matching_captions, caption_probe_times


class AnalysisCheckpoints:
    """Source/option bound, atomic stage results; never cache unfinished work."""
    def __init__(self, video, output, options):
        import hashlib
        from suo_jian.blueprint.templates import digest
        with Path(video).open("rb") as handle:
            sha = hashlib.file_digest(handle,"sha256").hexdigest()
        self.root = Path(output)/"checkpoints"/digest({"sha":sha,"options":options,"version":1})[:20]
        self.output = Path(output).resolve()

    def stage(self, name, compute):
        import json
        from suo_jian.blueprint.templates import write_json
        path = self.root/(name+".json")
        try:
            saved=json.loads(path.read_text("utf-8"))
            if all(Path(item["path"]).is_file() and Path(item["path"]).stat().st_size==item["size"] for item in saved["artifacts"]):
                return saved["result"]
        except (OSError, ValueError, KeyError):
            pass
        result=compute()
        artifacts=[]
        def visit(value):
            if isinstance(value,dict):
                for item in value.values(): visit(item)
            elif isinstance(value,(list,tuple)):
                for item in value: visit(item)
            elif isinstance(value,(str,Path)) and len(str(value))<260:
                candidate=Path(value)
                try:
                    if candidate.is_absolute() and candidate.is_relative_to(self.output) and candidate.is_file():
                        artifacts.append({"path":str(candidate),"size":candidate.stat().st_size})
                except OSError:
                    pass
        visit(result)
        write_json(path,{"result":result,"artifacts":artifacts})
        return result


def analyze(video_path, output_dir, with_subtitles=False, with_stems=False, progress=None):
    def report(value, stage):
        if progress: progress(value, stage)
    video, output = Path(video_path), Path(output_dir)
    if not video.exists(): raise FileNotFoundError(f"找不到视频：{video}")
    checkpoints=AnalysisCheckpoints(video,output,{"subtitles":with_subtitles,"stems":with_stems})
    report(5, "读取视频信息")
    metadata = checkpoints.stage("metadata", lambda: read_metadata(video))
    report(16, "识别镜头切点")
    scenes, notes = checkpoints.stage("scenes", lambda: detect_scenes(video, metadata["duration"]))
    report(27, "分析节奏与画面运动")
    beats, note = checkpoints.stage("beats", lambda: analyze_beats(video))
    if note: notes.append(note)
    report(38, "识别人声与口播")
    raw_speech, note = checkpoints.stage("speech_v2", lambda: transcribe_if_available(video, output) if with_subtitles else ([], "语音转写未启用。"))
    if note: notes.append(note)
    speech_transcript = usable_speech(raw_speech)
    ocr_subtitles, ocr_note = [], None
    text_analysis = {'policy':'speech-and-captions-v1','status':'skipped','reason':'未检测到可靠人声'}
    if speech_transcript:
        report(43, "检查口播字幕")
        probe, probe_note = checkpoints.stage('caption_probe_v1', lambda: detect_hard_subtitles(
            video, metadata['duration'], output,
            sample_times=caption_probe_times(speech_transcript, metadata['duration'])))
        if matching_captions(probe, speech_transcript):
            report(45, '用字幕核对口播脚本')
            detected, ocr_note = checkpoints.stage('ocr_speech_captions_v1', lambda: detect_hard_subtitles(
                video, metadata['duration'], output,
                progress=lambda fraction:report(round(45+11*fraction,1),'字幕核对 '+str(round(100*fraction))+'%')))
            ocr_subtitles = matching_captions(detected, speech_transcript)
            text_analysis.update(status='completed',reason='人声与字幕匹配，已核对脚本')
        else:
            text_analysis['reason'] = probe_note or '有口播，未发现匹配的字幕候选'
    if ocr_note: notes.append(ocr_note)
    # Preserve all detected text, but only treat lower-screen, repeatedly visible
    # OCR as subtitle candidates. This avoids turning signs and title cards into
    # editable caption slots while keeping them available for review.
    subtitle_candidates = [item for item in ocr_subtitles
                           if item.get("bbox")
                           and item["bbox"].get("y", 0) + item["bbox"].get("height", 0) * 0.5 >= 0.42
                           and int(item.get("sample_count", 1)) >= 2]
    subtitles = subtitle_candidates or speech_transcript
    subtitle_conflicts = []
    if ocr_subtitles and speech_transcript:
        import difflib
        for visual in subtitle_candidates:
            box = visual.get("bbox") or {}
            if box and box.get("y", 0) + box.get("height", 0) / 2 < 0.48:
                continue
            candidates = [spoken for spoken in speech_transcript
                          if float(spoken["start"]) <= float(visual["end"]) + 1.25
                          and float(spoken["end"]) >= float(visual["start"]) - 1.25]
            if not candidates:
                continue
            spoken = min(candidates, key=lambda item: abs(float(item["start"]) - float(visual["start"])))
            visual_text = "".join(str(visual["text"]).split())
            spoken_text = "".join(str(spoken["text"]).split())
            if visual_text in spoken_text or spoken_text in visual_text:
                continue
            similarity = difflib.SequenceMatcher(None, visual_text, spoken_text).ratio()
            if similarity < 0.42:
                subtitle_conflicts.append({"time": round(float(visual["start"]), 3),
                                           "onscreen_text": visual["text"],
                                           "speech_text": spoken["text"],
                                           "similarity": round(similarity, 3),
                                           "confidence": min(float(visual.get("confidence") or 0),
                                                             float(spoken.get("confidence") or 0.5))})
    report(57, "解析配音与镜头运动")
    # Voice profile and speaking-rate estimates must be based on spoken words,
    # never OCR text from signs or baked-in graphics.
    tts_analysis = checkpoints.stage("tts", lambda: analyze_tts(video, speech_transcript, metadata["duration"]))
    shots, motion_notes = checkpoints.stage("shots", lambda: build_shots(video, scenes, beats["timestamps"]))
    notes.extend(motion_notes)
    cuts = [float(shot["end"]) for shot in shots[:-1]]
    report(70, "定位音效与贴纸")
    sound_effects, note = checkpoints.stage("sfx", lambda: analyze_sound_effects(video, cuts))
    if note: notes.append(note)
    audio_stems, note = checkpoints.stage("stems", lambda: separate_audio_stems(video, output, enabled=with_stems))
    if note: notes.append(note)
    stickers, note = checkpoints.stage("stickers", lambda: analyze_stickers(video, metadata["duration"]))
    if note: notes.append(note)
    report(84, "整理字幕样式与创意元素")
    creative_analysis, creative_notes = checkpoints.stage("creative", lambda: analyze_creative(video, metadata, beats, output))
    notes.extend(creative_notes)
    blueprint = build_blueprint(metadata, shots, beats, subtitles, tts_analysis, notes,
                                creative_analysis, sound_effects, stickers, audio_stems,
                                speech_transcript=speech_transcript,
                                subtitle_conflicts=subtitle_conflicts,
                                onscreen_text=ocr_subtitles)
    blueprint['text_analysis'] = text_analysis
    blueprint_path = save_blueprint(blueprint, output / "edit_blueprint.json")
    skeleton_path, note = export_draft_skeleton(blueprint, output / "jianying")
    blueprint["warnings"].append(note)
    save_blueprint(blueprint, blueprint_path)
    write_preview(blueprint, output / "preview.html", video)
    report(100, "分析完成")
    return blueprint, blueprint_path, skeleton_path
