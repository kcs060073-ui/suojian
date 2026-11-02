#!/usr/bin/env python3
"""动态生成花字透明 PNG，供 render-mix.ts overlay 使用。

输入: --text <文案> --preset-id <id> --out <path> [--base-size 200]
读取 assets/manifests/fancy-text-presets.json 中的样式预设，
渲染渐变填充 + 描边 + 发光 + 圆角底 + 阴影，输出 RGBA PNG。
"""
from __future__ import annotations
import argparse, json, os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "assets", "manifests", "fancy-text-presets.json")
FONT_DIR = os.path.join(ROOT, "assets", "fonts")


def hex_to_rgba(h: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4)) + (alpha,)  # type: ignore[return-value]


def load_font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONT_DIR, filename), size)


def make_gradient(size: tuple[int, int], colors: list[tuple[int, int, int, int]], direction: str = "vertical") -> Image.Image:
    """生成纯色或渐变 RGBA 图。colors 按 0→1 均匀分布。"""
    w, h = size
    img = Image.new("RGBA", size)
    draw = ImageDraw.Draw(img)
    n = len(colors) - 1
    if n <= 0:
        draw.rectangle([0, 0, w, h], fill=colors[0] if colors else (255, 255, 255, 255))
        return img
    if direction == "vertical":
        for y in range(h):
            t = y / max(h - 1, 1)
            idx = min(int(t * n), n - 1)
            local_t = (t * n) - idx
            c1, c2 = colors[idx], colors[idx + 1]
            r = int(c1[0] + (c2[0] - c1[0]) * local_t)
            g = int(c1[1] + (c2[1] - c1[1]) * local_t)
            b = int(c1[2] + (c2[2] - c1[2]) * local_t)
            a = int(c1[3] + (c2[3] - c1[3]) * local_t)
            draw.line([(0, y), (w, y)], fill=(r, g, b, a))
    else:
        for x in range(w):
            t = x / max(w - 1, 1)
            idx = min(int(t * n), n - 1)
            local_t = (t * n) - idx
            c1, c2 = colors[idx], colors[idx + 1]
            r = int(c1[0] + (c2[0] - c1[0]) * local_t)
            g = int(c1[1] + (c2[1] - c1[1]) * local_t)
            b = int(c1[2] + (c2[2] - c1[2]) * local_t)
            a = int(c1[3] + (c2[3] - c1[3]) * local_t)
            draw.line([(x, 0), (x, h)], fill=(r, g, b, a))
    return img


def render_text(text: str, preset: dict, base_size: int = 200) -> Image.Image:
    font = load_font(preset["font"], base_size)

    # 测量文字（含 baseline 偏移）
    tmp = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    stroke_w = max(4, base_size // 18)
    glow_r = max(14, base_size // 12) if preset.get("glow") else 0
    shadow_off = max(7, base_size // 24) if preset.get("shadow") else 0
    has_bg = bool(preset.get("bg_hex"))
    bg_pad = base_size // 3 if has_bg else 0
    pad = base_size // 5

    total_w = int(tw + pad * 2 + stroke_w * 2 + glow_r * 2 + abs(shadow_off) + bg_pad * 2)
    total_h = int(th + pad * 2 + stroke_w * 2 + glow_r * 2 + abs(shadow_off) + bg_pad * 2)

    img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 文字左上角：让 bbox 左上角对齐此点
    x0 = pad + glow_r + stroke_w + max(shadow_off, 0) + bg_pad
    y0 = pad + glow_r + stroke_w + max(shadow_off, 0) + bg_pad
    draw_x = x0 - bbox[0]
    draw_y = y0 - bbox[1]

    # 1) 圆角底（如预设带 bg_hex）
    if has_bg:
        bg_color = hex_to_rgba(preset["bg_hex"])
        bg_left = x0 - bg_pad
        bg_top = y0 - bg_pad
        bg_right = bg_left + tw + bg_pad * 2
        bg_bottom = bg_top + th + bg_pad * 2
        radius = min((bg_bottom - bg_top) // 2, 80)
        d.rounded_rectangle([bg_left, bg_top, bg_right, bg_bottom], radius=radius, fill=bg_color)

    # 2) 投影（无圆角底时给文字投）
    if preset.get("shadow") and not has_bg:
        d.text((draw_x + shadow_off, draw_y + shadow_off), text, font=font, fill=(0, 0, 0, 110))

    # 3) 发光层（置于文字下方）
    if glow_r:
        glow_color = hex_to_rgba(preset["glow"], 170)
        glow_layer = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        gd.text((draw_x, draw_y), text, font=font, fill=glow_color, stroke_width=stroke_w, stroke_fill=glow_color)
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=glow_r))
        img = Image.alpha_composite(img, glow_layer)

    # 4) 渐变填充文字层（mask 决定字形）
    fill_colors = [hex_to_rgba(c) for c in preset.get("fill", ["#FFFFFF"])]
    gradient = make_gradient((total_w, total_h), fill_colors, direction="vertical")
    mask = Image.new("L", (total_w, total_h), 0)
    md = ImageDraw.Draw(mask)
    md.text((draw_x, draw_y), text, font=font, fill=255, stroke_width=stroke_w, stroke_fill=255)
    gradient.putalpha(mask)
    img = Image.alpha_composite(img, gradient)

    # 5) 描边层（仅描边， fill 透明）
    stroke_color = hex_to_rgba(preset.get("stroke_hex", "#000000"))
    stroke_layer = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stroke_layer)
    sd.text((draw_x, draw_y), text, font=font, fill=(0, 0, 0, 0), stroke_width=stroke_w, stroke_fill=stroke_color)
    img = Image.alpha_composite(img, stroke_layer)

    return img


def main():
    parser = argparse.ArgumentParser(description="生成花字透明 PNG")
    parser.add_argument("--text", required=True, help="花字文案")
    parser.add_argument("--preset-id", required=True, help="fancy-text-presets.json 中的预设 id")
    parser.add_argument("--out", required=True, help="输出 PNG 路径")
    parser.add_argument("--base-size", type=int, default=200, help="基础字号（像素），默认 200")
    args = parser.parse_args()

    with open(MANIFEST, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    preset = next((x for x in manifest.get("items", []) if x.get("id") == args.preset_id), None)
    if not preset:
        raise SystemExit(f"找不到花字预设: {args.preset_id}")

    img = render_text(args.text, preset, base_size=args.base_size)
    out_dir = os.path.dirname(args.out) or "."
    os.makedirs(out_dir, exist_ok=True)
    img.save(args.out, "PNG")
    print(args.out, img.size)


if __name__ == "__main__":
    main()
