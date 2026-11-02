"""Durable packaging-preview artifacts and their trusted media identity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import uuid

from .provenance import canonical_digest
from .packaging_plan import (
    canonical_packaging_gate_payload,
    confirm_plan,
    validate_plan_for_render,
)
from . import packaging_plan
from .packaging_layers import PackagingTimingContext, compile_packaging_layers
from .provenance import latest_gate
from .snapshots import file_content_signature


PREVIEW_CACHE_RELATIVE_ROOT = Path(
    "runtime/workbench/cache/packaging-previews"
)
PACKAGING_PREVIEW_RENDERER_VERSION = "packaging-preview-v1"


@dataclass(frozen=True)
class PreviewIdentity:
    task_id: str
    plan_id: str
    picture_lock_digest: str
    plan_digest: str
    renderer_version: str
    preview_profile: str = "portrait-540p-v1"
    input_digest: str = ""


@dataclass(frozen=True)
class PackagingPreviewInput:
    identity: PreviewIdentity
    duration_seconds: float
    task_type: str
    plan: dict
    coverage_targets: tuple[str, ...]


def preview_canonical_key(identity: PreviewIdentity) -> str:
    return canonical_digest(asdict(identity))


def _task_preview_context(db, task_id: str) -> tuple[str, float]:
    row = db.connection.execute(
        """
        SELECT t.task_type, MAX(sc.narrative_end) AS timeline_duration
        FROM tasks t
        LEFT JOIN scene_cards sc ON sc.task_id=t.task_id
        WHERE t.task_id=?
        GROUP BY t.task_id
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")
    task_type = row["task_type"]
    if task_type not in {"digital_human", "ad_mix"}:
        raise RuntimeError(f"UNSUPPORTED_ROUTE: 当前任务类型不支持包装预览：{task_type}")
    return task_type, float(row["timeline_duration"] or 0.0)


def _signature_or_missing(path: Path) -> dict:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        return {"path": str(resolved), "missing": True}
    return file_content_signature(resolved)


