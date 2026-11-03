"""Local motion measurements and source-window selection, independent of Cutroom."""
import hashlib
import json
import math
from pathlib import Path

from .camera_motion import detect_camera_motion

MOTIONS = {'static', 'pan_left', 'pan_right', 'pan_up', 'pan_down', 'zoom_in', 'zoom_out', 'unknown'}


def motion_match(wanted, measured):
    kind = measured.get('type', 'unknown')
    if kind == 'unknown' or measured.get('confidence', 0) < .45:
        return 0, 'unverified'
    if kind == wanted:
        return 3, 'exact'
    if kind.split('_')[0] == wanted.split('_')[0] and wanted != 'unknown':
        return 1, 'same-family'
    return 0, 'available-substitute'


def scan_folder(folder, *, window_seconds=4.0):
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError('素材文件夹不存在')
    if not math.isfinite(window_seconds) or not .5 <= window_seconds <= 30:
        raise ValueError('分析窗口必须为 0.5～30 秒')
    import cv2
    windows, errors = [], []
    for file in sorted(folder.iterdir()):
        if not file.is_file() or file.suffix.lower() not in {'.mp4', '.mov', '.mkv', '.avi', '.m4v'}:
            continue
        try:
            cap = cv2.VideoCapture(str(file))
            duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / max(cap.get(cv2.CAP_PROP_FPS), 1)
            cap.release()
            with file.open('rb') as handle:
                sha = hashlib.file_digest(handle, 'sha256').hexdigest()
            start = 0.0
            while duration - start >= .5:
                end = min(duration, start + window_seconds)
                measurement = detect_camera_motion(file, start, end)
                windows.append({'source': str(file), 'source_sha256': sha,
                                'source_start': start, 'source_end': end, 'motion': measurement})
                start = end
        except (ValueError, OSError) as error:
            errors.append({'file': file.name, 'error': str(error)})
    return {'schema': 'suojian-motion-index-1', 'folder': str(folder), 'windows': windows,
            'errors': errors, 'visual_accepted': False}


def plan_motion_shots(reference_shots, index, *, ratio='9:16'):
    if ratio not in {'9:16', '16:9'}:
        raise ValueError('画幅必须为 9:16 或 16:9')
    clips, used, cursor = [], {}, 0.0
    for number, shot in enumerate(reference_shots):
        wanted = shot.get('motion', 'unknown')
        if isinstance(wanted, dict):
            wanted = wanted.get('type', 'unknown')
        duration = float(shot['duration'])
        if wanted not in MOTIONS or not math.isfinite(duration) or duration <= 0:
            raise ValueError('镜头运镜类型或时长无效')
        eligible = [w for w in index['windows'] if w['source_end'] - w['source_start'] >= duration - .001]
        if not eligible:
            raise ValueError(f'第 {number + 1} 镜缺少足够长的真实素材窗口')
        def score(window):
            measured = window['motion']
            orientation = (measured.get('width', 0) > measured.get('height', 0)) == (ratio == '16:9')
            return (motion_match(wanted, measured)[0], orientation,
                    -used.get(window['source'], 0), measured.get('confidence', 0))
        selected = max(eligible, key=score)
        _, match = motion_match(wanted, selected['motion'])
        clips.append({'shot_id': shot.get('id', f'shot-{number + 1:03}'), 'start': cursor,
                      'end': cursor + duration, 'source': selected['source'],
                      'source_start': selected['source_start'], 'source_end': selected['source_start'] + duration,
                      'source_sha256': selected['source_sha256'], 'wanted_motion': wanted,
                      'measured_motion': selected['motion'], 'match': match,
                      'reason': '使用实测窗口；同类或下位替代均明确记录，不合成不存在的运镜'})
        used[selected['source']] = used.get(selected['source'], 0) + 1
        cursor += duration
    return {'schema': 'suojian-motion-plan-1', 'engine': 'suojian-local', 'ratio': ratio,
            'duration': cursor, 'clips': clips, 'visual_accepted': False}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('folder')
    parser.add_argument('--output', required=True)
    parser.add_argument('--reference')
    parser.add_argument('--ratio', default='9:16')
    args = parser.parse_args()
    result = scan_folder(args.folder)
    if args.reference:
        result = plan_motion_shots(json.loads(Path(args.reference).read_text('utf-8'))['shots'], result, ratio=args.ratio)
    with Path(args.output).open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
