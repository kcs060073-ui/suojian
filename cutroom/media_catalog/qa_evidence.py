"""Fail-closed release evidence checks shared by the two active video routes."""

from __future__ import annotations

import math
from pathlib import Path
import re

from .fingerprint import full_sha256, quick_fingerprint


RELEASE_QA_CHECKS = frozenset({
    "video_evidence",
    "black_frame",
    "freeze",
    "silence",
    "clipping",
    "subtitle_evidence",
    "loudness_integrated",
    "true_peak",
    "material_fingerprint",
    "material_scope",
    "material_authorization",
})


def qa_evidence_complete(report: dict) -> bool:
    """Require evidence for every release check, allowing explicit advisory warnings."""
    if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
        return False
    observed = {
        item.get("check")
        for item in report["issues"]
        if isinstance(item, dict) and (
            item.get("ok") is True
            or (
                item.get("check") in {"freeze", "clipping"}
                and item.get("severity") == "warn"
                and item.get("ok") is False)
        )
    }
    return RELEASE_QA_CHECKS.issubset(observed)


def _issue(check: str, ok: bool, detail: str) -> dict:
    return {
        "check": check,
        "severity": "info" if ok else "blocking",
        "ok": ok,
        "detail": detail,
    }


def _subtitle_time(value: str) -> float:
    match = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?\s*", value)
    if match is None:
        raise ValueError(f"非法字幕时间：{value!r}")
    hours, minutes, seconds = (int(match.group(index)) for index in (1, 2, 3))
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"非法字幕时间：{value!r}")
    fraction = match.group(4) or "0"
    return hours * 3600 + minutes * 60 + seconds + int(fraction) / (10 ** len(fraction))


def _split_ass_row(line: str, count: int) -> list[str]:
    values = line.split(":", 1)[1].split(",", max(0, count - 1))
    if len(values) != count:
        raise ValueError("ASS 字段数量与 Format 不一致")
    return [value.strip() for value in values]


def _parse_ass(path: Path) -> tuple[list[dict], dict[str, dict]]:
    section = ""
    style_format: list[str] = []
    event_format: list[str] = []
    styles: dict[str, dict] = {}
    cues: list[dict] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.casefold()
            continue
        if not line or line.startswith(";"):
            continue
        if section == "[v4+ styles]" and line.casefold().startswith("format:"):
            style_format = [item.strip().casefold() for item in line.split(":", 1)[1].split(",")]
        elif section == "[v4+ styles]" and line.casefold().startswith("style:"):
            if not style_format:
                raise ValueError("ASS Style 前缺少 Format")
            values = _split_ass_row(line, len(style_format))
            row = dict(zip(style_format, values))
            name = row.get("name")
            if not name:
                raise ValueError("ASS 样式缺少名称")
            styles[name] = row
        elif section == "[events]" and line.casefold().startswith("format:"):
            event_format = [item.strip().casefold() for item in line.split(":", 1)[1].split(",")]
        elif section == "[events]" and line.casefold().startswith("dialogue:"):
            if not event_format:
                raise ValueError("ASS Dialogue 前缺少 Format")
            values = _split_ass_row(line, len(event_format))
            cues.append(dict(zip(event_format, values)))
    if not cues:
        raise ValueError("ASS 没有 Dialogue 字幕事件")
    return cues, styles


def _parse_srt(path: Path) -> list[dict]:
    cues = []
    blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip())
    for block in blocks:
        lines = block.splitlines()
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            raise ValueError("SRT 字幕块缺少时间轴")
        start, end = [item.strip() for item in lines[timing_index].split("-->", 1)]
        cues.append({"start": start, "end": end, "text": "\\N".join(lines[timing_index + 1:])})
    if not cues:
        raise ValueError("SRT 没有字幕事件")
    return cues


