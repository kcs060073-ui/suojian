from __future__ import annotations

import re
from dataclasses import dataclass

from .semantic_splitter import fit_block_count, semantic_split, visible_chars


ROLE_RULES = [
    ("cta", re.compile(r"咨询|联系|获取|扫码|详情|入驻|致电|预约|欢迎")),
    ("price", re.compile(r"租金|价格|单价|元|优惠|低至|免租")),
    ("area", re.compile(r"面积|平方米|平米|㎡|亩|万方|起租")),
    ("transportation", re.compile(r"地铁|高速|机场|高铁|交通|公里|分钟|通达")),
    ("policy", re.compile(r"政策|扶持|补贴|税收|奖励|免租期")),
    ("location", re.compile(r"位于|坐落|开发区|产业园|园区|新区|区|市|镇")),
    ("facility", re.compile(r"厂房|办公|宿舍|食堂|配套|层高|承重|装修|生产")),
]


def classify_role(text: str) -> str:
    for role, pattern in ROLE_RULES:
        if pattern.search(text):
            return role
    return "selling_point"


@dataclass
class ScriptParser:
    speech_chars_per_second: float = 4.5

    def parse(self, text: str, context: dict | None = None) -> list[dict]:
        context = context or {}
        slots = context.get("edit_slots") or []
        preferred = [int(s.get("subtitle", {}).get("preferred_chars", 12)) for s in slots]
        target_chars = round(sum(preferred) / len(preferred)) if preferred else 12
        max_chars = max(16, round(target_chars * 1.65))
        blocks = semantic_split(text, target_chars=target_chars, max_chars=max_chars)
        if context.get("adaptation") == "strict" and slots:
            blocks = fit_block_count(blocks, len(slots), max_chars=max_chars)
        return [
            {
                "id": f"N{index:02d}",
                "text": value,
                "semantic_role": classify_role(value),
                "chars": visible_chars(value),
                "estimated_speech_duration": round(max(0.6, visible_chars(value) / self.speech_chars_per_second), 2),
            }
            for index, value in enumerate(blocks, start=1)
        ]
