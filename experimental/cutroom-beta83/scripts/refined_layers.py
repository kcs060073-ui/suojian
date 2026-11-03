"""Versioned Pillow layers shared by preview/final and layout measurement.

The font weight is explicit: the bundled variable font defaults to weight 100.
No task-specific words or source facts belong in this renderer.
"""
from pathlib import Path
import argparse
import json
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageColor

VERSION = "refined-v1"
ROOT = Path(__file__).resolve().parents[1]
FONT = ROOT / "assets/fonts/NotoSansSC-VF.ttf"
WARM = (245, 242, 234, 255)
COPPER = (188, 159, 125, 255)
INK = (28, 35, 39, 255)


def font(size, weight, path=None):
    result = ImageFont.truetype(str(path or FONT), max(1, round(size)))
    try: result.set_variation_by_axes([weight])
    except OSError: pass
    return result


from functools import lru_cache
import subprocess
import shutil
import os

@lru_cache(maxsize=256)
def word_tokens(words):
    # Reuse the Node runtime already required by both renderers. ICU's Chinese
    # word boundaries avoid a second bundled dictionary or network dependency.
    node = os.environ.get("CUTROOM_RENDER_NODE") or (str(ROOT / "node.exe") if (ROOT / "node.exe").is_file() else shutil.which("node"))
    if not node: raise ValueError("PACKAGING_WORD_BOUNDARY_UNAVAILABLE: Node runtime unavailable")
    script = "const fs=require('fs');const s=fs.readFileSync(0,'utf8');process.stdout.write(JSON.stringify(Array.from(new Intl.Segmenter('zh',{granularity:'word'}).segment(s),x=>x.segment)))"
    result = subprocess.run([node,"-e",script],input=words,text=True,encoding="utf-8",
        capture_output=True,check=True,timeout=10)
    tokens = json.loads(result.stdout)
    if "".join(tokens) != words: raise ValueError("PACKAGING_TEXT_CHANGED: 分词不能修改原文")
    # Protect full numerical quantities, including decimal and common units.
    import re
    protected = list(re.finditer(r"\d+(?:[.,]\d+)*(?:[%％万亿千百元米亩㎡m²公里年个月天度吨]+)?", words))
    offsets=[];pos=0
    for token in tokens:
        pos+=len(token)
        if not any(m.start()<pos<m.end() for m in protected): offsets.append(pos)
    start=0;merged=[]
    for end in offsets: merged.append(words[start:end]);start=end
    return merged


def wrap(words, face, width):
    """Keep source text and natural word boundaries; balance two short lines."""
    lines=[]
    for paragraph in words.split("\n"):
        tokens=word_tokens(paragraph)
        current="";group=[]
        for token in tokens:
            if current and face.getlength(current+token)>width:
                group.append(current);current=""
            current+=token
        group.append(current)
        if len(group)==2:
            choices=[]
            for i in range(1,len(tokens)):
                left,right="".join(tokens[:i]),"".join(tokens[i:])
                lw,rw=face.getlength(left),face.getlength(right)
                if max(lw,rw)<=width:choices.append((abs(lw-rw),left,right))
            if choices: _,left,right=min(choices);group=[left,right]
        lines.extend(group)
    return lines


def text(canvas, xy, words, face, color, shadow=True):
    if shadow:
        layer = Image.new("RGBA", canvas.size)
        ImageDraw.Draw(layer).text((xy[0], xy[1] + 1), words, font=face,
                                  fill=(7, 12, 16, 125), anchor="lt")
        canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(1.5)))
    ImageDraw.Draw(canvas).text(xy, words, font=face, fill=color, anchor="lt")


