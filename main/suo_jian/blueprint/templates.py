"""Versioned editing recipes; deliberately separate from Cutroom packaging."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class TemplateStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, reference: dict, name: str, source_job: str) -> dict:
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("模板名称请填写 1～80 个字。")
        recipe = {key: reference[key] for key in (
            "source", "shots", "subtitle_style", "subtitle_production", "subtitles",
            "subtitle_analysis", "text_templates", "audio_analysis", "tts_analysis",
            "sticker_analysis", "creative_elements", "creative_analysis", "styles", "edit_slots",
            "stickers", "sound_effects", "timing", "onscreen_text") if key in reference}
        value = {"schema_version": "suojian-edit-template-1", "name": name,
                 "source_job": source_job, "recipe": recipe,
                 "analysis_digest": digest(reference),
                 "cutroom_packaging_template_id": None,
                 "limitations": ["参考成片分析不是剪映原工程；字体、花字和音色可能只是候选。",
                                  "Cutroom 包装模板只保存包装组合，不包含完整剪辑结构。"]}
        value["template_id"] = "st-" + digest(value)[:20]
        path = self.root / (value["template_id"] + ".json")
        if path.exists():
            return self.get(value["template_id"])
        value["created_at"] = datetime.now(timezone.utc).isoformat()
        write_json(path, value)
        return value

    def get(self, template_id: str) -> dict:
        if not re.fullmatch(r"st-[0-9a-f]{20}", template_id):
            raise ValueError("无效的剪法模板编号。")
        return json.loads((self.root / f"{template_id}.json").read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        result = []
        for path in sorted(self.root.glob("st-*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            result.append({key: value.get(key) for key in (
                "template_id", "name", "source_job", "created_at", "cutroom_packaging_template_id", "packaging_choices", "packaging_revision")})
        return result

    def link_packaging(self, template_id: str, packaging_id: str, *, choices=None, reference_policy=None) -> dict:
        value = self.get(template_id)
        # Preserve the previous revision before changing the active link.
        write_json(self.root / "revisions" / f"{template_id}-{digest(value)[:12]}.json", value)
        value["cutroom_packaging_template_id"] = packaging_id
        if choices is not None:
            value["packaging_choices"] = choices
        if reference_policy is not None:
            value["reference_policy"] = reference_policy
        value["packaging_revision"] = digest({"choices":choices,"reference_policy":reference_policy})[:16]
        value["schema_version"] = "suojian-edit-template-2"
        value["limitations"] = ["字体与运镜识别为近似；复杂装饰在剪映中以独立素材呈现。"]
        write_json(self.root / f"{template_id}.json", value)
        return value
