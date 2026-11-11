"""Gate caption OCR on useful speech and matching subtitle evidence."""
import difflib
import re


def usable_speech(segments):
    result = []
    for segment in segments:
        text = re.sub(r'[^\w\u4e00-\u9fff]', '', str(segment.get('text') or ''))
        event = segment.get('speech_event')
        if len(text) < 2 or event == 'non_speech':
            continue
        if float(segment.get('no_speech_prob') or 0) > .6:
            continue
        confidence = segment.get('confidence')
        if confidence is not None and float(confidence) < .4:
            continue
        if event == 'speech' or float(segment.get('end') or 0) > float(segment.get('start') or 0):
            result.append(segment)
    return result


def matching_captions(observations, speech):
    result = []
    for item in observations:
        box = item.get('bbox') or {}
        if box.get('y', 0) + box.get('height', 0) / 2 < .55 or item.get('sample_count', 1) < 2:
            continue
        visual = re.sub(r'[^\w\u4e00-\u9fff]', '', item.get('text', '')).lower()
        if len(visual) < 2:
            continue
        for spoken in speech:
            start, end = float(spoken.get('start') or 0), float(spoken.get('end') or 0)
            if end > start and (start > float(item['end']) + 1.25 or end < float(item['start']) - 1.25):
                continue
            words = re.sub(r'[^\w\u4e00-\u9fff]', '', spoken.get('text', '')).lower()
            match = difflib.SequenceMatcher(None, visual, words).find_longest_match()
            if words and (visual in words or words in visual or match.size / len(visual) >= .5):
                result.append(item)
                break
    return result


def caption_probe_times(speech, duration):
    times = set()
    for segment in speech:
        start, end = float(segment.get('start') or 0), float(segment.get('end') or 0)
        if end <= start:
            start, end = 0, duration
        for fraction in (.15, .5, .8):
            time = min(max(0, duration - .05), start + (end - start) * fraction)
            times.add(round(time, 3))
            times.add(round(min(duration - .01, time + .75), 3))
    ordered = sorted(times)
    if len(ordered) > 18:
        # Keep paired samples so repeated text can be distinguished from a single sign.
        anchors = ordered[::2]
        chosen = [anchors[round(i * (len(anchors) - 1) / 8)] for i in range(9)]
        return sorted({t for a in chosen for t in (a, min(duration - .01, a + .75))})
    return ordered
