"""Register the curated 2026-09 open-commercial CUTROOM asset pack."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = ROOT / "asset-packs" / "open-commercial-2026-09"
IMPORTED_AT = "2026-09-10T18:00:00+08:00"
ALL_ROUTES = ["数字人混剪", "园区宣传", "信息流广告"]
RETIRED_IDS = {
    "bgm.json": {
        "bgm-uplifting-001",
        "bgm-epic-001",
        "bgm-lively-001",
    },
}


def sha256(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def item(kind: str, asset_id: str, name: str, source_url: str,
         source_license: str, **extra) -> dict:
    payload = {
        "id": asset_id,
        "kind": kind,
        "name": name,
        "source_url": source_url,
        "source_license": source_license,
        "license": "commercial-approved",
        "downloaded_at": IMPORTED_AT,
        **extra,
    }
    if payload.get("file"):
        payload["sha256"] = sha256(payload["file"])
    return payload


def bgm_items() -> list[dict]:
    source = "https://www.scottbuckley.com.au/library/"
    definitions = [
        ("bgm-sb-machinery-stars", "Machinery of the Stars · 科技向上",
         "machinery-of-the-stars.mp3", "machinery-of-the-stars/",
         "'Machinery of the Stars' by Scott Buckley - released under CC-BY 4.0. www.scottbuckley.com.au",
         ALL_ROUTES, 0.13, "科技、发展、产业升级"),
        ("bgm-sb-computations", "Computations · 克制科技氛围",
         "computations-no-cello.mp3", "computations/",
         "'Computations in a Snowstorm' by Scott Buckley - released under CC-BY 4.0. www.scottbuckley.com.au",
         ["数字人混剪", "园区宣传"], 0.11, "算力、数据、产业研究"),
        ("bgm-sb-inbound", "Inbound · 稳步昂扬",
         "inbound.mp3", "inbound/",
         "'Inbound' by Scott Buckley - released under CC-BY 4.0. www.scottbuckley.com.au",
         ["数字人混剪", "园区宣传"], 0.12, "招商成果、城市发展"),
        ("bgm-sb-machina", "Machina · 数字科技氛围",
         "machina.mp3", "machina/",
         "'Machina' by Scott Buckley - released under CC-BY 4.0. www.scottbuckley.com.au",
         ["数字人混剪", "信息流广告"], 0.12, "科技、算力、智能制造"),
        ("bgm-sb-clear-skies", "Clear Skies · 明朗积极",
         "clear-skies.mp3", "clear-skies/",
         "'Clear Skies' by Scott Buckley - released under CC-BY 4.0. www.scottbuckley.com.au",
         ["园区宣传", "信息流广告"], 0.13, "城市形象、园区航拍、发展愿景"),
    ]
    return [
        item(
            "bgm", asset_id, name, source + page, "CC-BY-4.0",
            file=f"asset-packs/bgm/open-commercial-2026-09/{filename}",
            attribution_required=True,
            attribution=attribution,
            default_for=routes,
            default_gain=gain,
            mood=mood,
            version="2026-09",
        )
        for asset_id, name, filename, page, attribution, routes, gain, mood in definitions
    ]


def sfx_items() -> list[dict]:
    pages = {
        "ui": "https://kenney.nl/assets/ui-audio",
        "interface": "https://kenney.nl/assets/interface-sounds",
        "impact": "https://kenney.nl/assets/impact-sounds",
        "jingle": "https://kenney.nl/assets/music-jingles",
    }
    definitions = [
        ("sfx-kenney-click-01", "轻点击 01", "kenney-ui-click-01.ogg", "ui", "ui", ["信息流广告", "数字人混剪"]),
        ("sfx-kenney-click-03", "轻点击 03", "kenney-ui-click-03.ogg", "ui", "ui", ["信息流广告", "数字人混剪"]),
        ("sfx-kenney-rollover-02", "轻划过", "kenney-ui-rollover-02.ogg", "ui", "ui", ALL_ROUTES),
        ("sfx-kenney-switch-01", "切换提示 01", "kenney-ui-switch-01.ogg", "ui", "transition", ALL_ROUTES),
        ("sfx-kenney-switch-06", "切换提示 06", "kenney-ui-switch-06.ogg", "ui", "transition", ALL_ROUTES),
        ("sfx-kenney-switch-12", "切换提示 12", "kenney-ui-switch-12.ogg", "ui", "transition", ALL_ROUTES),
        ("sfx-kenney-confirm-01", "确认提示 01", "kenney-confirm-01.ogg", "interface", "alert", ALL_ROUTES),
        ("sfx-kenney-confirm-02", "确认提示 02", "kenney-confirm-02.ogg", "interface", "alert", ALL_ROUTES),
        ("sfx-kenney-open-01", "卡片展开", "kenney-open-01.ogg", "interface", "transition", ALL_ROUTES),
        ("sfx-kenney-close-01", "卡片收起", "kenney-close-01.ogg", "interface", "transition", ALL_ROUTES),
        ("sfx-kenney-glitch-01", "数字故障", "kenney-glitch-01.ogg", "interface", "digital", ["数字人混剪", "信息流广告"]),
        ("sfx-kenney-glass-01", "清脆闪现", "kenney-glass-01.ogg", "interface", "highlight", ["信息流广告"]),
        ("sfx-kenney-drop-01", "信息落点", "kenney-drop-01.ogg", "interface", "highlight", ALL_ROUTES),
        ("sfx-kenney-bell-heavy", "重铃强调", "kenney-impact-bell-heavy.ogg", "impact", "hit", ALL_ROUTES),
        ("sfx-kenney-generic-light", "轻量重音", "kenney-impact-generic-light.ogg", "impact", "hit", ALL_ROUTES),
        ("sfx-kenney-metal-heavy", "金属重音", "kenney-impact-metal-heavy.ogg", "impact", "hit", ["数字人混剪", "信息流广告"]),
        ("sfx-kenney-wood-heavy", "柔和重音", "kenney-impact-wood-heavy.ogg", "impact", "hit", ["园区宣传", "数字人混剪"]),
        ("sfx-kenney-jingle-hit", "短促收尾", "kenney-jingle-hit-00.ogg", "jingle", "jingle", ["信息流广告"]),
        ("sfx-kenney-jingle-pizzi", "轻快收尾", "kenney-jingle-pizzi-00.ogg", "jingle", "jingle", ["信息流广告"]),
        ("sfx-kenney-jingle-sax", "活泼收尾", "kenney-jingle-sax-00.ogg", "jingle", "jingle", ["信息流广告"]),
    ]
    return [
        item(
            "sfx", asset_id, name, pages[pack], "CC0-1.0",
            file=f"asset-packs/sfx/open-commercial-2026-09/{filename}",
            attribution_required=False,
            default_for=routes,
            effect_kind=effect_kind,
            version="2026-09",
        )
        for asset_id, name, filename, pack, effect_kind, routes in definitions
    ]


def sticker_items() -> list[dict]:
    pages = {
        "base": "https://kenney.nl/assets/game-icons",
        "expansion": "https://kenney.nl/assets/game-icons-expansion",
        "emote": "https://kenney.nl/assets/emotes-pack",
    }
    definitions = [
        ("stk-kenney-target", "目标", "kenney-target.png", "base", "目标、招商指标", ALL_ROUTES),
        ("stk-kenney-trophy", "成果奖杯", "kenney-trophy.png", "base", "成绩、领先", ALL_ROUTES),
        ("stk-kenney-warning", "风险提示", "kenney-warning.png", "base", "风险、注意", ALL_ROUTES),
        ("stk-kenney-information", "信息说明", "kenney-information.png", "base", "说明、政策", ALL_ROUTES),
        ("stk-kenney-checkmark", "完成对勾", "kenney-checkmark.png", "base", "完成、优势", ALL_ROUTES),
        ("stk-kenney-star", "亮点星标", "kenney-star.png", "base", "亮点、推荐", ALL_ROUTES),
        ("stk-kenney-flag", "目标旗帜", "kenney-flag.png", "expansion", "目标、落地", ALL_ROUTES),
        ("stk-kenney-coin", "资金金币", "kenney-coin.png", "expansion", "投资、补贴", ALL_ROUTES),
        ("stk-kenney-cpu", "算力芯片", "kenney-cpu.png", "expansion", "算力、科技", ["数字人混剪", "信息流广告"]),
        ("stk-kenney-cloud-upload", "云端数据", "kenney-cloud-upload.png", "expansion", "数据、平台", ["数字人混剪", "信息流广告"]),
        ("stk-kenney-car", "交通汽车", "kenney-car.png", "expansion", "交通、区位", ["园区宣传", "数字人混剪"]),
        ("stk-kenney-idea", "创意灯泡", "kenney-idea.png", "emote", "创新、方案", ALL_ROUTES),
        ("stk-kenney-cash", "资金提示", "kenney-cash.png", "emote", "优惠、补贴", ["信息流广告"]),
        ("stk-kenney-stars", "多重亮点", "kenney-stars.png", "emote", "亮点、成果", ALL_ROUTES),
        ("stk-kenney-alert", "重点提醒", "kenney-alert.png", "emote", "注意、限时", ["信息流广告"]),
    ]
    return [
        item(
            "sticker", asset_id, name, pages[pack], "CC0-1.0",
            file=f"asset-packs/stickers/open-commercial-2026-09/{filename}",
            attribution_required=False,
            default_for=routes,
            note=note,
            sticker_kind="icon",
            version="2026-09",
        )
        for asset_id, name, filename, pack, note, routes in definitions
    ]


def transition_items() -> list[dict]:
    definitions = [
        ("tr-fade-soft", "柔和叠化", "fade", 0.45, ALL_ROUTES),
        ("tr-fade-black", "淡黑转场", "fadeblack", 0.35, ["园区宣传", "数字人混剪"]),
        ("tr-wipe-left", "向左擦除", "wipeleft", 0.35, ["信息流广告"]),
        ("tr-slide-left", "向左滑入", "slideleft", 0.32, ["信息流广告"]),
        ("tr-smooth-up", "平滑上推", "smoothup", 0.38, ALL_ROUTES),
        ("tr-circle-open", "圆形展开", "circleopen", 0.45, ["园区宣传", "数字人混剪"]),
        ("tr-radial", "径向转场", "radial", 0.42, ["数字人混剪", "信息流广告"]),
        ("tr-zoom-in", "轻推近", "zoomin", 0.35, ALL_ROUTES),
    ]
    return [
        item(
            "transition", asset_id, name,
            "https://ffmpeg.org/ffmpeg-filters.html#xfade",
            "generated-from-permissive-source",
            transition=transition,
            duration_s=duration,
            default_for=routes,
            attribution_required=False,
            version="2026-09",
        )
        for asset_id, name, transition, duration, routes in definitions
    ]


def subtitle_items() -> list[dict]:
    source_han = "https://github.com/adobe-fonts/source-han-serif"
    smiley = "https://github.com/atelier-anchor/smiley-sans"
    return [
        item(
            "subtitle", "sub-policy-bluebar", "政策蓝条·稳重说明", source_han, "OFL-1.1",
            font="SourceHanSerifSC-Heavy.otf", fontname_guess="Source Han Serif SC Heavy",
            size=58, punch_size=68, primary_hex="#FFFFFF", highlight_hex="#8FD3FF",
            outline_hex="#0B1F33", shadow_hex="#000000", bold=True, alignment=2,
            margin_v=420, outline=4, shadow=1, border_style=1, max_chars_per_line=13,
            max_lines=2, fade_in_ms=80, fade_out_ms=100, tail_pad_seconds=0.12,
            default_for=["园区宣传", "数字人混剪"], attribution_required=False,
            version="2026-09",
        ),
        item(
            "subtitle", "sub-ad-smiley-punch", "广告笑脸体·橙色强调", smiley, "OFL-1.1",
            font="SmileySans-Oblique.otf", fontname_guess="Smiley Sans Oblique",
            size=72, punch_size=88, primary_hex="#FFFFFF", highlight_hex="#FF8A3D",
            outline_hex="#161616", shadow_hex="#000000", bold=True, alignment=2,
            margin_v=390, outline=5, shadow=2, border_style=1, max_chars_per_line=11,
            max_lines=2, fade_in_ms=60, fade_out_ms=80, tail_pad_seconds=0.1,
            default_for=["信息流广告", "数字人混剪"], attribution_required=False,
            version="2026-09",
        ),
    ]


def title_card_items() -> list[dict]:
    source = "https://github.com/adobe-fonts/source-han-serif"
    definitions = [
        ("card-location-tech", "科技区位卡", "location", "#0B1F33", "#42B8FF", "区位、交通、园区地址"),
        ("card-data-emphasis", "核心数据卡", "data", "#171717", "#FD9437", "面积、投资额、产值、排名"),
        ("card-policy-summary", "政策摘要卡", "policy", "#F4F7FA", "#1D6FA5", "政策条款、扶持条件"),
    ]
    return [
        item(
            "title_card", asset_id, name, source, "OFL-1.1",
            card_kind=card_kind, background_hex=background, accent_hex=accent,
            font="SourceHanSerifSC-Heavy.otf", note=note,
            default_for=ALL_ROUTES, attribution_required=False, version="2026-09",
        )
        for asset_id, name, card_kind, background, accent, note in definitions
    ]


def fancy_text_items() -> list[dict]:
    smiley = "https://github.com/atelier-anchor/smiley-sans"
    source_han = "https://github.com/adobe-fonts/source-han-serif"
    return [
        item(
            "fancy_text", "ft-smiley-orange", "笑脸体·橙红弹入", smiley, "OFL-1.1",
            font="SmileySans-Oblique.otf", fontname_guess="Smiley Sans Oblique",
            fill=["#FFD166", "#FF7A3D"], stroke_hex="#6F2200", glow="#FFB347",
            shadow=True, entrance="scale", default_for=["信息流广告", "数字人混剪"],
            attribution_required=False, version="2026-09",
        ),
        item(
            "fancy_text", "ft-tech-blue", "科技蓝·轻发光", source_han, "OFL-1.1",
            font="SourceHanSerifSC-Heavy.otf", fontname_guess="Source Han Serif SC Heavy",
            fill=["#D8F4FF", "#42B8FF"], stroke_hex="#0B3D66", glow="#42B8FF",
            shadow=True, entrance="slide", default_for=ALL_ROUTES,
            attribution_required=False, version="2026-09",
        ),
        item(
            "fancy_text", "ft-policy-red", "政策红·重点强调", source_han, "OFL-1.1",
            font="SourceHanSerifSC-Heavy.otf", fontname_guess="Source Han Serif SC Heavy",
            fill=["#FFF4E8", "#FFB266"], stroke_hex="#B52418", glow=None,
            shadow=True, entrance="scale", default_for=["园区宣传", "数字人混剪"],
            attribution_required=False, version="2026-09",
        ),
    ]


def merge_core_manifest(filename: str, new_items: list[dict]) -> None:
    path = ROOT / "assets" / "manifests" / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    incoming_ids = {entry["id"] for entry in new_items}
    excluded_ids = incoming_ids | RETIRED_IDS.get(filename, set())
    payload["items"] = [
        entry for entry in payload.get("items", []) if entry.get("id") not in excluded_ids
    ] + new_items
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_docs(all_items: list[dict]) -> None:
    attribution_lines = [
        "# Open Commercial 2026-09 素材署名",
        "",
        "本包允许商业项目使用；其中 Scott Buckley 的音乐采用 CC BY 4.0，发布时必须署名。",
        "",
    ]
    for entry in all_items:
        if entry.get("attribution_required"):
            attribution_lines.append(f"- {entry['attribution']}")
    attribution_lines.extend([
        "",
        "Kenney 音效与图标采用 CC0 1.0，无强制署名；Smiley Sans 与思源字体采用 OFL 1.1。",
        "各来源许可证副本见 `licenses/`。",
        "",
    ])
    (PACK_ROOT / "ATTRIBUTION.md").write_text("\n".join(attribution_lines), encoding="utf-8")
    readme = """# CUTROOM Open Commercial 2026-09\n\n精选用于招商、园区、产业与信息流广告的开放商业素材包。\n\n- `bgm/`：5 首 CC BY 4.0 背景音乐，发布时必须署名。\n- `sfx/`：20 个 Kenney CC0 音效。\n- `stickers/`：15 个 Kenney CC0 透明 PNG。\n- `licenses/`：下载时保留的许可证副本。\n- `manifest.json`：来源、授权、哈希、用途和 CUTROOM 资产 ID。\n\n转场为 FFmpeg xfade 参数预设，不包含第三方视频模板。字幕、文字卡和花字为基于开放字体的本地参数预设。\n"""
    (PACK_ROOT / "README.md").write_text(readme, encoding="utf-8")


def main() -> None:
    groups = {
        "bgm": bgm_items(),
        "sfx": sfx_items(),
        "sticker": sticker_items(),
        "transition": transition_items(),
        "subtitle": subtitle_items(),
        "title_card": title_card_items(),
        "fancy_text": fancy_text_items(),
    }
    all_items = [entry for entries in groups.values() for entry in entries]
    pack_manifest = {
        "pack_id": "open-commercial-2026-09",
        "name": "开放商用包装素材精选 2026-09",
        "version": "2026-09",
        "downloaded_at": IMPORTED_AT,
        "items": all_items,
    }
    PACK_ROOT.mkdir(parents=True, exist_ok=True)
    (PACK_ROOT / "manifest.json").write_text(
        json.dumps(pack_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    merge_core_manifest("bgm.json", groups["bgm"])
    merge_core_manifest("sfx.json", groups["sfx"])
    merge_core_manifest("stickers.json", groups["sticker"])
    merge_core_manifest("subtitle-presets.json", groups["subtitle"])
    merge_core_manifest("visual-cards.json", groups["title_card"])
    merge_core_manifest("fancy-text-presets.json", groups["fancy_text"])
    write_docs(all_items)
    print(json.dumps({kind: len(entries) for kind, entries in groups.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
