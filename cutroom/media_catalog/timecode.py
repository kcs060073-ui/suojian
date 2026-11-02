"""P1-E 质量保障：时间码一致性 + 非破坏性调色参数。

时间码一致性：在代理/接触表/片段上选的切点，必须能精准对回原片。
这里校验"片段时间码、预览产物指向的区间"都落在原片时长内，避免剪错位置。

非破坏性调色：调色参数只存 color_params 表，渲染阶段再套用，
绝不把调整烤进原片或代理（代理用于看，原片用于剪）。
"""

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .preview import _duration_of
from .tools import default_ffprobe_search_roots, find_ffprobe


def _probe_proxy_duration(
    path: Path,
    *,
    ffprobe: Path | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> float:
    binary = ffprobe or find_ffprobe(
        os.environ,
        search_roots=default_ffprobe_search_roots(),
    )
    completed = runner(
        [
            str(binary),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "ffprobe 代理时长探测失败")[-2000:])
    try:
        return float(completed.stdout.strip())
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeError("ffprobe 未返回可解析的代理时长") from error


def verify_timecode_consistency(
    db,
    asset_id: str,
    *,
    probe_duration: Callable[[Path], float | None] | None = None,
    tolerance_sec: float = 0.25,
) -> dict:
    duration = _duration_of(db, asset_id)
    if duration is None:
        return {"asset_id": asset_id, "ok": False, "reason": "原片时长未知"}
    problems: list[str] = []
    rows = db.connection.execute(
        "SELECT segment_id, start_sec, end_sec FROM segments WHERE asset_id=?",
        (asset_id,),
    ).fetchall()
    for r in rows:
        if r["start_sec"] < 0 or r["end_sec"] > duration + 0.001 or r["start_sec"] >= r["end_sec"]:
            problems.append(
                f"片段 {r['segment_id']} 时间码越界："
                f"{r['start_sec']}–{r['end_sec']} 超出 [0, {duration:.2f}]"
            )
    proxy_duration = None
    proxy_row = db.connection.execute(
        "SELECT rel_path FROM preview_cache WHERE asset_id=? AND kind='proxy'",
        (asset_id,),
    ).fetchone()
    if proxy_row is not None:
        proxy_path = db.path.parent / "cache" / proxy_row["rel_path"]
        duration_probe = probe_duration or _probe_proxy_duration
        proxy_duration = duration_probe(proxy_path)
        if proxy_duration is None:
            problems.append("代理时长无法确定")
        else:
            difference = abs(float(proxy_duration) - duration)
            allowed = max(tolerance_sec, duration * 0.005)
            if difference > allowed:
                problems.append(
                    "代理与原片时长偏差"
                    f" {difference:.3f}s 超过容差 {allowed:.3f}s"
                )
    return {
        "asset_id": asset_id,
        "duration": duration,
        "segments": len(rows),
        "proxy_duration": proxy_duration,
        "ok": not problems,
        "problems": problems,
    }


def set_color_params(db, asset_id: str, params: dict) -> dict:
    allowed = {"brightness", "contrast", "saturation", "hue", "gamma"}
    cleaned = {k: float(v) for k, v in params.items() if k in allowed}
    if not cleaned:
        raise ValueError("调色参数必须包含 brightness/contrast/saturation/hue/gamma 之一")
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO color_params(asset_id, params_json)
            VALUES(?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
              params_json=excluded.params_json, created_at=datetime('now')
            """,
            (asset_id, json.dumps(cleaned, ensure_ascii=False)),
        )
    return {"asset_id": asset_id, "params": cleaned}


def get_color_params(db, asset_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT params_json FROM color_params WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["params_json"])
    except json.JSONDecodeError:
        return None
