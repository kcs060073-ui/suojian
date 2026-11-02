from __future__ import annotations
import argparse
import json
from suo_jian.pipeline import analyze

parser = argparse.ArgumentParser(description="溯剪：MP4 剪辑结构分析 Demo")
parser.add_argument("video")
parser.add_argument("--output", default="results")
parser.add_argument("--subtitles", action="store_true")
parser.add_argument("--separate-audio", action="store_true", help="分离人声和音乐音轨；首次运行会下载模型")
args = parser.parse_args()
blueprint, path, skeleton = analyze(args.video, args.output, args.subtitles, args.separate_audio)
print(json.dumps({"blueprint": str(path), "jianying_skeleton": str(skeleton), "shots": len(blueprint["shots"])}, ensure_ascii=False, indent=2))
