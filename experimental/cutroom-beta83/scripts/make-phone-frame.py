"""
生成 iPhone 风格样机边框 PNG（透明屏幕挖孔 + 钛金描边 + 灵动岛）。
渲染时把竖屏 APP 录屏垫在边框下方、对齐挖孔，再把本边框叠上去 → 手机样机效果。

屏幕挖孔尺寸/位置是和 render-promo.ts 的 PHONE 常量一一对应的，改这里也要改那里。

用法: .venv/Scripts/python scripts/make-phone-frame.py
产物: assets/phone_frame.png
"""
from PIL import Image, ImageDraw
import os

# —— 与 render-promo.ts PHONE 常量保持一致 ——
SCREEN_W, SCREEN_H = 422, 936     # 屏幕（挖孔）尺寸，比例≈9:20 贴合 576x1280 竖屏录屏
BEZEL = 18                        # 边框厚度
FRAME_W, FRAME_H = SCREEN_W + 2 * BEZEL, SCREEN_H + 2 * BEZEL
R_OUTER, R_SCREEN = 78, 56        # 外圆角 / 屏幕圆角
ISLAND_W, ISLAND_H = 116, 33      # 灵动岛

BODY = (22, 22, 24, 255)          # 机身（深钛灰）
EDGE = (78, 78, 84, 255)          # 高光描边
INNER = (8, 8, 9, 255)            # 屏幕内圈（黑边压一圈）
ISLAND = (0, 0, 0, 255)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "phone_frame.png")


def rr(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def main():
    img = Image.new("RGBA", (FRAME_W, FRAME_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 机身（外圆角实心）+ 高光描边
    rr(d, [0, 0, FRAME_W - 1, FRAME_H - 1], R_OUTER, EDGE)
    rr(d, [1, 1, FRAME_W - 2, FRAME_H - 2], R_OUTER - 1, BODY)

    # 屏幕内圈黑边（机身与玻璃之间压一圈）
    sx0, sy0 = BEZEL - 3, BEZEL - 3
    sx1, sy1 = BEZEL + SCREEN_W + 3, BEZEL + SCREEN_H + 3
    rr(d, [sx0, sy0, sx1, sy1], R_SCREEN + 3, INNER)

    # 屏幕挖孔（透明，露出下面的录屏）
    hx0, hy0 = BEZEL, BEZEL
    hx1, hy1 = BEZEL + SCREEN_W, BEZEL + SCREEN_H
    rr(d, [hx0, hy0, hx1, hy1], R_SCREEN, (0, 0, 0, 0))

    # 灵动岛（叠在屏幕顶部中央，盖住录屏顶部）
    cx = FRAME_W // 2
    iy = hy0 + 16
    rr(d, [cx - ISLAND_W // 2, iy, cx + ISLAND_W // 2, iy + ISLAND_H], ISLAND_H // 2, ISLAND)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT)
    print(f"✔ {OUT}  ({FRAME_W}x{FRAME_H}, 屏幕挖孔 {SCREEN_W}x{SCREEN_H} @ ({BEZEL},{BEZEL}))")


if __name__ == "__main__":
    main()
