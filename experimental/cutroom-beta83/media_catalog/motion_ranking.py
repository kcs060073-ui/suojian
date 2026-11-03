"""Bounded direction preference among equally supported, usable choices."""
from __future__ import annotations

import math
from .storyboard_motion import SIMPLE_DIRECTIONS


def adjacent_slots(timing):
    """Never collapse timeline gaps or infer adjacency from unordered requests."""
    if not any('start' in item or 'end' in item for item in timing):
        return [(i, i+1) for i in range(len(timing)-1)]
    def valid(item):
        start, end = item.get('start'), item.get('end')
        return (all(type(v) in (int, float) and math.isfinite(v) for v in (start, end))
                and 0 <= start < end and abs(end-start-float(item['dur'])) < 1e-6)
    if not all(valid(item) for item in timing):
        return []
    order = sorted(range(len(timing)), key=lambda i: (timing[i]['start'], i))
    return [(i, j) for i, j in zip(order, order[1:])
            if abs(timing[i]['end']-timing[j]['start']) < 1e-6]


def motion_edges(item, duration):
    """Project direction onto the displayed trim, not its unused source tail."""
    if item is None or item.get('media_kind') != 'video':
        return None, None
    observation = item.get('motion_observation') or {}
    if observation.get('status') != 'observed':
        return None, None
    start = round((item.get('source_start') or 0)*1e6)
    end = start+round(duration*1e6)
    first, last = observation.get('sourceStartUs'), observation.get('sourceEndUs')
    if (type(first) is not int or type(last) is not int
            or not first <= start < end <= last):
        return None, None
    camera = observation.get('cameraMotion')
    entry, exit_ = observation.get('entryMotion'), observation.get('exitMotion')
    uniform = camera if camera in SIMPLE_DIRECTIONS and all(
        edge is None or edge == camera for edge in (entry, exit_)) else None
    entry = (entry if entry is not None else uniform) if start == first else uniform
    exit_ = (exit_ if exit_ is not None else uniform) if end == last else uniform
    return (entry if entry in SIMPLE_DIRECTIONS else None,
            exit_ if exit_ in SIMPLE_DIRECTIONS else None)


def _variation(left, right):
    return int(left[1] is not None and right[0] is not None and left[1] != right[0])


def preference_for_slot(item, index, selected, timing, *, context=None):
    """Known changes at actual immediate neighbors; unknown is never a bonus."""
    sequence, times, positions = _timeline(selected, timing, context)
    position = positions[index]
    sequence[position] = item
    return sum(_variation(motion_edges(sequence[i],float(times[i]['dur'])),
                          motion_edges(sequence[j],float(times[j]['dur'])))
               for i, j in adjacent_slots(times) if position in (i,j))


def _timeline(selected, timing, context):
    if context is None:
        return list(selected), timing, list(range(len(selected)))
    sequence = list(context['items'])
    positions = context['positions']
    if len(positions) != len(selected) or len(set(positions)) != len(positions):
        raise ValueError('MOTION_CONTEXT_INVALID: slot positions must be unique')
    if len(sequence) != len(context['timing']) or any(type(i) is not int or not 0 <= i < len(sequence) for i in positions):
        raise ValueError('MOTION_CONTEXT_INVALID: timeline positions are invalid')
    for index, item in zip(positions, selected, strict=True):
        if item is not None:
            sequence[index] = item
    return sequence, context['timing'], positions


def fixed_reservations(context):
    if context is None:
        return []
    editable=set(context['positions'])
    return [(item,float(context['timing'][i]['dur']))
            for i,item in enumerate(context['items'])
            if i not in editable and item is not None and item.get('identity_key')]



def retained_reservations(selected, timing, context, *, except_index=None):
    """Unreplaced editable slots keep their existing source in a refresh."""
    if context is None:
        return []
    return [(context['items'][position], float(timing[i]['dur']))
            for i, position in enumerate(context['positions'])
            if i != except_index and selected[i] is None
            and context['items'][position] is not None
            and context['items'][position].get('identity_key')]


def subsequence_context(selected, timing, indices, context=None):
    sequence, times, positions = _timeline(selected, timing, context)
    return dict(items=sequence, timing=times, positions=[positions[i] for i in indices])


