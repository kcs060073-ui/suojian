"""P2-M5 自动质检（qa）引擎。

成片/样片渲染后跑一轮自动质检，覆盖：
- 视频：黑场 / 冻结 / 静音 / 削波（ffmpeg 滤镜探测）
- 工程一致性（DB）：素材依赖缺失 / 重复镜头 / 包装门禁 / 隐私增量
结果落库 qa_reports，验收门禁要求 "qa 0 阻断"（status=blocked 即未过）。

所有 ffmpeg/ffprobe 调用失败、视频不可解码或规格不符均失败关闭，阻断发布。
"""

import os
import re
import subprocess
import uuid
import json
import math
from fractions import Fraction
from pathlib import Path

from .rules import DEFAULT_PAYLOAD, rules_for_task
from .qa_evidence import check_loudness, check_subtitle_evidence, collect_qa_evidence
from .tools import ToolNotFound, default_ffprobe_search_roots, find_ffmpeg, find_ffprobe


class QADetectionError(RuntimeError):
    pass


def _run_ff(binary: Path, args: list[str], timeout: int = 180) -> str:
    """跑 ffmpeg/ffprobe，返回 stderr（诊断信息都在 stderr）。"""
    try:
        proc = subprocess.run(
            [str(binary), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise QADetectionError(f"检测超时：{binary.name}")
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "ignore")[-500:]
        raise QADetectionError(f"检测失败：{binary.name} code={proc.returncode} {detail}")
    return (proc.stderr or b"").decode("utf-8", "ignore")


def _parse_frame_rate(stream: dict) -> float | None:
    """Return the first valid positive avg/r frame rate."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if raw in (None, ""):
            continue
        try:
            rate = float(Fraction(str(raw)))
        except (ValueError, ZeroDivisionError):
            continue
        if math.isfinite(rate) and rate > 0:
            return rate
    return None


def _blocking(check: str, detail: str) -> dict:
    return {"check": check, "severity": "blocking", "ok": False, "detail": detail}


def _info(check: str, detail: str) -> dict:
    return {"check": check, "severity": "info", "ok": True, "detail": detail}


def _parse_ratio(value) -> float:
    text = str(value).strip()
    if not text:
        raise ValueError("empty ratio")
    try:
        if ":" in text:
            parts = text.split(":")
            if len(parts) != 2:
                raise ValueError("invalid ratio")
            ratio = float(Fraction(parts[0]) / Fraction(parts[1]))
        else:
            ratio = float(Fraction(text))
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("invalid ratio") from error
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("ratio must be positive")
    return ratio


def _parse_rotation(stream: dict) -> int:
    raw_values = []
    side_data = stream.get("side_data_list")
    if side_data is not None:
        if not isinstance(side_data, list):
            raise ValueError("side_data_list must be a list")
        for item in side_data:
            if not isinstance(item, dict):
                raise ValueError("side_data_list contains invalid item")
            if "rotation" in item:
                raw_values.append(item["rotation"])
    tags = stream.get("tags")
    if tags is not None:
        if not isinstance(tags, dict):
            raise ValueError("tags must be an object")
        if "rotate" in tags:
            raw_values.append(tags["rotate"])
    if not raw_values:
        return 0

    rotations = []
    for raw in raw_values:
        try:
            value = float(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("rotation is not numeric") from error
        if not math.isfinite(value):
            raise ValueError("rotation is not finite")
        quarter_turn = round(value / 90)
        if abs(value - quarter_turn * 90) > 1e-6:
            raise ValueError("rotation must be a multiple of 90")
        rotations.append((quarter_turn % 4) * 90)
    if len(set(rotations)) != 1:
        raise ValueError("rotation metadata conflicts")
    return rotations[0]


def _check_video_spec(
    ffprobe: Path, video: Path, timeout: int = 60, *, expected_ratio="9:16",
) -> tuple[list[dict], dict]:
    """Probe and validate against the intended canvas, never the measured output."""
    dimensions = {"9:16":(1080,1920), "16:9":(1920,1080), "1:1":(1080,1080), "4:3":(1440,1080), "3:4":(1080,1440)}
    if expected_ratio not in dimensions:
        raise QADetectionError("不支持的目标画幅：" + str(expected_ratio))
    min_width, min_height = dimensions[expected_ratio]
    target_ratio = min_width / min_height
    try:
        proc = subprocess.run(
            [
                str(ffprobe), "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(video),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise QADetectionError("ffprobe 视频规格检测超时") from error
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "ignore")[-500:]
        raise QADetectionError(f"ffprobe 无法读取成片：{detail}")
    try:
        payload = json.loads((proc.stdout or b"{}").decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QADetectionError("ffprobe 返回非法 JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise QADetectionError("ffprobe JSON 缺少 streams")

    streams = payload["streams"]
    video_stream = next(
        (stream for stream in streams
         if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (stream for stream in streams
         if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    issues: list[dict] = []
    spec = {
        "codec": None,
        "width": None,
        "height": None,
        "sample_aspect_ratio": None,
        "display_aspect_ratio": None,
        "rotation": None,
        "display_width": None,
        "display_height": None,
        "aspect_ratio": None,
        "fps": None,
        "duration": None,
    }

    if video_stream is None:
        issues.append(_blocking("video_stream", "成片缺少视频流"))
        issues.append(_blocking("video_evidence", "无法建立成片视频流证据"))
    else:
        issues.append(_info("video_stream", "成片包含视频流"))
        issues.append(_info("video_evidence", "ffprobe 已读取成片视频证据"))

        codec = str(video_stream.get("codec_name") or "").lower()
        spec["codec"] = codec or None
        if codec not in {"h264", "hevc"}:
            issues.append(_blocking(
                "video_codec", f"视频编码必须为 H.264/HEVC，当前为 {codec or '缺失'}",
            ))
        else:
            issues.append(_info("video_codec", f"视频编码 {codec} 可发布"))

        width = video_stream.get("width")
        height = video_stream.get("height")
        dimensions_valid = (
            isinstance(width, int) and not isinstance(width, bool) and width > 0
            and isinstance(height, int) and not isinstance(height, bool) and height > 0
        )
        if not dimensions_valid:
            issues.append(_blocking("resolution", "视频宽高字段缺失或非法"))
            issues.append(_blocking("aspect_ratio", "无法计算 9:16 显示画幅"))
        else:
            spec["width"] = width
            spec["height"] = height

        raw_sar = video_stream.get("sample_aspect_ratio")
        if raw_sar in (None, ""):
            sample_ratio = 1.0
            issues.append(_info(
                "pixel_aspect_ratio", "sample_aspect_ratio 缺失，按常见方形像素 1:1",
            ))
        else:
            try:
                sample_ratio = _parse_ratio(raw_sar)
            except ValueError:
                sample_ratio = None
                issues.append(_blocking(
                    "pixel_aspect_ratio",
                    f"sample_aspect_ratio 非法：{raw_sar!r}",
                ))
            else:
                issues.append(_info(
                    "pixel_aspect_ratio",
                    f"sample_aspect_ratio={raw_sar}",
                ))
        spec["sample_aspect_ratio"] = sample_ratio

        try:
            rotation = _parse_rotation(video_stream)
        except ValueError as error:
            rotation = None
            issues.append(_blocking("rotation", f"旋转元数据非法：{error}"))
        else:
            issues.append(_info("rotation", f"显示旋转 {rotation}°"))
        spec["rotation"] = rotation

        raw_dar = video_stream.get("display_aspect_ratio")
        if raw_dar in (None, ""):
            display_ratio_metadata = None
        else:
            try:
                display_ratio_metadata = _parse_ratio(raw_dar)
            except ValueError:
                display_ratio_metadata = None
                issues.append(_blocking(
                    "display_aspect_ratio",
                    f"display_aspect_ratio 非法：{raw_dar!r}",
                ))
            else:
                issues.append(_info(
                    "display_aspect_ratio",
                    f"display_aspect_ratio={raw_dar}",
                ))
        spec["display_aspect_ratio"] = display_ratio_metadata

        if dimensions_valid and rotation is not None:
            if rotation in (90, 270):
                display_width, display_height = height, width
            else:
                display_width, display_height = width, height
            spec["display_width"] = display_width
            spec["display_height"] = display_height
            if display_width < min_width or display_height < min_height:
                issues.append(_blocking(
                    "resolution",
                    f"显示分辨率 {display_width}x{display_height} 低于 {min_width}x{min_height}",
                ))
            else:
                issues.append(_info(
                    "resolution", f"显示分辨率 {display_width}x{display_height} 达标",
                ))

        if dimensions_valid and sample_ratio is not None:
            coded_display_ratio = (width * sample_ratio) / height
            if (
                display_ratio_metadata is not None
                and abs(display_ratio_metadata - coded_display_ratio) > 0.02
            ):
                issues.append(_blocking(
                    "display_aspect_ratio",
                    "display_aspect_ratio 与 coded width/height × SAR 不一致",
                ))
            if rotation is not None:
                final_ratio = (
                    1 / coded_display_ratio
                    if rotation in (90, 270)
                    else coded_display_ratio
                )
                spec["aspect_ratio"] = final_ratio
                if abs(final_ratio - target_ratio) > 0.02:
                    issues.append(_blocking(
                        "aspect_ratio",
                        f"显示画幅比例 {final_ratio:.4f} 不是 {expected_ratio}（容差 0.02）",
                    ))
                else:
                    issues.append(_info(
                        "aspect_ratio",
                        f"显示画幅比例 {final_ratio:.4f} 符合 {expected_ratio}",
                    ))

        fps = _parse_frame_rate(video_stream)
        spec["fps"] = fps
        if fps is None:
            issues.append(_blocking("frame_rate", "帧率字段缺失、分母为零或非法"))
        elif not 24 <= fps <= 60:
            issues.append(_blocking("frame_rate", f"帧率 {fps:.3f} 不在 24–60 fps"))
        else:
            issues.append(_info("frame_rate", f"帧率 {fps:.3f} fps 达标"))

    if audio_stream is None:
        issues.append(_blocking("audio_stream", "成片缺少音频流"))
    else:
        issues.append(_info("audio_stream", "成片包含音频流"))

    raw_duration = (
        payload.get("format", {}).get("duration")
        if isinstance(payload.get("format"), dict) else None
    )
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = None
    if duration is None or not math.isfinite(duration) or duration <= 0:
        issues.append(_blocking("duration", "成片时长字段缺失或非法"))
    else:
        spec["duration"] = duration
        issues.append(_info("duration", f"成片时长 {duration:.3f}s"))

    return issues, spec


# --------------------------------------------------------------------------- #
# 视频级检查
# --------------------------------------------------------------------------- #
def _check_black(ff: Path, video: Path, thr: dict) -> list[dict]:
    out = _run_ff(ff, ["-i", str(video),
                       "-vf", f"blackdetect=d={thr['min_dur']}:pix_th={thr['pix_th']}",
                       "-an", "-f", "null", "-"])
    issues = []
    for m in re.finditer(r"black_duration:\s*([\d.]+)", out):
        dur = float(m.group(1))
        if dur > thr["block_above"]:
            issues.append({
                "check": "black_frame", "severity": "blocking",
                "ok": False,
                "detail": f"检测到黑场约 {dur:.1f}s（>{thr['block_above']}s），疑似缺素材/转场断层",
            })
    if not issues:
        issues.append({"check": "black_frame", "severity": "info", "ok": True,
                       "detail": "未检测到异常黑场"})
    return issues


def _image_scene_intervals(db, evidence: dict | None) -> list[tuple[float, float]]:
    """Return candidate scene windows that intentionally display a still image."""
    if not isinstance(evidence, dict):
        return []
    dependency = evidence.get("dependency_snapshot", evidence)
    cards = dependency.get("scene_cards") if isinstance(dependency, dict) else None
    if not isinstance(cards, list):
        return []
    asset_ids = sorted({
        str(card["asset_id"])
        for card in cards
        if isinstance(card, dict) and card.get("asset_id")
    })
    if not asset_ids:
        return []
    placeholders = ",".join("?" for _ in asset_ids)
    image_ids = {
        row["asset_id"]
        for row in db.connection.execute(
            f"SELECT asset_id FROM assets WHERE media_kind='image' AND asset_id IN ({placeholders})",
            tuple(asset_ids),
        ).fetchall()
    }
    intervals = []
    for card in cards:
        if not isinstance(card, dict) or card.get("asset_id") not in image_ids:
            continue
        try:
            start = float(card.get("start", card.get("narrative_start")))
            end = float(card.get("end", card.get("narrative_end")))
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
            intervals.append((start, end))
    return sorted(intervals)


def _covered_by_image(start: float, end: float, intervals: list[tuple[float, float]]) -> bool:
    covered = start
    for image_start, image_end in intervals:
        if image_end < covered:
            continue
        if image_start > covered + 0.05:
            return False
        covered = max(covered, image_end)
        if covered + 0.05 >= end:
            return True
    return False


def _check_freeze(
    ff: Path, video: Path, thr: dict,
    *, image_intervals: list[tuple[float, float]] | None = None,
) -> list[dict]:
    out = _run_ff(ff, ["-i", str(video),
                       "-vf", f"freezedetect=n={thr['noise']}:d={thr['min_dur']}",
                       "-an", "-f", "null", "-"])
    issues = []
    start = None
    duration = None
    events = re.finditer(r"freeze_(start|duration|end):\s*([\d.]+)", out)
    for event in events:
        kind, raw = event.groups()
        value = float(raw)
        if kind == "start":
            start, duration = value, None
        elif kind == "duration":
            duration = value
        elif start is not None:
            actual_duration = duration if duration is not None else value - start
            if actual_duration > thr["warn_above"] and not _covered_by_image(
                start, value, image_intervals or []
            ):
                issues.append({
                    "check": "freeze", "severity": "warn", "ok": False,
                    "detail": f"检测到画面冻结约 {actual_duration:.1f}s（>{thr['warn_above']}s），疑似定帧/卡顿",
                    "startSeconds": start,
                })
            start, duration = None, None
    if start is not None and duration is not None:
        if duration > thr["warn_above"] and not _covered_by_image(
            start, start + duration, image_intervals or []
        ):
            issues.append({
                "check": "freeze", "severity": "warn", "ok": False,
                "detail": f"检测到画面冻结约 {duration:.1f}s（>{thr['warn_above']}s），疑似定帧/卡顿",
                "startSeconds": start,
            })
    if not issues:
        issues.append({"check": "freeze", "severity": "info", "ok": True,
                       "detail": "未检测到异常冻结"})
    return issues


def _check_silence(ff: Path, video: Path, thr: dict) -> list[dict]:
    out = _run_ff(ff, ["-i", str(video),
                       "-af", f"silencedetect=n={thr['noise']}dB:d={thr['dur']}",
                       "-vn", "-f", "null", "-"])
    durations = [float(m.group(1)) for m in re.finditer(r"silence_duration:\s*([\d.]+)", out)]
    total = sum(durations)
    issues = []
    if any(d > thr["block_above"] for d in durations):
        issues.append({
            "check": "silence", "severity": "blocking", "ok": False,
            "detail": f"存在 >{thr['block_above']}s 静音段（{max(durations):.1f}s），人声/配音可能缺失",
        })
    elif total > thr["warn_total_above"]:
        issues.append({
            "check": "silence", "severity": "warn", "ok": False,
            "detail": f"累计静音 {total:.1f}s 偏长，检查是否有大段无声",
        })
    if not issues:
        issues.append({"check": "silence", "severity": "info", "ok": True,
                       "detail": f"静音累计 {total:.1f}s，正常"})
    return issues


def _check_clipping(ff: Path, video: Path, thr: dict) -> list[dict]:
    out = _run_ff(ff, ["-i", str(video), "-af", "volumedetect", "-vn", "-f", "null", "-"])
    m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", out)
    if not m:
        return [{"check": "clipping", "severity": "warn", "ok": True,
                 "detail": "未能读取响度（跳过削波检查）"}]
    max_vol = float(m.group(1))
    if max_vol > thr["warn_above"]:
        return [{"check": "clipping", "severity": "warn", "ok": False,
                 "detail": f"峰值 {max_vol:.1f}dB 逼近 0dB，存在削波风险，需降增益/加 alimiter"}]
    return [{"check": "clipping", "severity": "info", "ok": True,
             "detail": f"峰值 {max_vol:.1f}dB，无削波"}]


# --------------------------------------------------------------------------- #
# 工程一致性检查（DB）
# --------------------------------------------------------------------------- #
def _check_dependencies(db, task_id: str) -> list[dict]:
    issues = []
    rows = db.connection.execute(
        "SELECT card_id, asset_id FROM scene_cards WHERE task_id=? AND asset_id IS NOT NULL",
        (task_id,),
    ).fetchall()
    missing = []
    for r in rows:
        exists = db.connection.execute(
            "SELECT 1 FROM assets WHERE asset_id=?", (r["asset_id"],)
        ).fetchone()
        if exists is None:
            missing.append(r["card_id"])
    if missing:
        issues.append({
            "check": "dependency", "severity": "blocking", "ok": False,
            "detail": f"{len(missing)} 张场景卡片引用了未入册素材：{missing}",
        })
    else:
        issues.append({"check": "dependency", "severity": "info", "ok": True,
                       "detail": "所有场景卡片素材均已入册"})
    return issues


def _check_repeated_shots(db, task_id: str, render_plan=None) -> list[dict]:
    if isinstance(render_plan, dict) and isinstance(render_plan.get("segments"), list):
        from collections import Counter
        counts = Counter(str(Path(row["src"]).resolve()) for row in render_plan["segments"] if row.get("src"))
        repeated = [f"{Path(path).name}×{count}" for path,count in counts.items() if count > 1]
        result = [{"check":"repeated_shot","severity":"warn" if repeated else "info","ok":not repeated,
                   "detail":"实际渲染镜头重复：" + "；".join(repeated) if repeated else "实际渲染镜头无重复"}]
        shots = (render_plan.get("referenceMatch") or {}).get("shots", [])
        loops = [str(row["shot"]) for row in shots if row.get("short_material_loop")]
        if loops:
            result.append({"check":"short_material_loop","severity":"warn","ok":False,"detail":"第 " + "、".join(loops) + " 镜素材短于配音窗口，存在循环"})
        substitutes = [str(row["shot"]) for row in shots if row.get("substitution") != "exact"]
        if substitutes:
            result.append({"check":"motion_substitution","severity":"warn","ok":False,"detail":"第 " + "、".join(substitutes) + " 镜采用下位运镜替代，请审看"})
        return result
    rows = db.connection.execute(
        """
        SELECT asset_id, COUNT(*) c FROM scene_cards
        WHERE task_id=? AND asset_id IS NOT NULL AND mix_form <> '数字人全屏'
        GROUP BY asset_id HAVING c > 1
        """,
        (task_id,),
    ).fetchall()
    if rows:
        detail = "；".join(f"{r['asset_id']}×{r['c']}" for r in rows)
        return [{"check": "repeated_shot", "severity": "warn", "ok": False,
                 "detail": f"以下 B-roll 素材被重复使用：{detail}（检查是否可用不同镜头替代）"}]
    return [{"check": "repeated_shot", "severity": "info", "ok": True,
             "detail": "B-roll 素材无重复"}]


def _check_packaging_gate(db, task_id: str) -> list[dict]:
    row = db.connection.execute(
        "SELECT COUNT(*) c FROM packaging_plans WHERE task_id=? AND status='confirmed'",
        (task_id,),
    ).fetchone()
    if (row["c"] or 0) == 0:
        return [{"check": "packaging_gate", "severity": "blocking", "ok": False,
                 "detail": "尚未确认任何包装方案（packaging_plan），不可进入 qa 后渲染"}]
    return [{"check": "packaging_gate", "severity": "info", "ok": True,
             "detail": "已有确认包装方案"}]


def _check_privacy(db, task_id: str) -> list[dict]:
    rows = db.connection.execute(
        """
        SELECT DISTINCT pf.flag_id, pf.asset_id
        FROM privacy_flags pf
        JOIN scene_cards sc ON sc.asset_id = pf.asset_id
        WHERE sc.task_id=? AND pf.needs_human_confirm=1 AND pf.confirmed=0
        """,
        (task_id,),
    ).fetchall()
    if rows:
        return [{"check": "privacy", "severity": "blocking", "ok": False,
                 "detail": f"{len(rows)} 个隐私标记待人工确认但未确认："
                           f"{[r['flag_id'] for r in rows]}"}]
    return [{"check": "privacy", "severity": "info", "ok": True,
             "detail": "无待确认隐私标记"}]


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run_qa(
    db,
    task_id: str,
    video_path: str | None = None,
    *,
    evidence: dict | None = None,
) -> dict:
    payload, rule_version = rules_for_task(db, task_id)
    qa = payload.get("qa", {})
    issues: list[dict] = []
    # DB 检查（始终执行）
    issues += _check_dependencies(db, task_id)
    render_plan = (evidence or {}).get("render_plan")
    issues += _check_repeated_shots(db, task_id, render_plan)
    issues += _check_packaging_gate(db, task_id)
    issues += _check_privacy(db, task_id)
    issues += collect_qa_evidence(db, task_id, evidence)

    # 视频检查（有文件才跑）
    video_spec = None
    if video_path and Path(video_path).is_file():
        try:
            roots = default_ffprobe_search_roots(os.environ)
            ff = find_ffmpeg(os.environ, search_roots=roots)
            probe = find_ffprobe(os.environ, search_roots=roots)
            spec_issues, video_spec = _check_video_spec(probe, Path(video_path), **({"expected_ratio":render_plan["ratio"]} if render_plan and render_plan.get("ratio") else {}))
            issues += spec_issues
            issues += check_subtitle_evidence(
                None if evidence is None else evidence.get("subtitle_path"),
                video_duration=(video_spec or {}).get("duration") or 0,
                frame_size=(
                    (video_spec or {}).get("display_width") or (video_spec or {}).get("width") or 0,
                    (video_spec or {}).get("display_height") or (video_spec or {}).get("height") or 0,
                ),
            )
            issues += _check_black(
                ff, Path(video_path), qa.get("black", DEFAULT_PAYLOAD["qa"]["black"])
            )
            issues += _check_freeze(
                ff, Path(video_path), qa.get("freeze", DEFAULT_PAYLOAD["qa"]["freeze"]),
                image_intervals=_image_scene_intervals(db, evidence),
            )
            issues += _check_silence(
                ff, Path(video_path), qa.get("silence", DEFAULT_PAYLOAD["qa"]["silence"])
            )
            issues += _check_clipping(
                ff, Path(video_path), qa.get("clipping", DEFAULT_PAYLOAD["qa"]["clipping"])
            )
            loudness_rule = qa.get(
                "loudness", DEFAULT_PAYLOAD["qa"]["loudness"]
            )
            if not isinstance(loudness_rule, dict):
                loudness_rule = {}
            loudness_output = _run_ff(
                ff,
                [
                    "-hide_banner", "-nostats", "-i", str(Path(video_path)),
                    "-filter_complex", "ebur128=peak=true:framelog=verbose",
                    "-f", "null", "-",
                ],
            )
            issues += check_loudness(
                loudness_output,
                target_lufs=loudness_rule.get("target_lufs", -16.0),
                tolerance_lu=loudness_rule.get("tolerance_lu", 4.0),
                max_true_peak_dbfs=loudness_rule.get("max_true_peak_dbfs", -1.0),
            )
        except (ToolNotFound, QADetectionError, OSError, RuntimeError) as error:
            issues.append({
                "check": "video_evidence", "severity": "blocking", "ok": False,
                "detail": f"视频质检证据不完整：{error}",
            })
    else:
        issues.append({
            "check": "video", "severity": "blocking", "ok": False,
            "detail": "成片路径缺失或文件不存在，不能通过 QA",
        })
    blocking = [i for i in issues if i["severity"] == "blocking"]
    status = "blocked" if blocking else "passed"
    report = {
        "report_id": f"qa-{uuid.uuid4().hex[:12]}",
        "task_id": task_id,
        "rule_version": rule_version,
        "video_path": video_path,
        "video_spec": video_spec,
        "status": status,
        "issue_count": len(issues),
        "blocking_count": len(blocking),
        "issues": issues,
        "created_at": _now(),
    }
    _save_report(db, report)
    return report


def _save_report(db, report: dict) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO qa_reports
              (report_id, task_id, video_path, status, issue_count, blocking_count, report_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report["report_id"], report["task_id"], report["video_path"],
                report["status"], report["issue_count"], report["blocking_count"],
                __import__("json").dumps(report, ensure_ascii=False),
            ),
        )


def list_reports(db, task_id: str) -> list[dict]:
    return [
        dict(r)
        for r in db.connection.execute(
            "SELECT report_id, task_id, video_path, status, issue_count, "
            "blocking_count, created_at FROM qa_reports WHERE task_id=? "
            "ORDER BY created_at DESC LIMIT 50",
            (task_id,),
        ).fetchall()
    ]


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
