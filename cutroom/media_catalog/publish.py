"""Build a fixed, review-ready release package without uploading it."""

import json
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from .tools import default_ffprobe_search_roots, find_ffmpeg
from .candidate_versions import resolve_candidate_media
from .editable_delivery import build_delivery_manifest
from .reviews import matching_approval
from .snapshots import file_content_signature, latest_review_candidate


DELIVER_ROOT = Path(__file__).resolve().parents[1] / "deliverables" / "publish"

# 起步合规清单：广告法绝对化用语 + 招商/财税高风险表述
SENSITIVE_WORDS = [
    "最", "第一", "国家级", "顶级", "唯一", "绝对", "100%", "百分百",
    " guaranteed", "包过", "保过", "稳赚", "零风险", "国家级园区",
    "最强", "首选", "极致", "极致性价比", "全网最低",
]

PACKAGE_FILES = (
    "video-final.mp4",
    "cover-01.jpg",
    "cover-02.jpg",
    "cover-03.jpg",
    "publish-copy.md",
    "publish-check.json",
    "qa-report.md",
)

_COVER_CHOICES = {"candidate-1", "candidate-2", "candidate-3", "external", "pending"}
_DELIVERY_TYPES = {"standard", "editable"}


def check_sensitive(text: str) -> list[str]:
    if not text:
        return []
    hits = []
    low = text.lower()
    for word in SENSITIVE_WORDS:
        if word.lower() in low:
            hits.append(word.strip())
    return hits


def _safe_component(value: str, label: str) -> str:
    if (
        not value or value in (".", "..") or len(value) > 80
        or any(ch in value for ch in "/\\:\0\r\n")
        or not re.fullmatch(r"[\w\-.\u4e00-\u9fff]+", value)
    ):
        raise ValueError(f"INVALID_{label.upper()}: 非法路径名称：{value!r}")
    return value


def _same_path(left, right) -> bool:
    if not left or not right:
        return False
    return Path(left).resolve() == Path(right).resolve()


def _run_hook(name: str) -> None:
    hook = globals().get(name)
    if hook is not None:
        hook()


