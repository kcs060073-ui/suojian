"""Prefer reviewed scene/wardrobe matches and retain uninterrupted spoken phrases."""
import re
from collections import Counter

def primary_profile(options,profiles):
    coverage=Counter()
    for choices in options:
        keys={(profiles[c['asset_id']].get('setting'),profiles[c['asset_id']].get('outfit'))
              for c in choices if c['asset_id'] in profiles}
        coverage.update(keys)
    return coverage.most_common(1)[0][0] if coverage else None

def continuity_bonus(candidate,previous,primary,profiles):
    profile=profiles.get(candidate['asset_id'],{});bonus=0.
    if primary and (profile.get('setting'),profile.get('outfit'))==primary:bonus+=.08
    if previous:
        last=profiles.get(previous['asset_id'],{})
        if profile and last:
            if profile.get('setting')==last.get('setting'):bonus+=.035
            if profile.get('outfit')==last.get('outfit'):bonus+=.035
            if profile.get('backdrop')==last.get('backdrop'):bonus+=.015
        if candidate['asset_id']==previous['asset_id'] and 0<=candidate['source_start']-previous['source_end']<2:bonus+=.04
    return bonus

def merge_uninterrupted(cuts,transcripts,max_gap=1.):
    merged=[]
    for original in cuts:
        cut=dict(original)
        if merged:
            previous=merged[-1]
            gap=cut['source_start']-previous['source_end']
            words=transcripts[cut['asset_id']]['words']
            omitted=words[previous['word_last']+1:cut['word_first']] if cut['asset_id']==previous['asset_id'] else []
            safe_gap=not any(re.search(r'[\w\u4e00-\u9fff]',w['text']) for w in omitted)
            if cut['asset_id']==previous['asset_id'] and 0<=gap<=max_gap and safe_gap:
                offset=cut['source_start']-previous['source_start']
                for caption in cut.get('captions') or [{'text':cut['text'],'local_start':0.,'local_end':cut['source_end']-cut['source_start']}]:
                    previous['captions'].append({**caption,'local_start':caption['local_start']+offset,
                        'local_end':caption['local_end']+offset})
                previous.update(source_end=cut['source_end'],word_last=cut['word_last'],
                    text=previous['text']+'，'+cut['text'],spoken_text=previous['spoken_text']+'，'+cut['spoken_text'])
                continue
        if not cut.get('captions'):
            cut['captions']=[{'text':cut['text'],'local_start':0.,'local_end':cut['source_end']-cut['source_start']}]
        merged.append(cut)
    clock=0.
    for cut in merged:
        cut['target_start']=round(clock,3);cut['duration']=round(cut['source_end']-cut['source_start'],3)
        clock+=cut['duration']
    return merged