def check_subtitle_evidence(
    subtitle_path: str | Path | None,
    *,
    video_duration: float,
    frame_size: tuple[int, int],
) -> list[dict]:
    """Validate structural, timing, line-count, and phone-safe-area evidence."""
    if subtitle_path is None:
        return [_issue("subtitle_evidence", False, "缺少烧录字幕源文件，无法证明字幕安全")]
    path = Path(subtitle_path)
    if not path.is_file():
        return [_issue("subtitle_evidence", False, f"字幕证据文件不存在：{path}")]
    try:
        duration = float(video_duration)
        width, height = (int(frame_size[0]), int(frame_size[1]))
        if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
            raise ValueError("成片时长或画幅非法")
        suffix = path.suffix.casefold()
        if suffix == ".ass":
            cues, styles = _parse_ass(path)
        elif suffix == ".srt":
            cues, styles = _parse_srt(path), {}
        else:
            raise ValueError("仅支持 ASS/SRT 字幕证据")
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        return [_issue("subtitle_evidence", False, f"字幕证据不可读：{error}")]

    issues: list[dict] = []
    last_start = -1.0
    last_end = -1.0
    timing_errors = []
    line_errors = []
    safe_errors = []
    for index, cue in enumerate(cues, start=1):
        try:
            start = _subtitle_time(cue.get("start", ""))
            end = _subtitle_time(cue.get("end", ""))
        except ValueError as error:
            timing_errors.append(f"第 {index} 条：{error}")
            continue
        if (
            start < 0 or end <= start or start < last_start or end < last_end
            or end > duration + 0.1
        ):
            timing_errors.append(
                f"第 {index} 条时间 {start:.3f}-{end:.3f}s 不在成片 0-{duration:.3f}s 内"
            )
        last_start = start
        last_end = end
        text = re.sub(r"\{[^}]*\}", "", str(cue.get("text") or "")).strip()
        lines = re.split(r"\\[Nn]", text) if text else []
        if not lines or any(not item.strip() for item in lines) or len(lines) > 2:
            line_errors.append(f"第 {index} 条为空或超过两行")

        if path.suffix.casefold() == ".ass":
            style = styles.get(str(cue.get("style") or ""))
            if style is None:
                safe_errors.append(f"第 {index} 条引用了不存在的样式")
                continue
            raw_margin = cue.get("marginv")
            try:
                margin = int(raw_margin) if raw_margin not in (None, "", "0") else int(style.get("marginv") or 0)
                alignment = int(style.get("alignment") or 0)
            except ValueError:
                safe_errors.append(f"第 {index} 条垂直边距或对齐值非法")
                continue
            safe_min = max(24, round(height * 0.02))
            safe_max = round(height * 0.45)
            if alignment not in range(1, 10) or not safe_min <= margin <= safe_max:
                safe_errors.append(
                    f"第 {index} 条 MarginV={margin} 不在手机安全区 {safe_min}-{safe_max}px"
                )
        else:
            safe_errors.append("SRT 不含烧录位置，无法证明手机安全区")

    issues.append(_issue(
        "subtitle_timing", not timing_errors,
        "字幕时间均在成片范围内" if not timing_errors else "；".join(timing_errors[:5]),
    ))
    issues.append(_issue(
        "subtitle_lines", not line_errors,
        "字幕均为一至两行非空文本" if not line_errors else "；".join(line_errors[:5]),
    ))
    issues.append(_issue(
        "subtitle_safe_area", not safe_errors,
        "字幕样式位于手机安全区" if not safe_errors else "；".join(safe_errors[:5]),
    ))
    ok = not any(item["severity"] == "blocking" for item in issues)
    issues.append(_issue(
        "subtitle_evidence", ok,
        f"已核验 {len(cues)} 条字幕" if ok else "字幕证据未满足发布门禁",
    ))
    return issues


