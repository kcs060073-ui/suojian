"""Editable emphasis components, decorations, and a version-gated native adapter."""
import copy
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from suo_jian.blueprint.templates import digest, write_json
from suo_jian.integrations.cutroom.execution_plan import file_signature


def resolve_native_binding(registry, template, editor_version):
    binding = (registry or {}).get("bindings", {}).get(template)
    if not binding or binding.get("status") != "verified":
        return None
    if not editor_version or binding.get("editor_version") != editor_version:
        return None
    if binding.get("kind") not in {"effect", "bubble"} or not binding.get("effect_id"):
        raise ValueError("已验证的原生花字映射缺少效果类型或编号。")
    if binding["kind"] == "bubble" and not binding.get("resource_id"):
        raise ValueError("原生气泡映射缺少资源编号。")
    return copy.deepcopy(binding)


def apply_native_binding(segment, binding):
    if binding is None:
        return
    if binding["kind"] == "effect":
        segment.add_effect(binding["effect_id"])
    elif binding["kind"] == "bubble":
        segment.add_bubble(binding["effect_id"], binding["resource_id"])
    else:
        raise ValueError("不支持的原生花字映射。")


def add_layer_animation(segment, row, draft, *, text=False):
    animation = row.get("animation")
    if not animation:
        return
    duration = float(row["duration"])
    fade_in = min(.1, duration / 4)
    fade_out = min(.08, duration / 4)
    if text:
        segment.add_animation(draft.TextIntro.渐显, duration=int(fade_in*1e6))
        segment.add_animation(draft.TextOutro.渐隐, duration=int(fade_out*1e6))
    else:
        for time,value in [(0,0),(fade_in,1),(duration-fade_out,1),(duration,0)]:
            segment.add_keyframe(draft.KeyframeProperty.alpha,int(round(time*1e6)),value)
    if animation == "slide":
        final_y = segment.clip_settings.transform_y
        segment.add_keyframe(draft.KeyframeProperty.position_y,0,final_y-.06)
        segment.add_keyframe(draft.KeyframeProperty.position_y,int(min(.14,duration/3)*1e6),final_y)


