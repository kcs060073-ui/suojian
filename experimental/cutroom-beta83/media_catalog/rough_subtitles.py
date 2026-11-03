"""Local rough-cut subtitle cues, compiled by the existing final subtitle engine."""
import json
import math
from pathlib import Path

from .subtitles import compile_task_ass
from .transcript_reconciliation import _task_artifacts, project_transcript_state


def build_rough_subtitle_preview(project_root: Path, task: dict, expected_digest: str) -> dict:
    if task.get("task_type") != "digital_human":
        raise ValueError("ROUGH_SUBTITLE_ROUTE: 当前接口仅用于数字人原声字幕")
    root = Path(project_root).resolve()
    def require_current():
        state = project_transcript_state(root, task)
        if not state or state.get("status") not in {"aligned", "reviewed"} or not expected_digest or state.get("digest") != expected_digest:
            raise ValueError("ROUGH_SUBTITLE_STALE: 当前原稿或转写已变化，请先完成口播核对")
        return state
    review = require_current()
    artifacts = _task_artifacts(root, task)
    if artifacts is None:
        raise ValueError("ROUGH_SUBTITLE_STALE: 任务目录不可用")
    task_root = artifacts.parent
    script = (task_root / task["brief"]["final_script"]["relative_path"]).resolve()
    if not script.is_relative_to(task_root):
        raise ValueError("ROUGH_SUBTITLE_STALE: 原稿路径超出任务范围")
    # A rough cut has no approved packaging. Compile plain authoritative text;
    # later packaging still controls final typography and emphasis.
    semantics = artifacts / "rough-subtitle-semantics.json"
    value = '{"keywords":[],"punchLines":[]}\n'
    if not semantics.is_file():
        try:
            with semantics.open("x", encoding="utf-8") as output:
                output.write(value)
        except FileExistsError:
            pass
    preset_id = "sub-modern-clean-v1"
    binding = compile_task_ass(root, task_root, task["task_id"], preset_id,
                               script, artifacts / "transcript.words.json", semantics,
                               highlight_enabled=False, alignment_review=review)
    metadata = json.loads(Path(binding["metadata"]["path"]).read_text(encoding="utf-8"))
    cues = metadata.get("cues")
    if not isinstance(cues, list) or not cues or any(
        not isinstance(cue.get("text"), str) or not cue["text"] or
        not all(isinstance(cue.get(k), (int, float)) and math.isfinite(cue[k]) for k in ("start", "end")) or
        not 0 <= cue["start"] < cue["end"] for cue in cues
    ):
        raise ValueError("ROUGH_SUBTITLE_INVALID: 字幕引擎未返回有效分句时码")
    require_current()
    presets = json.loads((root / "assets/manifests/subtitle-presets.json").read_text(encoding="utf-8"))
    preset = next(item for item in presets["items"] if item["id"] == preset_id)
    return {"digest": expected_digest, "cues": [{"text": cue["text"].replace("\\N", "\n"), "start": cue["start"], "end": cue["end"]} for cue in cues],
            "style": {"font_size": preset["size"], "margin_v": preset["margin_v"]},
            "speed": metadata["speed"], "lead": metadata["lead"]}
