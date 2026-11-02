"""Turn timed ASR into reviewable sentence ranges without guessing cut times."""
import difflib
import re


def split_sentences(transcript):
    words = transcript.get('words', [])
    chars, owners = [], []
    for i, word in enumerate(words):
        for char in word['text']:
            if char.isalnum():
                chars.append(char.lower())
                owners.append(i)
    raw = transcript.get('text', '')
    clean = ''.join(c.lower() for c in raw if c.isalnum())
    mapping = {}
    for block in difflib.SequenceMatcher(None, clean, ''.join(chars), autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = owners[block.b + offset]
    boundaries, position = set(), 0
    for char in raw:
        if char.isalnum():
            position += 1
        elif char in '，,。！？!?；;\n' and position and position - 1 in mapping:
            boundaries.add(mapping[position - 1])
    for i in range(len(words)-1):
        if words[i+1]['start'] - words[i]['end'] >= .8:
            boundaries.add(i)
    rows, first = [], 0
    for last in sorted(boundaries | ({len(words)-1} if words else set())):
        if last < first:
            continue
        text = ''.join(w['text'] for w in words[first:last+1]).strip()
        if re.search(r'[\w\u4e00-\u9fff]', text):
            rows.append({'first':first, 'last':last, 'text':text,
                         'start':words[first]['start'], 'end':words[last]['end']})
        first = last + 1
    return rows


def caption_ranges(words, first, last, max_length=16):
    result, start, count = [], first, 0
    for index in range(first, last + 1):
        length = len(re.sub(r'[\s，,。！？!?；;：:、]', '', words[index]['text']))
        if count and count + length > max_length:
            result.append({'first':start, 'last':index-1})
            start, count = index, 0
        count += length
    if count:
        result.append({'first':start, 'last':last})
    return result
