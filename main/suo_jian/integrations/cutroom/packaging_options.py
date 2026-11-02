"""Validate task-local packaging selections against Cutroom's real catalog."""

PACKAGING_SLOTS = ("subtitle", "brand", "bgm", "sfx", "sticker", "title_card", "fancy_text")


def validate_packaging_choices(choices, catalog):
    if not isinstance(choices, dict) or set(choices) != set(PACKAGING_SLOTS):
        raise ValueError("请完整选择字幕、边框、音乐、音效和图形包装。")
    result = {}
    for slot in PACKAGING_SLOTS:
        value = choices[slot]
        if value is None and slot != "subtitle":
            result[slot] = None
        elif isinstance(value, str) and value in {row["id"] for row in catalog.get(slot, [])}:
            result[slot] = value
        else:
            raise ValueError("包装选项已变化或不可用，请刷新后重新选择。")
    return result
