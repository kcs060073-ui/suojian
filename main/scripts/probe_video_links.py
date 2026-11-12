"""Probe public video imports without starting analysis or calling models.

Run from the repository root with --report PATH URL [URL ...].
--download-dir is optional and explicitly enables actual media downloads.
Reports exclude temporary signed playback URLs, cookies and request headers.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from suo_jian.ui.platform_video import extract_input_url, extract_platform_info, import_video_link, platform_name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('urls', nargs='+')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--download-dir', type=Path)
    args = parser.parse_args()
    rows = []
    for index, text in enumerate(args.urls, 1):
        started = time.monotonic()
        row = {'input': text, 'ok': False, 'downloaded': False}
        try:
            url = extract_input_url(text)
            row['platform'] = platform_name(url) or '其他公开视频'
            if args.download_dir:
                args.download_dir.mkdir(parents=True, exist_ok=True)
                # Refuse to overwrite an earlier experiment.
                target = args.download_dir / f'sample-{index:02d}-{time.time_ns()}.mp4'
                import_video_link(url, target)
                row.update(downloaded=True, file=str(target), bytes=target.stat().st_size)
                probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                    'format=duration:stream=codec_type,width,height', '-of', 'json', str(target)],
                    capture_output=True, text=True, encoding='utf-8', check=True, timeout=45)
                row['media'] = json.loads(probe.stdout)
                for position in (['-ss', '0'], ['-sseof', '-1']):
                    subprocess.run(['ffmpeg', '-v', 'error', *position, '-i', str(target),
                        '-t', '1', '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-'],
                        capture_output=True, check=True, timeout=45)
                row['opening_and_end_decode'] = 'passed'
            else:
                info = extract_platform_info(url)
                row['metadata'] = {key: info.get(key) for key in
                    ('id', 'title', 'duration', 'protocol', 'width', 'height')}
            row['ok'] = True
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            row['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
        row['elapsed_seconds'] = round(time.monotonic()-started, 2)
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != 'input'}, ensure_ascii=False), flush=True)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({'model_calls': 0, 'results': rows}, ensure_ascii=False, indent=2),
                               encoding='utf-8')
    return 0 if all(row['ok'] for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
