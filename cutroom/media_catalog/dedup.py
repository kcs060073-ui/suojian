"""P3-M2 重复检测（镜头级近重复）。

用 PIL + numpy 自算力感知哈希（ahash / dhash），不依赖 cv2 / imagehash。
抽帧依赖 ffmpeg（find_ffmpeg）；哈希与分组算法本身纯 Python，可用合成帧独立验证。

输出：跨片段的近重复帧分组（汉明距离 <= threshold），供剪辑时规避重复镜头。
"""

import json
import re
import subprocess
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

from .tools import ToolNotFound, find_ffmpeg

# 默认：每片段均匀抽 12 帧；汉明距离阈值（0-64，越大越宽松）
DEFAULT_FRAMES = 12
DEFAULT_THRESHOLD = 10  # ahash 64 位中允许的差异位数


# --------------------------------------------------------------------------- #
# 哈希
# --------------------------------------------------------------------------- #
def ahash(img: Image.Image, size: int = 8) -> int:
    """平均哈希：灰度缩放后二值化，返回 64 位整数。"""
    g = img.convert("L").resize((size, size), Image.BILINEAR)
    arr = np.asarray(g, dtype=np.float64)
    mean = arr.mean()
    bits = arr > mean
    return _bits_to_int(bits.flatten())


def dhash(img: Image.Image, size: int = 8) -> int:
    """差值哈希：相邻列亮度梯度。返回 (size-1)*size 位整数。"""
    g = img.convert("L").resize((size + 1, size), Image.BILINEAR)
    arr = np.asarray(g, dtype=np.float64)
    diff = arr[:, 1:] > arr[:, :-1]
    return _bits_to_int(diff.flatten())


def _bits_to_int(bits: np.ndarray) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | (1 if b else 0)
    return int(v)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count() if hasattr(int, "bit_count") else bin(a ^ b).count("1")


