"""Shared source/output time arithmetic. Internal timestamps are integer microseconds."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from typing import Iterable

MICROSECONDS = 1_000_000


class TimelineContractError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _integer(value, name: str, *, positive=False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise TimelineContractError("INVALID_TIME", f"{name}必须是{'正' if positive else '非负'}整数")
    return value


def seconds_to_us(value: int | float | str | Decimal) -> int:
    """The only seconds boundary conversion; never turn unknown duration into zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise TimelineContractError("INVALID_TIME", "时间必须是有限秒数")
    try:
        seconds = Decimal(str(value))
        if not seconds.is_finite() or seconds < 0:
            raise InvalidOperation
        return int((seconds * MICROSECONDS).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        raise TimelineContractError("INVALID_TIME", "时间必须是有限非负秒数") from None


@dataclass(frozen=True)
class TimeWindow:
    """A half-open interval [start, end), on one explicitly chosen time axis."""
    start_us: int
    end_us: int

    def __post_init__(self):
        _integer(self.start_us, "开始时间")
        _integer(self.end_us, "结束时间")
        if self.end_us <= self.start_us:
            raise TimelineContractError("INVALID_TIME_WINDOW", "结束时间必须晚于开始时间")

    @property
    def duration_us(self):
        return self.end_us - self.start_us


@dataclass(frozen=True)
class PlaybackRate:
    numerator: int
    denominator: int

    def __post_init__(self):
        _integer(self.numerator, "速度分子", positive=True)
        _integer(self.denominator, "速度分母", positive=True)

    def output_us(self, source_us: int) -> int:
        _integer(source_us, "源时间")
        return _round_fraction(Fraction(source_us * self.denominator, self.numerator))


def _round_fraction(value: Fraction) -> int:
    # Non-negative rational half-up. No binary float or banker's rounding.
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def output_window(source: TimeWindow, rate: PlaybackRate) -> TimeWindow:
    # Map absolute boundaries, not rounded durations; adjacent shots stay adjacent.
    return TimeWindow(rate.output_us(source.start_us), rate.output_us(source.end_us))


def validate_source_window(window: TimeWindow | None, *, media_kind: str,
                           duration_us: int | None, allowed: TimeWindow | None = None):
    if media_kind == "image":
        if window is not None:
            raise TimelineContractError("IMAGE_HAS_SOURCE_WINDOW", "照片只有展示时间，没有源视频入出点")
        return
    if media_kind not in {"video", "audio"}:
        raise TimelineContractError("UNSUPPORTED_MEDIA_KIND", "无法确认素材类型")
    if window is None:
        raise TimelineContractError("SOURCE_WINDOW_REQUIRED", "视频和声音必须指定源区间")
    if duration_us is None:
        raise TimelineContractError("SOURCE_DURATION_UNKNOWN", "需先测量源素材实际时长")
    _integer(duration_us, "源时长", positive=True)
    if window.end_us > duration_us:
        raise TimelineContractError("SOURCE_OUT_OF_BOUNDS", "取用区间超过源素材时长")
    if allowed is not None and (window.start_us < allowed.start_us or window.end_us > allowed.end_us):
        raise TimelineContractError("SOURCE_OUT_OF_BOUNDS", "取用区间超过本次允许的素材片段")


def validate_coverage(windows: Iterable[TimeWindow], expected: TimeWindow):
    """Validate ordered output slots, before separately compiling transition handles."""
    cursor = expected.start_us
    found = False
    for window in windows:
        found = True
        if window.start_us != cursor or window.end_us > expected.end_us:
            raise TimelineContractError("TIMELINE_COVERAGE_MISMATCH", "镜头区间有缺口、重复或越界")
        cursor = window.end_us
    if not found:
        raise TimelineContractError("SEGMENT_WITHOUT_SLOT", "每段文案至少保留一个镜头安排")
    if cursor != expected.end_us:
        raise TimelineContractError("TIMELINE_COVERAGE_MISMATCH", "镜头未覆盖完整文案时间")


def frame_boundary(time_us: int, *, fps_num: int, fps_den: int, rounding="nearest") -> int:
    _integer(time_us, "时间")
    _integer(fps_num, "帧率分子", positive=True)
    _integer(fps_den, "帧率分母", positive=True)
    value = Fraction(time_us * fps_num, MICROSECONDS * fps_den)
    if rounding == "nearest":
        return _round_fraction(value)
    if rounding == "floor":
        return value.numerator // value.denominator
    if rounding == "ceil":
        return -(-value.numerator // value.denominator)
    raise TimelineContractError("INVALID_ROUNDING", "帧换算必须明确舍入方式")
