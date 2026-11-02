"""Build a reusable reference policy from measured subtitles and shot analysis."""
import hashlib
import statistics
import subprocess
import tempfile
from pathlib import Path


def build_reference_policy(recipe, source_path):
    from PIL import Image
    source_path = Path(source_path).resolve()
    if not source_path.is_file():
        raise ValueError("参考视频已移动，请重新关联后再匹配字幕和镜头。")
    source = recipe["source"]
    width,height = int(source["width"]),int(source["height"])
    boxes = [row for row in recipe.get("subtitles",[]) if row.get("bbox",{}).get("width",0) > .15 and row.get("confidence",1) >= .85]
    if not boxes:
        raise ValueError("参考片没有可靠文字位置，请先完成字幕分析。")
    # Choose the most frequent vertical band, excluding occasional data/title cards.
    bands = {}
    for row in boxes:
        b=row["bbox"];band=round((b["y"]+b["height"]/2)/.15)
        bands.setdefault(band,[]).append(row)
    regular = max(bands.values(),key=len)
    top = statistics.median(row["bbox"]["y"] for row in regular)
    bottom = statistics.median(row["bbox"]["y"]+row["bbox"]["height"] for row in regular)
    sample = regular[0]
    timestamp=(sample["start"]+sample["end"])/2
    with tempfile.TemporaryDirectory(prefix="reference_style_") as folder:
        frame=Path(folder)/"frame.png"
        subprocess.run(["ffmpeg","-v","error","-y","-ss",str(timestamp),"-i",str(source_path),"-frames:v","1",str(frame)],check=True,capture_output=True,timeout=60)
        image=Image.open(frame).convert("RGB")
        b=sample["bbox"]
        region=image.crop((int(b["x"]*width),int(b["y"]*height),int((b["x"]+b["width"])*width),int((b["y"]+b["height"])*height)))
        bright=[(r,g,b) for r,g,b in region.getdata() if r>210 and g>190 and b<110]
        fill="#FFFFFF"
        if len(bright)>region.width*region.height*.035:
            fill="#"+"".join(f"{round(statistics.median(pixel[channel] for pixel in bright)):02X}" for channel in range(3))
    with source_path.open("rb") as handle:
        signature=hashlib.file_digest(handle,"sha256").hexdigest()
    return {"schema":1,"ratio":"16:9" if width>height else "9:16","width":width,"height":height,
            "reference_source":str(source_path),"reference_sha256":signature,
            "shots":recipe["shots"],"number_preset":"ft-pop",
            "subtitle":{"fontname_guess":"Alimama ShuHeiTi Bold","font":"5 Alimama ShuHeiTi 阿里妈妈数黑.otf",
                        "primary_hex":fill,"highlight_hex":fill,"outline_hex":"#111111","shadow_hex":"#000000",
                        "size":round(min(96,max(48,(bottom-top)*height*.84))),"margin_v":round((1-bottom+.014)*height),
                        "alignment":2,"outline":6,"shadow":1,"bold":True,"max_lines":1,"max_chars_per_line":15},
            "emphasis":build_emphasis_policy(recipe, top, bottom),
            "style_basis":"参考片常规字幕 OCR 位置与实际像素颜色；字体为已授权近似粗黑体",
            "fallback_order":["same-subject-same-motion","same-subject-similar-motion","same-subject-static","approved-available-visual"]}


def build_emphasis_policy(recipe, top, bottom):
    import re
    evidence=[]
    for row in recipe.get("onscreen_text",recipe.get("subtitles",[])):
        box=row.get("bbox") or {}
        if row.get("confidence",0)<.85 or box.get("width",0)<.15 or box.get("height",0)<.075: continue
        center=box["y"]+box["height"]/2
        if center>=top-.12: continue
        text=row["text"]
        if not re.search(r"租金|元|小面积|大面积|层高|承重|最大|最小",text): continue
        evidence.append({"text":text,"start":row["start"],"end":row["end"],"bbox":box,"confidence":row["confidence"]})
    centers=[r["bbox"]["y"]+r["bbox"]["height"]/2 for r in evidence]
    return {"enabled":True,"attribute_template":"business-label-A","number_template":"slanted-number-B",
            "attribute_center_y":(top+bottom)/2,"number_center_y":statistics.median(centers) if centers else .55,
            "evidence":evidence,"basis":"参考片中心大字/数字及面积对比的 OCR 位置；语义映射到新文案，采用用户选定的 A 标签与 B 数字字形。",
            "timing_basis":"新配音语句窗口，内部短句按停顿辅助估计，不是参考片秒数直搬。"}
