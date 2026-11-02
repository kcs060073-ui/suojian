from __future__ import annotations

from datetime import datetime, timezone

from suo_jian.script_parser import ScriptParser, SlotMatcher


def create_draft_blueprint(reference: dict, script_text: str = "", adaptation: str = "smart", keep_original: bool = False) -> dict:
    slots = reference.get("edit_slots", [])
    if keep_original:
        blocks = [
            {
                "id": f"N{index:02d}",
                "text": slot["subtitle"]["original_text"],
                "semantic_role": slot.get("semantic_role", "selling_point"),
                "chars": len(slot["subtitle"]["original_text"]),
                "estimated_speech_duration": round(slot["time"]["duration"], 2),
            }
            for index, slot in enumerate(slots, start=1)
        ]
        source_mode = "keep_reference"
    else:
        blocks = ScriptParser().parse(script_text, {"edit_slots": slots, "adaptation": adaptation})
        source_mode = "replace"
    matches = SlotMatcher().match(slots, blocks, adaptation)
    slot_map = {slot["slot_id"]: slot for slot in slots}
    timeline = []
    for match in matches:
        slot = slot_map[match["slot_id"]]
        timeline.append({
            "slot_id": match["slot_id"],
            "time": slot["time"],
            "video": slot["video"],
            "subtitle": {"text": match["replacement_text"], "style_id": slot["subtitle"]["style_id"]},
            "semantic_role": slot.get("semantic_role", "selling_point"),
            "fit": match,
        })
    return {
        "schema_version": "draft-0.1",
        "product": "溯剪 / SuoJian",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reference": reference.get("source", {}).get("file_name"),
        "reference_schema_version": reference.get("schema_version"),
        "script": {"mode": source_mode, "adaptation": adaptation, "text": script_text, "semantic_blocks": blocks},
        "styles": reference.get("styles", {}),
        "timeline": timeline,
        "draft_generation": {"status": "preview", "approved": False},
    }
