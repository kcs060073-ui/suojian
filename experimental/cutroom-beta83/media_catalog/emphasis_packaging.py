"""Reference-position emphasis, using approved local fonts and the selected A/B designs."""
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter


def split_emphasis_cues(rows, voice_path=None):
    from .reference_editing import split_intro_cues
    result=[]
    for row in rows:
        text=row["text"]
        if not re.search(r"层高|承重|单层最大|最小",text):
            result.append(row);continue
        pieces=[x for x in re.split(r"(?<=[，,、])",text) if x.strip()]
        if len(pieces)<2:result.append(row);continue
        # Reuse pause-assisted timing on a temporary separator, without altering locked copy.
        temporary={**row,"text":pieces[0]+"："+"".join(pieces[1:])}
        split=split_intro_cues([temporary],voice_path)
        if len(split)!=len(pieces):result.append(row);continue
        for cue,original in zip(split,pieces):
            result.append({**cue,"text":original,"sourceCueText":text})
    return result


def make_emphasis(root, task_id, plan_id, events, policy):
    settings=policy.get("emphasis") or {}
    if not settings.get("enabled"):return [],[]
    from .packaging_plan import find_item
    for template in ["ft-gold","ft-smiley-orange"]:
        if find_item("fancy_text",template).get("license") != "commercial-approved":
            raise ValueError("EMPHASIS_FONT_LICENSE_UNAVAILABLE")
    W,H=policy["width"],policy["height"]
    font_regular=Path(root)/"assets/fonts/5 Alimama ShuHeiTi 阿里妈妈数黑.otf"
    font_number=Path(root)/"assets/fonts/SmileySans-Oblique.otf"
    target=Path(root)/"runtime/workbench/tasks"/task_id/"packaging-effects"/plan_id
    target.mkdir(parents=True,exist_ok=True)
    fill=policy["subtitle"]["primary_hex"]
    report,overlays=[],[]
    for index,event in enumerate(events):
        text=event["displayText"]
        attribute=text in {"国资","可租可售","可租","可售"}
        numeric=bool(re.match(r"^(?:层高|一层承重|单层最大|最小|免租)",text) and re.search(r"\d",text))
        if not(attribute or numeric):continue
        im=Image.new("RGBA",(W,H));draw=ImageDraw.Draw(im)
        cy=settings["attribute_center_y"] if attribute else settings["number_center_y"]
        parts=[]
        if attribute:
            labels=['可租','可售'] if text=='可租可售' else [text]
            size=round(W*.057);font=ImageFont.truetype(str(font_regular),size)
            widths=[round(font.getlength(x)+W*.045) for x in labels]
            gap=W*.025;left=(W-sum(widths)-gap*(len(labels)-1))/2
            for label,width in zip(labels,widths):
                top=cy*H-size*.65
                draw.rounded_rectangle((left,top,left+width,top+size*1.25),radius=14,fill=(13,17,24,220),outline=fill,width=3)
                draw.text((left+width/2,cy*H),label,font=font,anchor='mm',fill=fill)
                left+=width+gap
        else:
            matches=list(re.finditer(r"\d+(?:\.\d+)?(?:到\d+)?",text));cursor=0
            for match in matches:
                if match.start()>cursor:parts.append((text[cursor:match.start()],font_regular,round(W*.046)))
                parts.append((match.group().replace('到','–'),font_number,round(W*.095)))
                cursor=match.end()
            if cursor<len(text):parts.append((text[cursor:],font_regular,round(W*.046)))
            fonts=[ImageFont.truetype(str(path),size) for _,path,size in parts]
            widths=[font.getlength(value) for (value,_,_),font in zip(parts,fonts)]
            available=W*.8
            scale=min(1,available/(sum(widths)+20*(len(parts)-1)))
            if scale<1:
                fonts=[ImageFont.truetype(str(path),round(size*scale)) for _,path,size in parts]
                widths=[font.getlength(value) for (value,_,_),font in zip(parts,fonts)]
            left=(W-sum(widths)-20*(len(parts)-1))/2
            mask=Image.new("L",(W,H));mask_draw=ImageDraw.Draw(mask)
            for (value,path,size),font,width in zip(parts,fonts,widths):
                draw.text((left,cy*H),value,font=font,anchor='lm',fill=fill,stroke_width=7,stroke_fill='#111111')
                mask_draw.text((left,cy*H),value,font=font,anchor='lm',fill=255)
                left+=width+20
            gradient=Image.new('RGBA',(W,H),fill);gd=ImageDraw.Draw(gradient)
            radius=W*.095/2
            for yy in range(max(0,int(cy*H-radius)),min(H,int(cy*H+radius))):
                amount=(yy-(cy*H-radius))/(2*radius)
                gd.line([(0,yy),(W,yy)],fill=(255,round(239-27*amount),round(67-35*amount),255))
            gradient.putalpha(mask);im.alpha_composite(gradient)
        asset=target/f'cue-{index:03}.png';im.save(asset)
        template=settings['attribute_template'] if attribute else settings['number_template']
        overlays.append({'src':str(asset),'start':event['start'],'end':event['end'],'x':0,'y':0,'scale':1,'opacity':1,
                         'entrance':'fade' if attribute else 'slide','text':text,'template':template})
        report.append({'text':text,'start':event['start'],'end':event['end'],'template':template,'center_y':cy,
                       'reference_evidence':settings.get('evidence',[]),'timing':event.get('subtitleTimingMode','existing-phrase')})
        # Replace the body caption with emphasis, avoiding a duplicate text layer.
        event['displayText']=''
        event['emphasisReplacement']=True
    return overlays,report
