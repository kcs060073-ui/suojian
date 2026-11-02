from __future__ import annotations


def _ratio_score(left: float, right: float) -> float:
    if left <= 0 or right <= 0:
        return 0.0
    return max(0.0, min(left, right) / max(left, right))


def _semantic_score(slot_role: str, block_role: str) -> float:
    if slot_role == block_role:
        return 1.0
    if "selling_point" in {slot_role, block_role}:
        return 0.72
    related = {
        frozenset(("price", "policy")),
        frozenset(("location", "transportation")),
        frozenset(("area", "facility")),
    }
    return 0.58 if frozenset((slot_role, block_role)) in related else 0.35


class SlotMatcher:
    weights = {"duration": 0.35, "chars": 0.25, "semantic": 0.25, "rhythm": 0.15}

    def match(self, edit_slots: list[dict], blocks: list[dict], mode: str = "smart") -> list[dict]:
        if not edit_slots:
            return []
        if not blocks:
            blocks = [{"id": "N00", "text": "", "semantic_role": "selling_point", "chars": 0, "estimated_speech_duration": 0.0}]
        if mode == "smart" and len(blocks) <= len(edit_slots):
            pairs = [
                (edit_slots[round(index * (len(edit_slots) - 1) / max(1, len(blocks) - 1))], block)
                for index, block in enumerate(blocks)
            ]
        elif mode == "smart":
            pairs = []
            for index, slot in enumerate(edit_slots):
                start = round(index * len(blocks) / len(edit_slots))
                end = round((index + 1) * len(blocks) / len(edit_slots))
                group = blocks[start:max(start + 1, end)]
                pairs.append((slot, {
                    "id": "+".join(item["id"] for item in group),
                    "text": "".join(item["text"] for item in group),
                    "semantic_role": group[0]["semantic_role"],
                    "chars": sum(item["chars"] for item in group),
                    "estimated_speech_duration": sum(item["estimated_speech_duration"] for item in group),
                }))
        else:
            empty = {"id": "N00", "text": "", "semantic_role": "selling_point", "chars": 0, "estimated_speech_duration": 0.0}
            pairs = [(slot, blocks[index] if index < len(blocks) else empty) for index, slot in enumerate(edit_slots)]

        matches: list[dict] = []
        for slot, block in pairs:
            time = slot["time"]
            subtitle = slot["subtitle"]
            duration_fit = _ratio_score(float(time["duration"]), float(block["estimated_speech_duration"]))
            char_fit = _ratio_score(float(subtitle["preferred_chars"]), max(1.0, float(block["chars"])))
            semantic_fit = _semantic_score(slot.get("semantic_role", "selling_point"), block["semantic_role"])
            rhythm_fit = 1.0 if slot.get("rhythm", {}).get("beat_sync") else 0.78
            score = (
                duration_fit * self.weights["duration"]
                + char_fit * self.weights["chars"]
                + semantic_fit * self.weights["semantic"]
                + rhythm_fit * self.weights["rhythm"]
            )
            warnings: list[str] = []
            if block["chars"] > subtitle["max_chars"]:
                warnings.append("字幕可能超出安全区域")
            if block["estimated_speech_duration"] > time["duration"] * 1.12:
                warnings.append(f"预计需要 {block['estimated_speech_duration']:.1f}s，当前 Slot 只有 {time['duration']:.1f}s")
            matches.append({
                "slot_id": slot["slot_id"],
                "block_id": block["id"],
                "original_text": subtitle["original_text"],
                "replacement_text": block["text"],
                "original_chars": len(subtitle["original_text"]),
                "replacement_chars": block["chars"],
                "preferred_chars": subtitle["preferred_chars"],
                "estimated_speech_duration": block["estimated_speech_duration"],
                "fit_score": round(score, 3),
                "score_breakdown": {
                    "duration_fit": round(duration_fit, 3),
                    "char_count_fit": round(char_fit, 3),
                    "semantic_fit": round(semantic_fit, 3),
                    "rhythm_fit": round(rhythm_fit, 3),
                },
                "warnings": warnings,
                "status": "warning" if warnings else "good",
            })
        return matches