def prefer_motion(selected, options, timing, conflicts, handle, *, context=None):
    """Strictly improve adjacent direction changes without changing slot merit.

    Only one-choice replacements and equal-merit swaps are attempted. Every
    accepted move adds at least one known direction change, so the number of
    passes is bounded by the number of actual adjacent edges, not pool size.
    """
    selected = list(selected)
    sequence, times, positions = _timeline(selected, timing, context)
    edges = adjacent_slots(times)
    if not edges:
        return selected
    durations = [float(item['dur']) for item in timing]
    def priority(score, item):
        return (score, (item.get('match_evidence') or {}).get('score'),
                item.get('source_mode'), item.get('usage_status'),
                (item.get('quality_evidence') or {}).get('penalty', 0))
    def key(item):
        return (item['asset_id'], item['media_kind'], item.get('source_start'),
                item.get('source_end'), item.get('segment_id'))
    peers, by_key = [], []
    for current, shot in zip(selected, options, strict=True):
        if current is None:
            peers.append([]); by_key.append({}); continue
        current_score = next(score for score, item in shot if item is current)
        rank = priority(current_score, current)
        choices = [item for score, item in shot if priority(score,item) == rank]
        peers.append(choices)
        by_key.append({key(item): item for item in reversed(choices)})
    def gain(changes):
        new_edges = {positions[i]: motion_edges(item, durations[i]) for i,item in changes.items()}
        delta = 0
        for a,b in edges:
            if a not in new_edges and b not in new_edges:
                continue
            left = motion_edges(sequence[a],float(times[a]['dur']))
            right = motion_edges(sequence[b],float(times[b]['dur']))
            delta += _variation(new_edges.get(a,left),new_edges.get(b,right))-_variation(left,right)
        return delta
    def valid(changes):
        return not any(other is not None and i != j and conflicts(
            (item,durations[i]), (changes.get(j,other),durations[j]), handle)
            for i,item in changes.items() for j,other in enumerate(selected))
    for _ in range(len(edges)):
        change = None
        for i, choices in enumerate(peers):
            for item in choices:
                proposed = {i:item}
                if gain(proposed) > 0 and valid(proposed):
                    change = proposed; break
            if change is not None: break
        if change is None:
            for i, a in enumerate(selected):
                if a is None: continue
                for j in range(i+1,len(selected)):
                    b=selected[j]
                    if b is None: continue
                    new_a, new_b = by_key[i].get(key(b)), by_key[j].get(key(a))
                    if new_a is None or new_b is None: continue
                    proposed={i:new_a,j:new_b}
                    if gain(proposed)>0 and valid(proposed):
                        change=proposed; break
                if change is not None: break
        if change is None: break
        for i,item in change.items():
            selected[i]=item; sequence[positions[i]]=item
    return selected


def context_for_cards(db, state, editable):
    """Read fixed neighbors; only the named editable slots may be reallocated."""
    from .rough_cut import _source_window
    from .visual_observations import visual_observation_for_shot
    cards=state['cards']
    positions={card['card_id']:i for i,card in enumerate(cards)}
    items=[]; timing=[]
    for card in cards:
        duration=float(card['narrative_end'])-float(card['narrative_start'])
        timing.append(dict(dur=duration,start=card['narrative_start'],end=card['narrative_end']))
        item=None
        if card.get('asset_id'):
            try:
                start,_end=_source_window(db.connection,state,card)
                kind=db.connection.execute('SELECT media_kind FROM assets WHERE asset_id=?',(card['asset_id'],)).fetchone()[0]
                visual=visual_observation_for_shot(db,dict(card,resolved_source_start=start,resolved_source_end=start+duration))
                asset=db.connection.execute('SELECT full_sha256 FROM assets WHERE asset_id=?',(card['asset_id'],)).fetchone()
                location=db.connection.execute("SELECT file_identity FROM asset_locations WHERE asset_id=? AND state='available' AND file_identity IS NOT NULL AND file_identity<>'' LIMIT 1",(card['asset_id'],)).fetchone()
                identity=('sha:'+asset[0] if asset[0] else 'file:'+location[0] if location else 'asset:'+card['asset_id'])
                item=dict(asset_id=card['asset_id'],media_kind=kind,source_start=start,
                          identity_key=identity,motion_observation=visual)
            except (ValueError,KeyError):
                pass  # An unresolved fixed shot provides no directional evidence.
        items.append(item)
    return dict(items=items,timing=timing,positions=[positions[c['card_id']] for c in editable])