# --------------------------------------------------------------------------- #
# 抽帧（ffmpeg）
# --------------------------------------------------------------------------- #
def _sample_frames(video: Path, out_dir: Path, n: int, ff) -> tuple[float, list[tuple[Path, float]]]:
    """Seek to each bin midpoint and retain the decoded frame's actual PTS."""
    if n < 1:
        raise ValueError("抽帧数量必须大于零")
    duration, start_time = _video_timing(video)
    out_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    for i in range(n):
        at = duration * (i + 0.5) / n
        output = out_dir / f"frame_{i:03d}.png"
        proc = subprocess.run(
            [str(ff), "-y", "-hide_banner", "-loglevel", "info",
             "-ss", f"{at:.9f}", "-copyts", "-i", str(video),
             "-vf", "scale=160:160,showinfo", "-vsync", "0",
             "-frames:v", "1", str(output)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, check=False,
        )
        stderr = proc.stderr.decode('utf-8', errors='replace') if isinstance(proc.stderr, bytes) else (proc.stderr or '')
        timestamps = re.findall(r"pts_time:([0-9.eE+-]+)", stderr)
        if proc.returncode != 0 or not output.is_file() or not timestamps:
            raise RuntimeError(f"重复检测抽帧失败（{at:.3f}s）：{stderr[-1000:]}")
        position = float(timestamps[0]) - start_time
        if not 0 <= position <= duration:
            raise RuntimeError('抽帧时间戳超出视频时长')
        samples.append((output, position))
    return duration, samples


def extract_frames(video: Path, out_dir: Path, n: int = DEFAULT_FRAMES, ff=None) -> list[Path]:
    ff = ff or find_ffmpeg(__import__('os').environ)
    return [path for path, _ in _sample_frames(video, out_dir, n, ff)[1]]


def _video_timing(video: Path) -> tuple[float, float]:
    from .tools import find_ffprobe
    fp = find_ffprobe(__import__('os').environ)
    out = subprocess.run(
        [str(fp), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=duration,start_time:format=duration,start_time", "-of", "json", str(video)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
    )
    try:
        data = json.loads(out.stdout)
        stream = (data.get('streams') or [{}])[0]
        fmt = data.get('format') or {}
        duration = float(stream.get('duration') or fmt.get('duration'))
        start_time = float(stream.get('start_time') or fmt.get('start_time') or 0)
    except (ValueError, TypeError, AttributeError, IndexError) as error:
        raise RuntimeError("无法读取真实视频时长") from error
    if (out.returncode != 0 or not __import__('math').isfinite(duration)
            or not __import__('math').isfinite(start_time) or duration <= 0):
        raise RuntimeError("无法读取有效的视频时长")
    return duration, start_time


def _duration(video: Path) -> float:
    return _video_timing(video)[0]


def video_hashes(video: Path, ff, n: int = DEFAULT_FRAMES, tmp: Path | None = None) -> list[dict]:
    owned = tmp is None
    tmp = tmp or (Path(tempfile_dir()) / f"dedup_{uuid.uuid4().hex}")
    try:
        duration, frames = _sample_frames(video, tmp, n, ff)
        result = []
        for i, (path, pts) in enumerate(frames):
            with Image.open(path) as img:
                result.append({
                    "frame": i,
                    "t_sec": pts,
                    "t_rel": round(pts / duration, 3),
                    "ahash": ahash(img),
                    "dhash": dhash(img),
                })
        return result
    finally:
        if owned:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 分组
# --------------------------------------------------------------------------- #
def find_groups(samples: list[dict], threshold: int = DEFAULT_THRESHOLD) -> list[list[dict]]:
    """samples: [{src, frame, t_rel, ahash, dhash}, ...]；
    用并查集按 ahash 汉明距离合并近重复，返回分组列表。"""
    parent = list(range(len(samples)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(samples)):
        for j in range(i + 1, len(samples)):
            if hamming(samples[i]["ahash"], samples[j]["ahash"]) <= threshold:
                union(i, j)
    groups: dict[int, list[dict]] = {}
    for i, s in enumerate(samples):
        groups.setdefault(find(i), []).append(s)
    return [g for g in groups.values() if len(g) > 1]


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def scan_videos(videos: list[str], threshold: int = DEFAULT_THRESHOLD,
                n: int = DEFAULT_FRAMES, ff=None) -> dict:
    if ff is None:
        ff = find_ffmpeg(__import__("os").environ)
    samples: list[dict] = []
    for v in videos:
        p = Path(v)
        if not p.is_file():
            continue
        try:
            for h in video_hashes(p, ff, n):
                samples.append({"src": str(p), **h})
        except ToolNotFound:
            continue
    groups = find_groups(samples, threshold)
    return {
        "video_count": len(videos),
        "frame_count": len(samples),
        "group_count": len(groups),
        "threshold": threshold,
        "groups": [
            {
                "size": len(g),
                "members": [
                    {"src": m["src"], "frame": m["frame"], "t_rel": m["t_rel"]}
                    for m in g
                ],
            }
            for g in groups
        ],
    }


def run_dedup(db, task_id: str | None, videos: list[str],
              scope: str, threshold: int = DEFAULT_THRESHOLD) -> dict:
    from .tools import find_ffmpeg
    ff = find_ffmpeg(__import__("os").environ)
    result = scan_videos(videos, threshold, ff=ff)
    report = {
        "report_id": f"dedup-{uuid.uuid4().hex[:12]}",
        "task_id": task_id,
        "scope": scope,
        **result,
        "created_at": _now(),
    }
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO dedup_reports(report_id, task_id, scope, video_count, "
            "frame_count, group_count, report_json) VALUES(?,?,?,?,?,?,?)",
            (report["report_id"], task_id, scope, result["video_count"],
             result["frame_count"], result["group_count"],
             json.dumps(result, ensure_ascii=False)),
        )
    return report


def list_reports(db, task_id: str | None = None) -> list[dict]:
    sql = "SELECT report_id, task_id, scope, video_count, frame_count, group_count, created_at FROM dedup_reports"
    args: tuple = ()
    if task_id:
        sql += " WHERE task_id=?"
        args = (task_id,)
    sql += " ORDER BY created_at DESC LIMIT 50"
    return [dict(r) for r in db.connection.execute(sql, args).fetchall()]


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()
