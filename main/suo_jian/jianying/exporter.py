from __future__ import annotations

import json
from pathlib import Path


def export_draft_skeleton(blueprint, output: Path):
    """Interchange skeleton; adapt through pyJianYingDraft after version validation."""
    output.mkdir(parents=True, exist_ok=True)
    payload = {"name": "溯剪导出骨架", "source": blueprint["source"]["file_name"], "tracks": [{"type": "video", "segments": [{"start": s["start"], "duration": s["duration"], "motion": s["motion"], "transition_out": s["transition_out"]} for s in blueprint["shots"]]}]}
    file = output / "jianying_draft_skeleton.json"
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return file, "已生成剪映草稿时间线骨架；尚未伪装成可直接打开的剪映草稿。"
