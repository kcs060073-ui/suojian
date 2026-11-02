from __future__ import annotations

import json
from pathlib import Path

from suo_jian.blueprint import create_draft_blueprint, enrich_blueprint
from .builder import JianyingBuilder


def generate_editable_draft(blueprint_path, video_path, draft_root, draft_name="溯剪-相似草稿", user_data_path=None, jianying_install_dir=None, draft_blueprint_path=None, allow_replace=False):
    blueprint_path = Path(blueprint_path)
    reference = enrich_blueprint(json.loads(blueprint_path.read_text(encoding="utf-8")))
    if draft_blueprint_path:
        draft_blueprint = json.loads(Path(draft_blueprint_path).read_text(encoding="utf-8"))
    else:
        draft_blueprint = create_draft_blueprint(reference, keep_original=True)
    draft_blueprint["draft_generation"] = {"status": "approved", "approved": True}
    return JianyingBuilder().build(reference, draft_blueprint, video_path, draft_root, draft_name, user_data_path, jianying_install_dir, allow_replace)