def valid_layout(value, words):
    if isinstance(value, str):
        value = json.loads(value or "{}")
    if not value:
        return {"version": VERSION, "body": words}
    if value.get("version") != VERSION or value.get("text") != words:
        raise ValueError("PACKAGING_LAYOUT_STALE: 分层文字与当前正文不一致")
    parts = [value.get(k, "") for k in ("label", "kicker", "body")]
    if not all(isinstance(p, str) for p in parts) or not value.get("body"):
        raise ValueError("PACKAGING_LAYOUT_INVALID: 分层正文缺失")
    # Explicit spans preserve every meaningful character in order. Only a
    # linking 的 between label and subject may be rendered as whitespace.
    joined = "".join(parts)
    if joined != words and not (value.get("label") and words == value["label"] + "的" + value.get("kicker", "") + value.get("body", "")):
        raise ValueError("PACKAGING_LAYOUT_INVALID: 分层不能删改事实文字")
    return value


def draw_element(cue, width, height):
    """Return the actual raster and its non-shadow content bounds."""
    words = cue.get("text", "")
    role = cue.get("role", "title_card")
    design = valid_layout(cue.get("contentLayout"), words)
    # Uniform typography scale follows the shorter canvas edge; geometry uses
    # normalized coordinates on either orientation.
    unit = min(width, height) / 540
    scale = float(cue.get("scale", .60 if role == "title_card" else .40))
    baseline = .60 if role == "title_card" else .40
    unit *= scale / baseline
    x = round(float(cue.get("x", .075 if role == "title_card" else .063)) * width)
    y = round(float(cue.get("y", .09 if role == "title_card" else .575)) * height)
    max_width = min(width * scale, width * .98 - x)
    canvas = Image.new("RGBA", (width, height))
    label, kicker, body = [design.get(k, "") for k in ("label", "kicker", "body")]
    if role == "title_card":
        face = font(60 * unit, 650)
        lines = wrap(body, face, max_width)
        cursor = y
        if label:
            ImageDraw.Draw(canvas).rectangle((x, cursor, x + 3 * unit, cursor + 16 * unit), fill=COPPER)
            text(canvas, (x + 14 * unit, cursor), label, font(15 * unit, 500), WARM)
            cursor += 41 * unit
        if kicker:
            text(canvas, (x, cursor), kicker, font(28 * unit, 500), COPPER)
            cursor += 45 * unit
        for line in lines:
            text(canvas, (x, cursor), line, face, WARM)
            cursor += 70 * unit
        content_width = max([face.getlength(line) for line in lines] +
                            [font(28 * unit, 500).getlength(kicker), font(15 * unit, 500).getlength(label) + (14 * unit if label else 0)])
        bounds = (x, y, x + content_width, cursor - 10 * unit)
        mask = Image.new("L", canvas.size)
        ImageDraw.Draw(mask).rounded_rectangle((x - 70 * unit, y - 40 * unit,
            bounds[2] + 20 * unit, bounds[3] + 30 * unit), radius=50 * unit, fill=155)
        shade = Image.new("RGBA", canvas.size, (17, 25, 31, 0))
        shade.putalpha(mask.filter(ImageFilter.GaussianBlur(55 * unit)))
        shade.alpha_composite(canvas); canvas = shade
    elif role == "fancy_text":
        face = font(36 * unit, 600)
        lines = wrap(label + kicker + body, face, max_width)
        for index,line in enumerate(lines): text(canvas,(x,y+index*44*unit),line,face,WARM)
        bounds = (x,y,x+max(face.getlength(line) for line in lines),y+len(lines)*44*unit)
    else:
        body = kicker + body
        padding = 20 * unit
        face = font(28 * unit, 600)
        lines = wrap(body, face, max_width - padding * 2)
        content_width = max(face.getlength(line) for line in lines)
        box_w = max(content_width + 2 * padding,
                    font(15 * unit, 600).getlength(label) + 75 * unit if label else 0)
        box_h = 2 * padding + len(lines) * 35 * unit + (30 * unit if label else 0) - 7 * unit
        w, h = round(box_w), round(box_h)
        mask = Image.new("L", (w, h))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=10 * unit, fill=255)
        gradient = Image.new("L", (w, h)); d = ImageDraw.Draw(gradient)
        for row in range(h): d.line((0, row, w, row), fill=round(238 - row / h * 55))
        card = Image.new("RGBA", (w, h), WARM)
        card.putalpha(ImageChops.multiply(gradient, mask)); canvas.alpha_composite(card, (x, y))
        cursor = y + padding
        if label:
            ImageDraw.Draw(canvas).rectangle((x + padding, cursor + 7 * unit,
                x + padding + 24 * unit, cursor + 10 * unit), fill=COPPER)
            text(canvas, (x + padding + 35 * unit, cursor), label, font(15 * unit, 600), INK, False)
            cursor += 30 * unit
        for line in lines:
            text(canvas, (x + padding, cursor), line, face, INK, False); cursor += 35 * unit
        bounds = (x, y, x + w, y + h)
    return canvas, bounds


