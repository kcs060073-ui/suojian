"""Explicit choices for the three locally vendored skills (no bilingual captions)."""
from pathlib import Path
import json

FEATURES = [
 ('vibe','world','寓言 / 夜曲','render','两套外观、色义、字幕读屏、无旁白；当前竖屏融合为适配版'),
 ('vibe','visualization','六种讲解画法与生活锚点','storyboard','实物、过程、数量、心智、规律、文字留白'),
 ('vibe','causality','连续空间与可计算实验','render','同世界连续运镜、因果动作、开头回扣；实验参数可配置'),
 ('vibe','silent','纯字幕无旁白','render','与讲解配音互斥，可独立输出对照'),
 ('vibe','events','事件音 / 用户音乐','audio_analyze','动作事件、提问留白、重音揭晓'),
 ('vibe','canvas','原生 Canvas 引擎','vendor_workbench','seek、静帧、转场拼图、片段、确定性检查、并行渲染；原生只支持横屏'),
 ('shotcraft','modes','模板 / 自主 / 共同创作','storyboard','保留三种制作决策模式，不覆盖已选风格'),
 ('shotcraft','recipes','完整镜头卡与所有变体','catalog','从本地 gallery/api/library.json 实时读取，附准确配方与 demo 源码'),
 ('shotcraft','capture','真实网页采集','capture','全页 / 元素截图、坐标与设计 tokens；仅采集用户指定网页'),
 ('shotcraft','components','组件与 3D 素材','catalog','PageCam、ClipCard、DigitRoll、FlashCut、Caption、FlatPanel、VerticalTicker、motion helpers'),
 ('shotcraft','music','音乐网格与节拍回测','audio_analyze','BPM 候选、0.5/1/2倍、瞬态、频段鼓点候选、RMS、切点误差；不冒充人工确认'),
 ('shotcraft','sounds','全部 BGM / 16 类 SFX','catalog','实际本地音频清单与归属说明'),
 ('shotcraft','dual-render','带 BGM / 无 BGM 对照','render','同一时间线保留 SFX，独立导出'),
 ('shotcraft','workbench','浏览器多轨编辑','vendor_workbench','镜头、转场、字幕、音效；参数 schema、颜色字号位置与时长'),
 ('shotcraft','jianying','剪映原生字幕与分轨','export_draft','clean plate 分镜底片＋单语原生文本＋独立人声/BGM/SFX；复杂动画烘焙'),
 ('shotcraft','publish','跨平台发布准备与校验','publish_prepare','平台/文案/定时/隐私/封面/AI 声明；先 dry run；已配置账号后由用户确认具体内容提交，重复请求只查询状态'),
 ('explainer','research','资料采集与事实出处','research','来源、原文、获取时间和 SHA256；待核清单，采集不等于验证'),
 ('explainer','chapters','长片章节 / HUD / 流程轨','storyboard','单章与多章、承上启下、贯穿案例、章节进度'),
 ('explainer','narration','口播与语义分镜','storyboard','空行画面单元、字幕块、停留预算、光效与运镜白名单'),
 ('explainer','tts','配音与外部音频','tts','已有晓晓原音优先；Edge/Kokoro/Kokoro ONNX/Piper/外部录音按依赖选择；独立缓存'),
 ('explainer','language','单语中文 / 英文','tts','单独语言选择与节奏预算，不生成双语字幕'),
 ('explainer','graphics','图元 / 光效 / 运镜 / 幕底','catalog','白线图解、紫色重点、点阵波/星点、GlitchIn/SoftIn、LightSweep/StageLine/GhostText'),
 ('explainer','qc','静态与成片量化 QC','qc','解码、帧率、音峰、画面采样、运动；主体/光/文字遮挡另列人工审查'),
 ('explainer','build','分组构建与迭代审查','package','构建记录、镜头覆盖、前30秒样片、修复/复验留档'),
 ('all','package','可迁移工程与许可证','package','源码/素材/事实/分镜/QA 哈希清单；保留独立对照'),
]

CONFLICTS = [
 {'id':'narration','label':'人声选择','options':['existing','silent','edge','kokoro','kokoro_onnx','piper','external'],'default':'existing','note':'夜曲原生无旁白，讲解片有旁白；按输出选择，不修改已有音频'},
 {'id':'world','label':'视觉世界','options':['current','nocturne','fable','explainer','product'],'default':'current'},
 {'id':'aspect','label':'画幅','options':['portrait','landscape'],'default':'portrait','note':'Vibe Canvas 原引擎只支持横屏；Remotion 融合支持竖屏适配'},
 {'id':'workflow','label':'制作模式','options':['template','autonomous','guided'],'default':'guided'},
 {'id':'backdrop','label':'背景','options':['blurred-real','dots','stars','plain'],'default':'blurred-real'},
 {'id':'timeline','label':'时间策略','options':['speech','beat','manual'],'default':'speech','note':'保留完整口播；卡点可调整镜头时长，不删用户文案'},
 {'id':'motion','label':'人物','options':['realistic-halfbody','centered','fixed-mouth','hidden'],'default':'realistic-halfbody','note':'最终选定写实设备与半身版；中央固定嘴鼻版仍独立保留'},
 {'id':'caption','label':'单语字幕','options':['zh','en','none'],'default':'zh'},
 {'id':'signature','label':'片尾署名','options':['none','attribution'],'default':'none','note':'可选署名，不覆盖任何许可证文件'},
]

def build_catalog(vendor):
    vendor = Path(vendor)
    base = vendor / 'video-shotcraft-main'
    if not (base / 'gallery/api/library.json').is_file():
        base = Path(__file__).resolve().parents[1] / 'work/native/shotcraft'
    library = base / 'gallery/api/library.json'
    if not library.is_file():
        raise FileNotFoundError('Shotcraft 模板目录缺失：请检查 work/native/shotcraft/gallery/api/library.json')
    data = json.loads(library.read_text(encoding='utf-8-sig'))
    cards = data.get('cards', [])
    for card in cards:
        source = base / card.get('source', '')
        card['sourcePath'] = str(source)
        card['sourceAvailable'] = source.is_file()
        if source.is_file():
            text = source.read_text(encoding='utf-8')
            import re
            card['demoReferences'] = list(dict.fromkeys(re.findall(r'(?:demos|assets/lib)/[\w./-]+\.(?:tsx?|jsx?)', text)))
        for style in card.get('styles', []):
            relative = style.get('media', {}).get('url', '').split('?')[0].removeprefix('./')
            style['localMedia'] = str(base / 'gallery' / relative)
            style['localMediaAvailable'] = (base / 'gallery' / relative).is_file()
    assets = []
    if (base / 'assets').exists():
        for file in sorted((base / 'assets').rglob('*')):
            if file.is_file() and file.suffix.lower() in {'.mp3','.wav','.ogg','.tsx','.ts','.md'}:
                assets.append({'name':file.name,'path':str(file),'relative':file.relative_to(base).as_posix(),'bytes':file.stat().st_size})
    references = []
    for repo in sorted(vendor.glob('*')):
        for folder in ('references','reference'):
            for file in sorted((repo / folder).glob('*.md')):
                references.append({'skill':repo.name,'name':file.stem,'path':str(file)})
    return {'features':[dict(zip(('skill','id','label','action','detail'), f)) for f in FEATURES],
            'conflicts':CONFLICTS,'cards':cards,'stats':data.get('stats',{}),'assets':assets,'references':references,
            'excluded':['bilingual-subtitles'], 'limits':['外部配音/模型/第三方账号按本机依赖启用','剪映 Windows 打开兼容性需在实际剪映验证','原生复杂动画不自动转换为剪映关键帧']}
