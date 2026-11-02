from __future__ import annotations

import json
from pathlib import Path

from .subtitle_builder import add_subtitles
from .timeline_builder import add_reference_timeline


class JianyingBuilder:
    def build(self, reference: dict, draft_blueprint: dict, video_path, draft_root, draft_name="溯剪-相似草稿", user_data_path=None, jianying_install_dir=None, allow_replace=False) -> Path:
        if not draft_blueprint.get("draft_generation", {}).get("approved"):
            raise PermissionError("DraftBlueprint 尚未获得用户许可。")
        import pyJianYingDraft as draft

        video_path, draft_root = Path(video_path), Path(draft_root)
        draft_root.mkdir(parents=True, exist_ok=True)
        source = reference["source"]
        folder_kwargs = {}
        if user_data_path:
            folder_kwargs["user_data_path"] = str(user_data_path)
        if jianying_install_dir:
            folder_kwargs["content_codec"] = draft.JianyingDraftCryptoCodec(draft.DraftCryptoConfig(jy_install_dir=str(jianying_install_dir)))
        script = draft.DraftFolder(str(draft_root), **folder_kwargs).create_draft(
            draft_name, int(source["width"]), int(source["height"]), int(round(source["fps"])), allow_replace=allow_replace
        )
        output = draft_root / draft_name
        media_stats = add_reference_timeline(script, draft, reference, video_path, output)
        subtitle_count = add_subtitles(script, draft, draft_blueprint)
        script.save()
        draft_blueprint["draft_generation"].update({"status": "completed", "completed": True})
        manifest = {
            "generator": "溯剪 / SuoJian",
            "draft_name": draft_name,
            "source_video": str(video_path.resolve()),
            "video_segments": media_stats["video_segments"],
            "audio_tracks": media_stats["audio_tracks"],
            "audio_track_names": media_stats.get("audio_track_names", []),
            "editable_text_segments": subtitle_count,
            "canvas": [source["width"], source["height"]],
            "fps": source["fps"],
        }
        (output / "suojian_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return output