def draw_subtitle(cue, width, height, appearance=None):
    appearance = appearance or {}
    unit = min(width, height) / 540
    face = font(float(appearance.get("fontSize", 54)) / 2 * unit, 500, appearance.get("fontFile"))
    span = float(appearance.get("width", .8222)) * width
    lines = wrap(cue["text"].strip(), face, span)
    line_height = face.size * 34 / 27
    text_width = max(face.getlength(line) for line in lines)
    if text_width + 28 * unit > width * .96:
        raise ValueError("PACKAGING_SAFE_AREA: 字幕完整词组超出画面，请调整字号或可用宽度")
    center = float(appearance.get("x", .5)) * width
    bottom = float(appearance.get("y", 822 / 960)) * height
    top = bottom - (len(lines) * line_height - 7 * unit)
    canvas = Image.new("RGBA", (width, height))
    opacity = round(255 * float(appearance.get("backgroundOpacity", 99 / 255)))
    ImageDraw.Draw(canvas).rounded_rectangle((center - text_width / 2 - 14 * unit,
        top - 9 * unit, center + text_width / 2 + 14 * unit, bottom + 10 * unit),
        radius=5 * unit, fill=ImageColor.getrgb(appearance.get("backgroundHex", "#101417")) + (opacity,))
    color = appearance.get("primaryHex", WARM)
    for index, line in enumerate(lines):
        xy = (center - face.getlength(line) / 2, top + index * line_height)
        outline = round(float(appearance.get("outlineWidth", 0)) / 2 * unit)
        # Actual appearance controls are consumed here, not only in the editor.
        ImageDraw.Draw(canvas).text(xy, line, font=face, fill=color, anchor="lt",
            stroke_width=outline, stroke_fill=appearance.get("outlineHex", "#101417"))
        if cue.get("highlight"):
            for term in cue.get("highlightTerms", []):
                offset = 0
                while term and (pos := line.find(term, offset)) >= 0:
                    ImageDraw.Draw(canvas).text((xy[0]+face.getlength(line[:pos]),xy[1]),term,font=face,
                        fill=appearance.get("highlightHex", "#BC9F7D"),anchor="lt",stroke_width=outline,
                        stroke_fill=appearance.get("outlineHex", "#101417"))
                    offset=pos+len(term)
    return canvas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    result = []
    for i, cue in enumerate(payload["cues"]):
        image = (draw_subtitle(cue, payload["width"], payload["height"], payload.get("appearance"))
            if cue["role"] == "subtitle" else draw_element(cue, payload["width"], payload["height"])[0])
        bounds = image.getbbox() or (0, 0, 1, 1)
        path = out / f"refined-{i}.png"; image.crop(bounds).save(path)
        result.append({"file": str(path), "x": bounds[0], "y": bounds[1], "start": cue["start"], "end": cue["end"], "motion": cue["role"] != "subtitle"})
    print(json.dumps(result))


if __name__ == "__main__": main()