def _preview_input_digest(
    db,
    task_id: str,
    plan_id: str,
    task_type: str,
    duration: float,
) -> str:
    row = db.connection.execute(
        "SELECT title,brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - guarded by _task_preview_context
        raise KeyError(f"任务不存在：{task_id}")
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("BRIEF_INVALID: 任务文案不是有效 JSON") from error
    keywords = brief.get("keywords") if isinstance(brief, dict) else None
    keyword = (
        keywords[0]
        if isinstance(keywords, list) and keywords and isinstance(keywords[0], str)
        else brief.get("keyword") if isinstance(brief, dict) else None
    ) or row["title"]
    project_root = packaging_plan.MANIFEST_DIR.resolve().parents[1]
    voice_path = (
        project_root / "runtime/workbench/tasks" / task_id / "artifacts/ad/voice.mp3"
    ).resolve()
    compiled = compile_packaging_layers(
        db,
        plan_id,
        PackagingTimingContext(
            duration_seconds=duration,
            title=str(row["title"]),
            keyword=str(keyword),
            voice_mode=(
                f"cached:{voice_path}" if task_type == "ad_mix" else "original"
            ),
        ),
    )
    auxiliary: list[dict] = []
    if task_type == "digital_human":
        sources = brief.get("subtitle_sources") if isinstance(brief, dict) else None
        if isinstance(sources, dict):
            for key in sorted(sources):
                auxiliary.append({"kind": key, **_signature_or_missing(Path(sources[key]))})
    else:
        trusted_tasks_root = (project_root / "runtime/workbench/tasks").resolve()
        task_root = (trusted_tasks_root / task_id).resolve()
        if task_root != trusted_tasks_root and trusted_tasks_root not in task_root.parents:
            raise RuntimeError("INVALID_TASK_ID: 任务目录越界")
        script = (brief.get("final_script") or {}) if isinstance(brief, dict) else {}
        script_relative_path = (
            script.get("relative_path") if isinstance(script, dict) else None
        )
        if not isinstance(script_relative_path, str) or not script_relative_path.strip():
            raise RuntimeError("BRIEF_INVALID: 广告任务缺少锁定文案路径")
        script_path = (task_root / script_relative_path).resolve()
        if script_path != task_root and task_root not in script_path.parents:
            raise RuntimeError("LOCKED_SCRIPT_PATH_INVALID: 锁定广告文案路径越界")
        paths = {
            "script": script_path,
            "adplan": task_root / "artifacts/ad/adplan.json",
            "adtiming": task_root / "artifacts/ad/adtiming.json",
            "voice": voice_path,
        }
        auxiliary.extend(
            {"kind": kind, **_signature_or_missing(path)}
            for kind, path in sorted(paths.items())
        )
    return canonical_digest({
        "brief": brief,
        "compiled_packaging": compiled.snapshot,
        "plan_fields": compiled.plan_fields,
        "auxiliary_files": auxiliary,
    })


def build_preview_input(db, task_id: str, plan_id: str) -> PackagingPreviewInput:
    plan = validate_plan_for_render(db, plan_id)
    if plan["task_id"] != task_id:
        raise PermissionError("PACKAGING_PLAN_NOT_OWNED: 方案不属于当前任务")
    picture = latest_gate(db, task_id, "picture_lock")
    if picture is None:
        raise RuntimeError("PICTURE_LOCK_MISSING: 必须先完成画面锁定")
    task_type, timeline_duration = _task_preview_context(db, task_id)
    duration = min(12.0, timeline_duration)
    if duration <= 0:
        raise RuntimeError(
            "PACKAGING_PREVIEW_TIMELINE_EMPTY: 当前任务没有可预览时长"
        )
    identity = PreviewIdentity(
        task_id=task_id,
        plan_id=plan_id,
        picture_lock_digest=picture["gate_digest"],
        plan_digest=canonical_digest(canonical_packaging_gate_payload(plan)),
        renderer_version=PACKAGING_PREVIEW_RENDERER_VERSION,
        input_digest=_preview_input_digest(
            db, task_id, plan_id, task_type, duration
        ),
    )
    slots = tuple(
        sorted(
            {
                item["slot"]
                for item in plan["items"]
                if not item["is_backup"]
            }
        )
    )
    return PackagingPreviewInput(identity, duration, task_type, plan, slots)


def current_preview_matches(db, preview: dict) -> bool:
    try:
        current = build_preview_input(db, preview["task_id"], preview["plan_id"])
    except (KeyError, PermissionError, RuntimeError, ValueError):
        return False
    return preview_canonical_key(current.identity) == preview["canonical_key"]


def assert_preview_confirmable(
    db,
    task_id: str,
    plan_id: str,
    preview_id: str | None,
    *,
    project_root: Path | None = None,
) -> dict:
    if not preview_id:
        raise RuntimeError(
            "PACKAGING_PREVIEW_REQUIRED: 确认包装方案前必须生成并观看当前预览"
        )
    preview = get_preview_artifact(db, preview_id)
    if (
        preview is None
        or preview["task_id"] != task_id
        or preview["plan_id"] != plan_id
    ):
        raise RuntimeError("PACKAGING_PREVIEW_REQUIRED: 当前方案没有对应预览")
    if preview["status"] == "missing":
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
    if preview["status"] == "stale" or not current_preview_matches(db, preview):
        raise RuntimeError("PACKAGING_PREVIEW_STALE: 画面或包装方案已变化")
    if preview["status"] != "ready":
        raise RuntimeError("PACKAGING_PREVIEW_REQUIRED: 当前预览尚未生成完成")
    if not preview.get("viewed_at"):
        raise RuntimeError("PACKAGING_PREVIEW_NOT_VIEWED: 请先完整观看包装预览")
    if not preview.get("rel_path") or not preview.get("output_signature"):
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件证据不完整")
    if project_root is None:
        from .render_mix_adapter import PROJECT_ROOT

        project_root = PROJECT_ROOT
    media_path = _resolve_cache_path(Path(project_root), preview["rel_path"])
    if not media_path.is_file():
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
    if file_content_signature(media_path) != preview["output_signature"]:
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_CHANGED: 预览文件内容已变化")
    return preview


def confirm_plan_after_preview(
    db,
    task_id: str,
    plan_id: str,
    preview_id: str | None,
    *,
    project_root: Path | None = None,
) -> dict:
    """Atomically bind viewed preview evidence to the packaging gate."""
    with db.transaction(immediate=True):
        preview = assert_preview_confirmable(
            db,
            task_id,
            plan_id,
            preview_id,
            project_root=project_root,
        )
        result = confirm_plan(
            db,
            plan_id,
            evidence={
                "preview_id": preview["preview_id"],
                "viewed_at": preview["viewed_at"],
                "canonical_key": preview["canonical_key"],
            },
        )
    return {**result, "preview_id": preview["preview_id"]}


def _preview_output_path(
    project_root: Path, task_id: str, preview_id: str
) -> Path:
    if any(part in task_id for part in ("/", "\\")) or task_id in {".", ".."}:
        raise ValueError("INVALID_TASK_ID: 任务编号包含非法路径字符")
    return (
        Path(project_root).resolve()
        / PREVIEW_CACHE_RELATIVE_ROOT
        / task_id
        / f"{preview_id}.mp4"
    ).resolve()


def _digital_subtitle_sources(db, task_id: str) -> dict:
    row = db.connection.execute(
        "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("ASS_SOURCE_MISMATCH: 任务字幕来源无效") from error
    sources = brief.get("subtitle_sources") if isinstance(brief, dict) else None
    required = {"script_path", "words_path", "semantics_path"}
    if not isinstance(sources, dict) or set(sources) != required:
        raise RuntimeError("ASS_SOURCE_MISMATCH: 包装预览缺少锁定字幕来源")
    return {key: str(Path(sources[key]).resolve()) for key in required}


def render_digital_packaging_preview(
    db,
    current: PackagingPreviewInput,
    project_root: Path,
    preview_id: str,
    runner,
) -> dict:
    """Render a bounded digital-human preview through the production mix path."""
    from .render_mix_adapter import (
        build_packaging_preview_mix_job,
        run_render_mix,
    )

    root = Path(project_root).resolve()
    output = _preview_output_path(
        root, current.identity.task_id, preview_id
    )
    work_dir = output.parent / f"{preview_id}-work"
    job = build_packaging_preview_mix_job(
        db,
        current.identity.task_id,
        current.identity.plan_id,
        work_dir,
        output,
        current.duration_seconds,
        subtitle_sources=_digital_subtitle_sources(
            db, current.identity.task_id
        ),
        subtitle_runner=runner,
    )
    rendered = run_render_mix(job, runner=runner)
    return {
        "output_path": rendered,
        "duration_seconds": current.duration_seconds,
        "coverage": list(job["packaging"].get("coverage") or []),
    }


def render_packaging_preview(
    db, preview_id: str, project_root: Path, runner=None
) -> dict:
    """Render one immutable preview identity without changing workflow gates."""
    preview = get_preview_artifact(db, preview_id)
    if preview is None:
        raise KeyError(f"找不到包装预览：{preview_id}")
    current = build_preview_input(db, preview["task_id"], preview["plan_id"])
    if preview_canonical_key(current.identity) != preview["canonical_key"]:
        return set_preview_status(
            db,
            preview_id,
            "stale",
            error_code="PACKAGING_PREVIEW_STALE",
        )
    # Generic job resume requeues the job, not its preview artifact. Re-enter
    # through queued after validating the same input identity, just as a new
    # preview request does. This never records viewing or confirms a gate.
    if preview["status"] in {"failed", "missing", "stale"}:
        set_preview_status(db, preview_id, "queued")
    set_preview_status(db, preview_id, "rendering")
    try:
        if current.task_type == "digital_human":
            result = render_digital_packaging_preview(
                db, current, Path(project_root), preview_id, runner
            )
        elif current.task_type == "ad_mix":
            from .ad_actions import render_ad_packaging_preview

            result = render_ad_packaging_preview(
                db, current, Path(project_root), preview_id, runner
            )
        else:  # pragma: no cover - build_preview_input already rejects it
            raise RuntimeError(
                f"UNSUPPORTED_ROUTE: 当前任务类型不支持包装预览：{current.task_type}"
            )
        refreshed = build_preview_input(
            db, preview["task_id"], preview["plan_id"]
        )
        if preview_canonical_key(refreshed.identity) != preview["canonical_key"]:
            return set_preview_status(
                db,
                preview_id,
                "stale",
                error_code="PACKAGING_PREVIEW_STALE",
            )
        output = Path(result["output_path"]).resolve()
        relative = output.relative_to(Path(project_root).resolve()).as_posix()
        ready = finalize_preview_artifact(
            db,
            preview_id,
            project_root=Path(project_root),
            rel_path=relative,
            duration_seconds=float(result["duration_seconds"]),
            coverage=list(result.get("coverage") or []),
        )
        return {
            **ready,
            "route": current.task_type,
            "profile": current.identity.preview_profile,
        }
    except Exception as error:
        latest = get_preview_artifact(db, preview_id)
        if latest is not None and latest["status"] == "rendering":
            code = str(error).partition(":")[0] or "PACKAGING_PREVIEW_FAILED"
            set_preview_status(
                db,
                preview_id,
                "failed",
                error_code=code,
                error_detail=str(error).partition(":")[0],
            )
        raise


def _decode_artifact(row) -> dict | None:
    if row is None:
        return None
    value = dict(row)
    value["coverage"] = json.loads(value.pop("coverage_json") or "[]")
    signature = value.pop("output_signature_json")
    value["output_signature"] = json.loads(signature) if signature else None
    return value


def get_preview_artifact(db, preview_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM packaging_preview_artifacts WHERE preview_id=?",
        (preview_id,),
    ).fetchone()
    return _decode_artifact(row)


def find_reusable_preview(db, identity: PreviewIdentity) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM packaging_preview_artifacts WHERE canonical_key=?",
        (preview_canonical_key(identity),),
    ).fetchone()
    return _decode_artifact(row)


def current_preview_for_plan(db, task_id: str, plan_id: str) -> dict | None:
    """Return the latest preview, projecting superseded input as stale."""
    current = build_preview_input(db, task_id, plan_id)
    row = db.connection.execute(
        "SELECT * FROM packaging_preview_artifacts "
        "WHERE task_id=? AND plan_id=? ORDER BY rowid DESC LIMIT 1",
        (task_id, plan_id),
    ).fetchone()
    preview = _decode_artifact(row)
    if preview is None:
        return None
    if preview["canonical_key"] != preview_canonical_key(current.identity):
        return {**preview, "status": "stale", "viewed_at": None}
    return preview


def create_preview_artifact(db, identity: PreviewIdentity) -> dict:
    key = preview_canonical_key(identity)
    with db.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM packaging_preview_artifacts WHERE canonical_key=?",
            (key,),
        ).fetchone()
        if existing is None:
            preview_id = f"pv-{uuid.uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO packaging_preview_artifacts(
                  preview_id,task_id,plan_id,canonical_key,picture_lock_digest,
                  plan_digest,renderer_version,preview_profile,status
                ) VALUES(?,?,?,?,?,?,?,?, 'queued')
                """,
                (
                    preview_id,
                    identity.task_id,
                    identity.plan_id,
                    key,
                    identity.picture_lock_digest,
                    identity.plan_digest,
                    identity.renderer_version,
                    identity.preview_profile,
                ),
            )
    result = find_reusable_preview(db, identity)
    if result is None:  # pragma: no cover - guarded by the insert/select above
        raise RuntimeError("PACKAGING_PREVIEW_CREATE_FAILED: 无法创建包装预览记录")
    return result


def _active_job_for_preview(db, preview_id: str) -> dict | None:
    from .workbench_worker import get_job

    rows = db.connection.execute(
        """
        SELECT j.job_id,p.payload_json
        FROM jobs j
        JOIN job_payloads p ON p.job_id=j.job_id
        WHERE j.job_type='packaging_preview'
          AND j.status IN ('queued','running')
        ORDER BY j.created_at,j.job_id
        """
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("preview_id") == preview_id:
            return get_job(db, row["job_id"])
    return None


def _next_preview_job_id(db, preview_id: str) -> str:
    base = f"packaging-preview-{preview_id}"
    existing = {
        row["job_id"]
        for row in db.connection.execute(
            "SELECT job_id FROM jobs WHERE job_id=? OR job_id LIKE ?",
            (base, f"{base}-r%"),
        ).fetchall()
    }
    if base not in existing:
        return base
    for retry in range(1, 100):
        candidate = f"{base}-r{retry}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("PACKAGING_PREVIEW_RETRY_LIMIT: 预览重试次数过多")


def _preview_request_projection(artifact: dict, job: dict | None) -> dict:
    return {
        "previewId": artifact["preview_id"],
        "planId": artifact["plan_id"],
        "status": artifact["status"],
        "profile": artifact["preview_profile"],
        "jobId": None if job is None else job["job_id"],
        "job": job,
    }


def request_preview(db, task_id: str, plan_id: str) -> dict:
    """Idempotently bind one canonical preview artifact to one active job."""
    from .workbench_worker import enqueue_job, get_job

    value = build_preview_input(db, task_id, plan_id)
    artifact = create_preview_artifact(db, value.identity)
    if artifact["status"] == "ready" and current_preview_matches(db, artifact):
        return _preview_request_projection(artifact, None)
    active = _active_job_for_preview(db, artifact["preview_id"])
    if active is not None:
        return _preview_request_projection(artifact, active)
    if artifact["status"] == "rendering":
        artifact = set_preview_status(
            db,
            artifact["preview_id"],
            "failed",
            error_code="PACKAGING_PREVIEW_JOB_ORPHANED",
        )
    if artifact["status"] in {"failed", "stale", "missing"}:
        artifact = set_preview_status(db, artifact["preview_id"], "queued")
    job_id = _next_preview_job_id(db, artifact["preview_id"])
    try:
        enqueue_job(
            db,
            job_id=job_id,
            job_type="packaging_preview",
            task_id=task_id,
            payload={"preview_id": artifact["preview_id"]},
            retry_policy="explicit",
        )
    except ValueError:
        active = _active_job_for_preview(db, artifact["preview_id"])
        if active is None:
            raise
        return _preview_request_projection(artifact, active)
    return _preview_request_projection(artifact, get_job(db, job_id))


_ALLOWED_TRANSITIONS = {
    "queued": {"rendering", "ready", "failed", "stale", "missing"},
    "rendering": {"ready", "failed", "stale", "missing"},
    "ready": {"stale", "missing"},
    "failed": {"queued"},
    "stale": {"queued"},
    "missing": {"queued"},
}


def set_preview_status(
    db,
    preview_id: str,
    status: str,
    *,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> dict:
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT status FROM packaging_preview_artifacts WHERE preview_id=?",
            (preview_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"找不到包装预览：{preview_id}")
        current = row["status"]
        if status != current and status not in _ALLOWED_TRANSITIONS[current]:
            raise RuntimeError(
                f"PACKAGING_PREVIEW_STATE_INVALID: {current} 不能变为 {status}"
            )
        connection.execute(
            """
            UPDATE packaging_preview_artifacts
            SET status=?, error_code=?, error_detail=?,
                viewed_at=CASE WHEN ?='ready' THEN viewed_at ELSE NULL END,
                updated_at=datetime('now')
            WHERE preview_id=?
            """,
            (status, error_code, error_detail, status, preview_id),
        )
    result = get_preview_artifact(db, preview_id)
    if result is None:  # pragma: no cover
        raise KeyError(f"找不到包装预览：{preview_id}")
    return result


def _resolve_cache_path(project_root: Path, rel_path: str) -> Path:
    relative = Path(rel_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("PACKAGING_PREVIEW_PATH_INVALID: 预览路径无效")
    root = (Path(project_root).resolve() / PREVIEW_CACHE_RELATIVE_ROOT).resolve()
    resolved = (Path(project_root).resolve() / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RuntimeError(
            "PACKAGING_PREVIEW_PATH_INVALID: 预览不在受控缓存目录"
        ) from error
    if resolved.suffix.lower() != ".mp4":
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_INVALID: 预览必须是 MP4")
    return resolved


def finalize_preview_artifact(
    db,
    preview_id: str,
    *,
    project_root: Path,
    rel_path: str,
    duration_seconds: float,
    coverage: list[dict],
) -> dict:
    media_path = _resolve_cache_path(Path(project_root), rel_path)
    if not media_path.is_file():
        set_preview_status(
            db,
            preview_id,
            "missing",
            error_code="PACKAGING_PREVIEW_MEDIA_MISSING",
        )
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
    signature = file_content_signature(media_path)
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT status FROM packaging_preview_artifacts WHERE preview_id=?",
            (preview_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"找不到包装预览：{preview_id}")
        if "ready" not in _ALLOWED_TRANSITIONS[row["status"]]:
            raise RuntimeError(
                f"PACKAGING_PREVIEW_STATE_INVALID: {row['status']} 不能变为 ready"
            )
        connection.execute(
            """
            UPDATE packaging_preview_artifacts
            SET status='ready', rel_path=?, output_signature_json=?,
                duration_seconds=?, size_bytes=?, coverage_json=?,
                error_code=NULL, error_detail=NULL, viewed_at=NULL,
                updated_at=datetime('now')
            WHERE preview_id=?
            """,
            (
                Path(rel_path).as_posix(),
                json.dumps(signature, sort_keys=True),
                float(duration_seconds),
                signature["size_bytes"],
                json.dumps(coverage, ensure_ascii=False, sort_keys=True),
                preview_id,
            ),
        )
    result = get_preview_artifact(db, preview_id)
    if result is None:  # pragma: no cover
        raise KeyError(f"找不到包装预览：{preview_id}")
    return result


def mark_preview_viewed(db, task_id: str, preview_id: str) -> dict:
    with db.transaction(immediate=True) as connection:
        changed = connection.execute(
            """
            UPDATE packaging_preview_artifacts
            SET viewed_at=datetime('now'), updated_at=datetime('now')
            WHERE preview_id=? AND task_id=? AND status='ready'
            """,
            (preview_id, task_id),
        ).rowcount
    if changed != 1:
        raise RuntimeError("PACKAGING_PREVIEW_NOT_READY: 预览尚未完成")
    result = get_preview_artifact(db, preview_id)
    if result is None:  # pragma: no cover
        raise KeyError(f"找不到包装预览：{preview_id}")
    return result


def resolve_packaging_preview_media(
    db, preview_id: str, project_root: Path
) -> Path:
    preview = get_preview_artifact(db, preview_id)
    if preview is None:
        raise KeyError(f"找不到包装预览：{preview_id}")
    if preview["status"] != "ready" or not preview.get("rel_path"):
        if preview["status"] == "missing":
            raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
        raise RuntimeError("PACKAGING_PREVIEW_NOT_READY: 预览尚未完成")
    media_path = _resolve_cache_path(Path(project_root), preview["rel_path"])
    if not media_path.is_file():
        set_preview_status(
            db,
            preview_id,
            "missing",
            error_code="PACKAGING_PREVIEW_MEDIA_MISSING",
        )
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_MISSING: 预览文件不存在")
    actual = file_content_signature(media_path)
    if actual != preview["output_signature"]:
        raise RuntimeError("PACKAGING_PREVIEW_MEDIA_CHANGED: 预览文件内容已变化")
    return media_path
