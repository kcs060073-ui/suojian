"""Upgrade old demo-video replacements into independent card-motion parameters."""
import copy
import json
from pathlib import Path

EFFECTS=('none','spotlight-hero-card','deck-deal-flyin','card-stack','row-embed','aurora-bloom-bg-flip')
ALIASES={'depth-stack':'card-stack','exploded-view':'card-stack'}

def migrate(config,root):
    c=copy.deepcopy(config)
    if c.get('scenePack')!='jiading-script07':return c
    data=json.loads((Path(root)/'work/park-fusion/src/script07-data.json').read_text(encoding='utf-8'))
    c.setdefault('backgroundBlur',24);c.setdefault('backgroundBrightness',.64)
    for shot in c.get('shots',[]):
        base=next((s for s in data['scenes'] if shot.get('id')==f'jiading-{s["index"]+1}'),None)
        if not base:continue
        source=str(shot.get('source',''));recipe=shot.get('recipe','')
        if source.startswith('/media/generated-cards/'):
            job=Path(source).stem
            try:receipt=json.loads((Path(root)/'outputs/runs'/job/'job.json').read_text(encoding='utf-8'))['result']['receipt']
            except (OSError,ValueError,KeyError):receipt={}
            effect=ALIASES.get(receipt.get('id'),receipt.get('id'))
            if effect not in EFFECTS:continue  # Unknown native clips remain explicit media until a source adapter exists.
            shot.update(referencePreview=source,source='/media/'+base['footage'],sourceType='video',sourceOffset=base['from'],footageOffset=0,
                audioScene=base['index'],recipe=f'jiading-{base["index"]}',cardEffect=effect,cardMotion=False,
                asset='嘉定园区实拍 · '+base['label'],layerVersion=2)
        elif recipe in EFFECTS[1:] or recipe in ALIASES:
            shot.setdefault('cardEffect',ALIASES.get(recipe,recipe))
            shot['recipe']=shot.get('beforeCardRecipe') if str(shot.get('beforeCardRecipe','')).startswith('jiading-') else f'jiading-{base["index"]}'
            shot['cardMotion']=False;shot['layerVersion']=2
    return c