def layered_emphasis_plan(plan, cutroom_root, artifact_root, *, registry=None, editor_version=None):
    """Replace baked glyphs with editable parts; never mutate the preview lineage."""
    value = copy.deepcopy(plan)
    if value.get("export_mode") == "layered-emphasis":
        return value
    policy = (value.get("reference_match") or {}).get("policy") or {}
    base = policy.get("subtitle", {})
    width, height = value["canvas"]["width"], value["canvas"]["height"]
    regular_path = Path(cutroom_root)/"assets/fonts/5 Alimama ShuHeiTi 阿里妈妈数黑.otf"
    number_path = Path(cutroom_root)/"assets/fonts/SmileySans-Oblique.otf"
    for path in (regular_path, number_path):
        if not path.is_file():
            raise FileNotFoundError("分层花字所需字体已移动："+path.name)
    version = digest({"version":1,"canvas":value["canvas"],"base":base,"report":value.get("emphasis_report"),
                      "fonts":[file_signature(regular_path),file_signature(number_path)],"registry":registry,"editor":editor_version})[:16]
    folder = Path(artifact_root)/version
    folder.mkdir(parents=True,exist_ok=True)
    replacements=[]
    components=[]
    regular_color=base.get("primary_hex","#FFE820")
    reports=value.get("emphasis_report") or []
    for cue_index,row in enumerate(value.get("overlays", [])):
        if row["kind"] != "重点花字素材":
            replacements.append(row)
            continue
        match=next((item for item in reports if abs(item["start"]-row["start"])<.01 and abs(item["end"]-row["start"]-row["duration"])<.02),None)
        if match is None:
            raise ValueError("花字素材缺少对应文字、时间和模板映射。")
        template=match["template"]
        if template not in {"business-label-A","slanted-number-B"}:
            raise ValueError("尚未支持此花字的分层导出，请选择保留外观。")
        group=f"emphasis-{cue_index:02}"
        cy=float(match["center_y"])
        native_binding=resolve_native_binding(registry,template,editor_version)
        label=template=="business-label-A"
        animation="fade" if label else "slide"
        decoration=Image.new("RGBA",(width,height))
        draw=ImageDraw.Draw(decoration)
        parts=[]
        if label:
            labels=["可租","可售"] if match["text"]=="可租可售" else [match["text"]]
            size=round(width*.057)
            font=ImageFont.truetype(str(regular_path),size)
            widths=[round(font.getlength(text)+width*.045) for text in labels]
            gap=width*.025
            left=(width-sum(widths)-gap*(len(labels)-1))/2
            for text,box_width in zip(labels,widths):
                top=cy*height-size*.65
                draw.rounded_rectangle((left,top,left+box_width,top+size*1.25),radius=14,
                                       fill=(13,17,24,220),outline=regular_color,width=3)
                parts.append((text,size,left+box_width/2,False))
                left+=box_width+gap
        else:
            runs=[];cursor=0
            for number in re.finditer(r"\d+(?:\.\d+)?(?:到\d+)?",match["text"]):
                if number.start()>cursor:runs.append((match["text"][cursor:number.start()],False))
                runs.append((number.group().replace("到","–"),True));cursor=number.end()
            if cursor<len(match["text"]):runs.append((match["text"][cursor:],False))
            if not any(numeric for _,numeric in runs):raise ValueError("数字模板缺少数字。")
            sizes=[round(width*(.095 if numeric else .046)) for _,numeric in runs]
            fonts=[ImageFont.truetype(str(number_path if numeric else regular_path),size) for (_,numeric),size in zip(runs,sizes)]
            run_widths=[font.getlength(text) for (text,_),font in zip(runs,fonts)]
            scale=min(1,width*.8/(sum(run_widths)+20*(len(runs)-1)))
            sizes=[max(1,round(size*scale)) for size in sizes]
            fonts=[ImageFont.truetype(str(number_path if numeric else regular_path),size) for (_,numeric),size in zip(runs,sizes)]
            run_widths=[font.getlength(text) for (text,_),font in zip(runs,fonts)]
            left=(width-sum(run_widths)-20*(len(runs)-1))/2
            for (text,numeric),size,run_width in zip(runs,sizes,run_widths):
                parts.append((text,size,left+run_width/2,numeric))
                if numeric:
                    # Decoration contains no glyphs; edited numbers remain native text.
                    y=cy*height+size*.53
                    draw.rounded_rectangle((left,y,left+run_width,y+max(3,height*.004)),radius=2,fill=regular_color)
                left+=run_width+20
        for part_index,(text,size,cx,numeric) in enumerate(parts):
            value["texts"].append({"kind":f"重点文字·{cue_index+1}-{part_index+1}","text":text,
                "start":row["start"],"duration":row["duration"],"group_id":group,"animation":animation,
                "native_binding":native_binding if (label or numeric) else None,
                "style":{**base,"size":size,"x":cx/width,"y":cy,"max_lines":1,
                         "fontname_guess":"Smiley Sans" if numeric else base.get("fontname_guess",""),
                         "italic":numeric,"outline":7 if numeric else (0 if label else 6)}})
        asset=folder/(group+"-decoration.png")
        decoration.save(asset)
        replacements.append({"kind":"花字装饰·"+group,"source":str(asset.resolve()),"start":row["start"],
            "duration":row["duration"],"x":0,"y":0,"scale":1,"opacity":1,"animation":animation,"group_id":group})
        value.setdefault("source_signatures",{})[str(asset.resolve())]=file_signature(asset)
        components.append({"group_id":group,"text":match["text"],"template":template,"start":row["start"],
            "duration":row["duration"],"parts":len(parts),"animation":animation,
            "backend":"native-template" if native_binding else "editable-layered","native_binding":native_binding})
    value["overlays"]=replacements
    value["emphasis_components"]=components
    value["export_mode"]="layered-emphasis"
    value["limitations"]=[x for x in value.get("limitations",[]) if not x.startswith("重点花字在预览")]
    value["limitations"].append("分层花字文字可修改；底板和数字强调线为独立素材，颜色、位置和时间来自预览。数字为近似斜体纯色文字，未还原原图片逐像素渐变。改字后需检查底板宽度；各层时间与动画可独立调整。")
    value["limitations"].append("花字各层已写入剪映原生关键帧或文字动画；尚需在当前剪映版本中核对字体、字号和动画显示。原生花字编号仅对经过版本验证的映射启用。")
    write_json(folder/"components.json",components)
    return value