def check_loudness(
    ffmpeg_output: str,
    *,
    target_lufs: float,
    tolerance_lu: float,
    max_true_peak_dbfs: float = -1.0,
) -> list[dict]:
    """Parse an ebur128 summary and evaluate integrated loudness and true peak."""
    text = str(ffmpeg_output or "")
    integrated = re.findall(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS\b", text, re.IGNORECASE)
    peaks = re.findall(r"\bPeak:\s*(-?\d+(?:\.\d+)?)\s*dBFS\b", text, re.IGNORECASE)
    if not integrated:
        return [_issue("loudness_evidence", False, "无法从 ebur128 输出读取综合响度")]
    try:
        measured = float(integrated[-1])
        target = float(target_lufs)
        tolerance = float(tolerance_lu)
        peak_limit = float(max_true_peak_dbfs)
    except (TypeError, ValueError):
        return [_issue("loudness_evidence", False, "响度规则或测量值非法")]
    if (
        not all(math.isfinite(item) for item in (measured, target, tolerance, peak_limit))
        or tolerance < 0
    ):
        return [_issue("loudness_evidence", False, "响度规则或测量值非法")]
    within = abs(measured - target) <= tolerance
    issues = [_issue(
        "loudness_integrated", within,
        f"综合响度 {measured:.1f} LUFS，目标 {target:.1f}±{tolerance:.1f} LU",
    )]
    if not peaks:
        issues.append(_issue("true_peak_evidence", False, "无法从 ebur128 输出读取真峰值"))
    else:
        peak = float(peaks[-1])
        peak_ok = math.isfinite(peak) and peak <= peak_limit
        issues.append(_issue(
            "true_peak", peak_ok,
            f"真峰值 {peak:.1f} dBFS，上限 {peak_limit:.1f} dBFS",
        ))
    return issues


def _latest_task_source_membership(
    db, task_id: str,
) -> tuple[set[tuple[str, str]], dict[str, str], bool]:
    rows = db.connection.execute(
        """
        SELECT scope.library_id,snapshot.summary_json
        FROM task_source_scopes scope
        LEFT JOIN task_source_snapshots snapshot
          ON snapshot.rowid=(
            SELECT latest.rowid FROM task_source_snapshots latest
            WHERE latest.scope_id=scope.scope_id
            ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1
          )
        WHERE scope.task_id=? AND scope.active=1
        """,
        (task_id,),
    ).fetchall()
    membership: set[tuple[str, str]] = set()
    authorization: dict[str, str] = {}
    complete = bool(rows)
    for row in rows:
        if not row["summary_json"]:
            complete = False
            continue
        try:
            import json

            summary = json.loads(row["summary_json"])
            assets = summary["assets"]
            if not isinstance(assets, list):
                raise TypeError("assets must be a list")
        except (KeyError, TypeError, ValueError):
            complete = False
            continue
        for asset in assets:
            if isinstance(asset, dict) and asset.get("asset_id"):
                asset_id = str(asset["asset_id"])
                membership.add((asset_id, str(row["library_id"])))
                if asset.get("authorization_status"):
                    authorization[asset_id] = str(asset["authorization_status"])
    return membership, authorization, complete


def _catalog_asset_entry(db, asset_id: str, dependency_files: list[dict]) -> dict:
    row = db.connection.execute(
        """
        SELECT asset.asset_id,asset.quick_fingerprint,asset.full_sha256,
               location.library_id,location.relative_path,library.mount_path
        FROM assets asset
        LEFT JOIN asset_locations location
          ON location.asset_id=asset.asset_id AND location.state='available'
        LEFT JOIN libraries library USING(library_id)
        WHERE asset.asset_id=?
        ORDER BY location.location_id LIMIT 1
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        return {"asset_id": asset_id}
    result = dict(row)
    if row["mount_path"] is not None and row["relative_path"] is not None:
        result["path"] = str(Path(row["mount_path"]) / Path(row["relative_path"]))
    for item in dependency_files:
        if item.get("path") and result.get("path") and Path(item["path"]).resolve() == Path(result["path"]).resolve():
            result.update({key: item.get(key) for key in ("sha256", "size_bytes", "mtime_ns")})
            break
    return result


def _candidate_assets(db, dependency_snapshot: dict) -> list[dict]:
    raw_assets = dependency_snapshot.get("assets")
    if isinstance(raw_assets, list) and raw_assets:
        return [dict(item) for item in raw_assets if isinstance(item, dict)]
    dependency_files = [
        dict(item) for item in dependency_snapshot.get("files") or []
        if isinstance(item, dict)
    ]
    asset_ids = []
    for card in dependency_snapshot.get("scene_cards") or []:
        if isinstance(card, dict) and card.get("asset_id") and card["asset_id"] not in asset_ids:
            asset_ids.append(str(card["asset_id"]))
    return [_catalog_asset_entry(db, asset_id, dependency_files) for asset_id in asset_ids]


def collect_qa_evidence(db, task_id: str, candidate_inputs: dict | None) -> list[dict]:
    """Check immutable material bytes, frozen task scope, and commercial authorization."""
    if not isinstance(candidate_inputs, dict):
        return [
            _issue("material_fingerprint", False, "缺少候选依赖快照，无法复核素材内容"),
            _issue("material_scope", False, "缺少候选来源证据，无法复核任务素材范围"),
            _issue("material_authorization", False, "缺少素材授权证据"),
        ]
    dependency = candidate_inputs.get("dependency_snapshot", candidate_inputs)
    if not isinstance(dependency, dict):
        dependency = {}
    assets = _candidate_assets(db, dependency)
    fingerprint_errors = []
    scope_errors = []
    authorization_errors = []
    membership, snapshot_authorization, membership_complete = (
        _latest_task_source_membership(db, task_id)
    )
    explicit_authorization = candidate_inputs.get("shot_authorization")
    explicit_authorization = (
        explicit_authorization if isinstance(explicit_authorization, dict) else {}
    )

    if not assets:
        fingerprint_errors.append("候选依赖快照没有画面素材")
        scope_errors.append("候选依赖快照没有可核对的素材来源")
    for entry in assets:
        asset_id = str(entry.get("asset_id") or "")
        path_value = entry.get("path")
        if not asset_id or not path_value:
            fingerprint_errors.append(f"素材 {asset_id or 'unknown'} 缺少路径或身份")
            continue
        path = Path(path_value)
        if not path.is_file():
            fingerprint_errors.append(f"素材文件不存在：{asset_id}/{path}")
        else:
            expected_sha = entry.get("sha256") or entry.get("full_sha256")
            expected_quick = entry.get("quick_fingerprint")
            if expected_sha:
                if full_sha256(path) != expected_sha:
                    fingerprint_errors.append(f"素材内容与冻结 SHA-256 不一致：{asset_id}")
            elif expected_quick:
                if quick_fingerprint(path) != expected_quick:
                    fingerprint_errors.append(f"素材内容与冻结快速指纹不一致：{asset_id}")
            else:
                fingerprint_errors.append(f"素材缺少冻结指纹：{asset_id}")
        library_id = entry.get("library_id")
        if not library_id:
            row = db.connection.execute(
                """
                SELECT location.library_id FROM asset_locations location
                WHERE location.asset_id=? AND location.state='available'
                ORDER BY location.location_id LIMIT 1
                """,
                (asset_id,),
            ).fetchone()
            library_id = None if row is None else row["library_id"]
        if not membership_complete or (asset_id, str(library_id or "")) not in membership:
            scope_errors.append(f"素材不在任务冻结来源快照内：{asset_id}")
        status = (
            explicit_authorization.get(asset_id)
            or entry.get("authorization_status")
            or snapshot_authorization.get(asset_id)
            or "unknown"
        )
        if status != "approved":
            authorization_errors.append(f"镜头素材授权未明确通过：{asset_id}/{status}")

    packaging = dependency.get("packaging_compiled") or dependency.get("packaging")
    packaging_items = packaging.get("items") if isinstance(packaging, dict) else None
    if not isinstance(packaging_items, list):
        authorization_errors.append("缺少已确认包装方案的授权证据")
    else:
        for item in packaging_items:
            if not isinstance(item, dict) or item.get("license", "unknown") != "commercial-approved":
                slot = item.get("slot", "unknown") if isinstance(item, dict) else "unknown"
                authorization_errors.append(f"包装素材授权不可商用：{slot}")

    return [
        _issue(
            "material_fingerprint", not fingerprint_errors,
            "素材内容与冻结指纹一致" if not fingerprint_errors else "；".join(fingerprint_errors[:8]),
        ),
        _issue(
            "material_scope", not scope_errors,
            "素材均来自任务冻结来源快照" if not scope_errors else "；".join(scope_errors[:8]),
        ),
        _issue(
            "material_authorization", not authorization_errors,
            "镜头与包装素材授权证据完整" if not authorization_errors else "；".join(authorization_errors[:8]),
        ),
    ]