def _validate_release_inputs(
    db,
    task_id: str,
    video_path: str | Path | None,
    qa_report: dict | None,
    candidate_id: str | None = None,
) -> tuple[Path, float, dict, dict]:
    """Load release authority from SQLite; caller data supplies only report_id."""
    if video_path is None:
        raise ValueError("VIDEO_PATH_REQUIRED: 完整发布包必须绑定候选成片")
    video = Path(video_path).resolve()
    if not video.is_file():
        raise ValueError(f"VIDEO_NOT_FOUND: 候选成片不存在：{video}")
    if not isinstance(qa_report, dict) or not qa_report.get("report_id"):
        raise ValueError("QA_REPORT_REQUIRED: 完整发布包必须提供 QA report_id")

    requested_report_id = qa_report["report_id"]
    row = db.connection.execute(
        """
        SELECT report_id, task_id, video_path, status, issue_count,
               blocking_count, report_json
        FROM qa_reports
        WHERE task_id=? AND report_id=?
        """,
        (task_id, requested_report_id),
    ).fetchone()
    if row is None:
        raise ValueError("QA_REPORT_NOT_FOUND: SQLite 中不存在当前任务的 QA 报告")
    if row["status"] != "passed" or row["blocking_count"] != 0:
        raise ValueError("QA_NOT_PASSED: SQLite QA 必须 passed 且零阻断")
    if not _same_path(row["video_path"], video):
        raise ValueError("QA_VIDEO_MISMATCH: SQLite QA 未绑定当前候选成片")

    try:
        stored_report = json.loads(row["report_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("QA_REPORT_INVALID: SQLite report_json 非法") from error
    if (
        not isinstance(stored_report, dict)
        or stored_report.get("report_id") != row["report_id"]
        or stored_report.get("task_id") != row["task_id"]
        or stored_report.get("status") != row["status"]
        or stored_report.get("blocking_count") != row["blocking_count"]
        or not _same_path(stored_report.get("video_path"), row["video_path"])
    ):
        raise ValueError("QA_REPORT_INVALID: SQLite QA 列与 report_json 身份不一致")

    raw_duration = (stored_report.get("video_spec") or {}).get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = math.nan
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("QA_DURATION_INVALID: SQLite QA 缺少有效成片时长")

    candidate = latest_review_candidate(db, task_id)
    if candidate is None or candidate.get("producer") not in {"assemble-v1", "assemble-ad-v1"}:
        raise ValueError(
            "TRUSTED_CANDIDATE_REQUIRED: 缺少可信审片候选"
        )
    if not candidate.get("candidate_id"):
        raise ValueError("TRUSTED_CANDIDATE_REQUIRED: 当前成片缺少候选版本身份")
    if candidate_id is not None and candidate.get("candidate_id") != candidate_id:
        raise ValueError("CANDIDATE_BINDING_CHANGED: 发布请求绑定的候选已变化")
    try:
        trusted_video = resolve_candidate_media(db, task_id, candidate["candidate_id"])
    except (KeyError, RuntimeError) as error:
        raise ValueError("CANDIDATE_VIDEO_CHANGED: 候选成片路径或内容已变化") from error
    if trusted_video != video:
        raise ValueError("QA_VIDEO_MISMATCH: 发布路径不是登记的候选成片")
    if candidate.get("qa_report_id") != row["report_id"]:
        raise ValueError("QA_CANDIDATE_MISMATCH: QA 报告不是当前可信候选")
    if (
        not _same_path(candidate.get("output_path"), video)
        or not _same_path(candidate.get("qa_output_path"), video)
    ):
        raise ValueError("QA_VIDEO_MISMATCH: 可信候选未绑定当前成片")

    current_signature = file_content_signature(video)
    if (
        not isinstance(candidate.get("output_signature"), dict)
        or candidate.get("qa_output_signature") != candidate.get("output_signature")
        or current_signature != candidate.get("output_signature")
    ):
        raise ValueError("CANDIDATE_VIDEO_CHANGED: 审片后成片内容已变化")

    approval = matching_approval(
        db,
        task_id,
        candidate["qa_snapshot_id"],
        row["report_id"],
    )
    if approval is None or approval["decision"] != "approved":
        raise ValueError(
            "REVIEW_APPROVAL_REQUIRED: 缺少与当前 snapshot + QA 匹配的批准记录"
        )

    return video, duration, stored_report, candidate


def _extract_cover(ffmpeg: Path, video: Path, timestamp: float, target: Path) -> None:
    command = [
        str(ffmpeg),
        "-v", "error",
        "-ss", f"{timestamp:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-q:v", "2",
        "-an",
        "-sn",
        str(target),
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"COVER_EXTRACTION_FAILED: {target.name}: {error}") from error
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = (completed.stderr or b"").decode("utf-8", "ignore")[-500:]
        raise RuntimeError(
            f"COVER_EXTRACTION_FAILED: {target.name}: "
            f"ffmpeg code={completed.returncode} {detail}"
        )


def _write_exclusive(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as target:
        target.write(content)


def _validate_external_cover(path_value: str | Path | None) -> tuple[Path, dict]:
    if path_value is None:
        raise ValueError("EXTERNAL_COVER_REQUIRED: 外部封面模式必须选择图片")
    path = Path(path_value).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"EXTERNAL_COVER_INVALID: 外部封面不存在或为空：{path}")
    suffix = path.suffix.lower()
    header = path.read_bytes()[:16]
    valid = (
        (suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
        or (suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
        or (suffix == ".webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    )
    if not valid:
        raise ValueError("EXTERNAL_COVER_INVALID: 仅支持内容有效的 JPG、PNG 或 WebP 图片")
    return path, file_content_signature(path)


def _manifest_files(check: dict) -> tuple[str, ...]:
    raw = check.get("files")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("PUBLISH_PACKAGE_CHANGED: 发布包清单缺少实际文件列表")
    files = tuple(str(name) for name in raw)
    if (
        len(files) != len(set(files))
        or "publish-check.json" not in files
        or any(Path(name).name != name or name in {"", ".", ".."} for name in files)
    ):
        raise RuntimeError("PUBLISH_PACKAGE_CHANGED: 发布包文件清单非法")
    return files


def _package_files_from_check(package_dir: Path) -> tuple[str, ...]:
    check_path = package_dir / "publish-check.json"
    try:
        check = json.loads(check_path.read_text(encoding="utf-8"))
        return _manifest_files(check)
    except (OSError, ValueError, TypeError, RuntimeError):
        return ()


def _qa_markdown(qa_report: dict) -> str:
    lines = [
        "# QA 报告",
        "",
        f"- report_id：{qa_report['report_id']}",
        f"- 状态：{qa_report['status']}",
        f"- 阻断项：{qa_report.get('blocking_count', 0)}",
        f"- 成片：{qa_report.get('video_path', '')}",
        "",
        "## 检查明细",
        "",
    ]
    for issue in qa_report.get("issues", []):
        mark = "通过" if issue.get("ok") is True else issue.get("severity", "异常")
        lines.append(
            f"- [{mark}] {issue.get('check', 'unknown')}：{issue.get('detail', '')}"
        )
    return "\n".join(lines) + "\n"


def _publish_markdown(
    task_id: str,
    platform: str,
    title: str | None,
    description: str | None,
    tags: list[str],
) -> str:
    return (
        f"# {title or task_id} · {platform} 发布文案\n\n"
        f"- 标题：{title or ''}\n"
        f"- 标签：{', '.join(tags)}\n\n"
        f"## 简介\n\n{description or ''}\n"
    )


def build_publish_package(
    db,
    task_id: str,
    platform: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    cover_path: str | None = None,
    *,
    cover_choice: str = "candidate-1",
    external_cover_path: str | Path | None = None,
    delivery_type: str = "standard",
    component_overrides: dict | None = None,
    trusted_roots: list[str | Path] | None = None,
    video_path: str | Path | None = None,
    qa_report: dict | None = None,
    candidate_id: str | None = None,
) -> dict:
    """Create a candidate-bound standard or editable delivery package."""
    if db.connection.in_transaction:
        raise ValueError(
            "ACTIVE_TRANSACTION_UNSUPPORTED: 发布门禁不支持调用方持有活动事务"
        )
    if external_cover_path is None and cover_path is not None:
        external_cover_path = cover_path
        if cover_choice == "candidate-1":
            cover_choice = "external"
    if cover_choice not in _COVER_CHOICES:
        raise ValueError(f"COVER_CHOICE_INVALID: 不支持的封面模式：{cover_choice}")
    if delivery_type not in _DELIVERY_TYPES:
        raise ValueError(f"DELIVERY_TYPE_INVALID: 不支持的交付类型：{delivery_type}")
    external_cover = None
    external_signature = None
    if cover_choice == "external":
        external_cover, external_signature = _validate_external_cover(external_cover_path)
    elif external_cover_path is not None:
        raise ValueError("EXTERNAL_COVER_UNUSED: 只有 external 封面模式可提交外部图片")
    override_request = {}
    for name, raw_path in sorted((component_overrides or {}).items()):
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise ValueError(f"EDITABLE_COMPONENT_UNTRUSTED: 组件不存在：{path}")
        override_request[name] = {
            "path": str(path),
            "signature": file_content_signature(path),
        }
    safe_task = _safe_component(task_id, "task")
    safe_platform = _safe_component(platform, "platform")
    video, duration, qa_report, candidate = _validate_release_inputs(
        db, task_id, video_path, qa_report, candidate_id
    )

    hits = check_sensitive(title or "") + check_sensitive(description or "")
    status = "blocked" if hits else "cleared"
    tag_list = tags or []
    request = {
        "task_id": task_id, "platform": safe_platform, "title": title,
        "description": description, "tags": tag_list,
        "cover_choice": cover_choice,
        "external_cover": None if external_cover is None else {
            "path": str(external_cover), "signature": external_signature,
        },
        "delivery_type": delivery_type,
        "component_overrides": override_request,
        "qa_snapshot_id": candidate["qa_snapshot_id"],
        "qa_report_id": qa_report["report_id"],
        "candidate_id": candidate["candidate_id"],
        "video_signature": file_content_signature(video),
    }
    input_digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    root = DELIVER_ROOT.resolve()
    records = db.connection.execute(
        "SELECT * FROM publish_records WHERE task_id=? ORDER BY rowid DESC", (task_id,)
    ).fetchall()
    for record in records:
        if (
            record["candidate_id"] != candidate["candidate_id"]
            or record["package_status"] != "current"
        ):
            continue
        package_dir = Path(record["cover_path"]).resolve().parent
        check_path = package_dir / "publish-check.json"
        if not check_path.is_file():
            continue
        try:
            check = json.loads(check_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if check.get("input_digest") != input_digest:
            continue
        if check.get("record_id") != record["record_id"] or check.get("task_id") != task_id:
            raise RuntimeError("PUBLISH_PACKAGE_BINDING_CHANGED: 发布包与账本绑定不一致")
        if check.get("candidate_id") != candidate["candidate_id"]:
            raise RuntimeError("PUBLISH_PACKAGE_BINDING_CHANGED: 发布包候选绑定不一致")
        manifest_files = _manifest_files(check)
        signatures = check.get("file_signatures") or {}
        if any(not (package_dir / name).is_file() for name in manifest_files) or any(
            signatures.get(name) != file_content_signature(package_dir / name)
            for name in manifest_files if name != "publish-check.json"
        ):
            raise RuntimeError("PUBLISH_PACKAGE_CHANGED: 已生成发布包文件缺失或变化")
        _run_hook("_before_publish_finalize_hook")
        with db.transaction(immediate=True) as connection:
            _locked_video, _locked_duration, locked_qa_report, locked_candidate = _validate_release_inputs(
                db, task_id, video_path, qa_report, candidate_id,
            )
            locked_record = connection.execute(
                "SELECT * FROM publish_records WHERE record_id=? AND task_id=?",
                (record["record_id"], task_id),
            ).fetchone()
            if locked_record is None or dict(locked_record) != dict(record):
                raise RuntimeError("PUBLISH_PACKAGE_BINDING_CHANGED: 发布包账本记录已变化")
            return {
                "record_id": record["record_id"], "task_id": task_id,
                "platform": safe_platform, "title": title, "description": description,
                "tags": tag_list, "cover_path": record["cover_path"],
                "cover_choice": cover_choice, "delivery_type": delivery_type,
                "sensitive_hits": check["sensitive_hits"], "status": record["status"],
                "package_dir": str(package_dir),
                "files": [str(package_dir / name) for name in manifest_files],
                "qa_report_id": locked_qa_report["report_id"], "reused": True,
                "candidate_id": locked_candidate["candidate_id"],
                "candidate_label": locked_candidate["version_label"],
                "package_status": record["package_status"],
            }
    record_id = f"pub-{uuid.uuid4().hex[:12]}"
    version = len(records) + 1
    out_dir = root / (safe_task if version == 1 else f"{safe_task}-v{version}")
    if out_dir.parent != root:
        raise ValueError("INVALID_TASK: 发布目录越界")
    if out_dir.exists():
        raise FileExistsError(f"发布包目标已存在且未绑定当前请求：{out_dir}")

    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{safe_task}.part-{uuid.uuid4().hex}"
    staging.mkdir(exist_ok=False)
    moved = False
    try:
        shutil.copyfile(video, staging / "video-final.mp4")
        ffmpeg = find_ffmpeg(
            os.environ, search_roots=default_ffprobe_search_roots(os.environ)
        )
        for index, fraction in enumerate((0.15, 0.50, 0.85), start=1):
            _extract_cover(
                ffmpeg,
                video,
                duration * fraction,
                staging / f"cover-{index:02d}.jpg",
            )

        selected_name = {
            "candidate-1": "cover-01.jpg",
            "candidate-2": "cover-02.jpg",
            "candidate-3": "cover-03.jpg",
        }.get(cover_choice)
        cover_manifest = {"choice": cover_choice}
        if selected_name is not None:
            cover_manifest.update({"mode": "candidate", "path": selected_name})
        elif cover_choice == "external":
            selected_name = f"cover-external{external_cover.suffix.lower()}"
            shutil.copyfile(external_cover, staging / selected_name)
            cover_manifest.update({
                "mode": "external", "path": selected_name,
                "source_path": str(external_cover),
                "source_signature": external_signature,
            })
        else:
            selected_name = "cover-pending.json"
            _write_exclusive(
                staging / selected_name,
                json.dumps({
                    "status": "pending",
                    "message": "封面尚未确定，发布前必须补选封面。",
                }, ensure_ascii=False, indent=2) + "\n",
            )
            cover_manifest.update({"mode": "pending", "path": selected_name})

        if delivery_type == "editable":
            build_delivery_manifest(
                staging,
                candidate,
                trusted_roots or [video.parent],
                component_overrides=component_overrides,
            )

        _write_exclusive(
            staging / "publish-copy.md",
            _publish_markdown(task_id, safe_platform, title, description, tag_list),
        )
        _write_exclusive(staging / "qa-report.md", _qa_markdown(qa_report))
        actual_files = list(PACKAGE_FILES)
        if selected_name not in actual_files:
            actual_files.insert(actual_files.index("publish-check.json"), selected_name)
        if delivery_type == "editable":
            for path in sorted(staging.iterdir(), key=lambda item: item.name):
                if path.is_file() and path.name not in actual_files:
                    actual_files.insert(actual_files.index("publish-check.json"), path.name)

        package_check = {
            "record_id": record_id,
            "input_digest": input_digest,
            "task_id": task_id,
            "platform": safe_platform,
            "status": status,
            "qa_report_id": qa_report["report_id"],
            "candidate_id": candidate["candidate_id"],
            "candidate_label": candidate["version_label"],
            "qa_video_path": qa_report["video_path"],
            "sensitive_hits": hits,
            "tags": tag_list,
            "cover": cover_manifest,
            "delivery_type": delivery_type,
            "files": actual_files,
        }
        package_check["file_signatures"] = {
            name: file_content_signature(staging / name)
            for name in actual_files if name != "publish-check.json"
        }
        _write_exclusive(
            staging / "publish-check.json",
            json.dumps(package_check, ensure_ascii=False, indent=2) + "\n",
        )

        _run_hook("_before_publish_finalize_hook")
        connection = db.connection
        transaction_started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            locked_video, _locked_duration, locked_qa_report, locked_candidate = _validate_release_inputs(
                db, task_id, video_path, qa_report, candidate_id,
            )
            if locked_candidate["candidate_id"] != candidate["candidate_id"]:
                raise ValueError("CANDIDATE_BINDING_CHANGED: 发布期间当前候选已变化")
            staged_signature = file_content_signature(staging / "video-final.mp4")
            if staged_signature != file_content_signature(locked_video):
                raise ValueError(
                    "CANDIDATE_VIDEO_CHANGED: 暂存发布包与当前可信候选成片不一致"
                )
            if out_dir.exists():
                raise FileExistsError(f"发布包目标已存在：{out_dir}")
            os.rename(staging, out_dir)
            moved = True
            connection.execute(
                """
                INSERT INTO publish_records
                  (record_id, task_id, platform, title, description, tags, cover_path,
                   sensitive_hits, status, candidate_id, package_status)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'current')
                """,
                (
                    record_id,
                    task_id,
                    safe_platform,
                    title,
                    description,
                    json.dumps(tag_list, ensure_ascii=False),
                    str(out_dir / selected_name),
                    json.dumps(hits, ensure_ascii=False),
                    status,
                    candidate["candidate_id"],
                ),
            )
            connection.execute(
                """UPDATE candidate_versions
                SET status='published', updated_at=datetime('now')
                WHERE task_id=? AND candidate_id=?""",
                (task_id, candidate["candidate_id"]),
            )
            connection.commit()
            video = locked_video
            qa_report = locked_qa_report
        except BaseException:
            if transaction_started:
                connection.rollback()
            raise
    except BaseException:
        if moved:
            shutil.rmtree(out_dir, ignore_errors=True)
        else:
            shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "record_id": record_id,
        "task_id": task_id,
        "platform": safe_platform,
        "title": title,
        "description": description,
        "tags": tag_list,
        "cover_path": str(out_dir / selected_name),
        "cover_choice": cover_choice,
        "delivery_type": delivery_type,
        "sensitive_hits": hits,
        "status": status,
        "package_dir": str(out_dir),
        "files": [str(out_dir / name) for name in actual_files],
        "qa_report_id": qa_report["report_id"],
        "candidate_id": candidate["candidate_id"],
        "candidate_label": candidate["version_label"],
        "package_status": "current",
        "reused": False,
    }


def list_publish(db, task_id: str) -> list[dict]:
    result = []
    current = latest_review_candidate(db, task_id)
    current_candidate_id = None if current is None else current.get("candidate_id")
    for row in db.connection.execute(
        """SELECT p.record_id,p.task_id,p.platform,p.status,p.cover_path,p.created_at,
                  p.candidate_id,p.package_status,c.version_no
        FROM publish_records AS p
        LEFT JOIN candidate_versions AS c ON c.candidate_id=p.candidate_id
        WHERE p.task_id=? ORDER BY p.created_at DESC,p.rowid DESC""",
        (task_id,),
    ).fetchall():
        item = dict(row)
        cover_path = item.pop("cover_path", None)
        package_dir = (
            Path(cover_path).resolve().parent
            if isinstance(cover_path, str) and cover_path
            else None
        )
        manifest_files = () if package_dir is None else _package_files_from_check(package_dir)
        files = [] if package_dir is None else [str(package_dir / name) for name in manifest_files]
        check = {}
        if package_dir is not None:
            try:
                check = json.loads((package_dir / "publish-check.json").read_text("utf-8"))
            except (OSError, ValueError, TypeError):
                check = {}
        item.update({
            "candidate_label": (
                None if item.pop("version_no", None) is None
                else f"candidate-v{row['version_no']}"
            ),
            "is_current": bool(
                item.get("package_status") == "current"
                and item.get("candidate_id") == current_candidate_id
            ),
            "package_dir": None if package_dir is None else str(package_dir),
            "files": files,
            "cover_path": cover_path,
            "cover_choice": (check.get("cover") or {}).get("choice"),
            "delivery_type": check.get("delivery_type"),
            "tags": check.get("tags") or [],
            "package_complete": (
                bool(files) and all(Path(path).is_file() for path in files)
            ),
        })
        result.append(item)
    return result
