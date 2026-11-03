"""Frozen preview output geometry shared by identity and both render routes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreviewOutputProfile:
    profile_id: str
    width: int
    height: int
    crf: int = 28

    def render_fields(self) -> dict:
        return {"width": self.width, "height": self.height, "crf": self.crf}


def packaging_preview_output(task_type: str, aspect_ratio: str | None = None) -> PreviewOutputProfile:
    """Keep preview orientation bound to the route's locked output orientation."""
    if task_type == "digital_human":
        return PreviewOutputProfile("portrait-540p-v1", 540, 960)
    if task_type == "ad_mix":
        if aspect_ratio == "9:16":
            return PreviewOutputProfile("portrait-540p-v1", 540, 960)
        if aspect_ratio == "16:9":
            return PreviewOutputProfile("landscape-540p-v1", 960, 540)
        raise ValueError("AD_ASPECT_RATIO_INVALID: 广告预览缺少已确认的 9:16 或 16:9 画幅")
    raise ValueError(f"UNSUPPORTED_ROUTE: 不支持包装预览的任务类型：{task_type}")
