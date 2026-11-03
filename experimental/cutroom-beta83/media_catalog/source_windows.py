"""One interval rule for manual choices and automatic storyboard matching."""


def usable_source_ranges(start_us: int, end_us: int, marks) -> list[tuple[int, int]]:
    """Subtract current human exclusions, preserving disjoint half-open ranges."""
    ranges = [(start_us, end_us)] if end_us > start_us else []
    for mark in marks:
        if mark["usage_status"] != "不建议使用":
            continue
        left, right = mark["source_start_us"], mark["source_end_us"]
        remaining = []
        for start, end in ranges:
            if right <= start or left >= end:
                remaining.append((start, end))
            else:
                if left > start:
                    remaining.append((start, left))
                if right < end:
                    remaining.append((right, end))
        ranges = remaining
    return ranges


def assert_usable_source(connection, asset_id: str, start_us: int, end_us: int, *, image=False):
    from types import SimpleNamespace
    from .segment_marks import current_marks
    marks = current_marks(SimpleNamespace(connection=connection), asset_id).values()
    if image:
        rejected = any(mark["usage_status"] == "不建议使用" for mark in marks)
    else:
        rejected = usable_source_ranges(start_us, end_us, marks) != [(start_us, end_us)]
    if rejected:
        raise ValueError("SOURCE_USAGE_REJECTED: 所选来源含已标记不建议使用的画面，请调整源区间或先修订素材标记")
