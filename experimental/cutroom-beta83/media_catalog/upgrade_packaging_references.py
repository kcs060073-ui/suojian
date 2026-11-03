"""Read-only adapters for recorded packaging references; never resume work.

``bind`` owns byte observation and deduplication. This module supplies only
original, recorded signatures. Unknown layouts are reported through ``pending``.
The returned auditComplete stays false: inventory is not recovery approval.
"""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

from .upgrade_recovery import RecoveryPlanError, _plain_path


_DIGEST = re.compile(r"[0-9a-f]{64}")
_PREVIEW_VERSIONS = {"packaging-preview-v1"} | {
    f"{route}-packaging-preview-{version}{suffix}"
    for route in ("ad", "digital")
    for version in ("v4-recipe", "v5-recipe-music-v1", "v6-recipe-continuous-ass-layout")
    for suffix in ("", "-montage")
}
_JOB_FIELDS = {
    "packaging_preview": {"preview_id"},
    "render_and_qa": {"action", "task_id", "qa_rework_id", "intelligent_review"},
    "build_publish_package": {"action", "task_id", "candidate_id", "platform", "title",
                              "description", "tags", "cover_choice", "external_cover_path",
                              "expected_external_cover_signature", "delivery_type", "output_root"},
}
_JOB_ADAPTERS = {"packaging_preview": "packaging-preview-request/v1",
                 "render_and_qa": "render-and-qa-request/v1",
                 "build_publish_package": "publish-package-request/v1"}


def _fail(code, message):
    raise RecoveryPlanError(code, message)


def _object(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                _fail("FROZEN_RECORD_INVALID", "冻结记录包含重复字段。")
            result[key] = item
        return result
    try:
        result = json.loads(value, object_pairs_hook=pairs,
                            parse_constant=lambda _: _fail("FROZEN_RECORD_INVALID", "冻结记录含非有限数值。"))
    except (TypeError, json.JSONDecodeError) as error:
        raise RecoveryPlanError("FROZEN_RECORD_INVALID", "冻结记录不是有效 JSON。") from error
    if not isinstance(result, dict):
        _fail("FROZEN_RECORD_INVALID", "冻结记录不是对象。")
    return result


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _known_review_options(value):
    # Historical request only. Do not consult mutable current model settings or
    # dispatch any review while inventorying an upgrade. The enclosing payload
    # digest still authenticates these exact saved options.
    if not isinstance(value, dict):
        return False
    if set(value) == {"setup_error"}:
        reason = value["setup_error"]
        return isinstance(reason, str) and reason.partition(":")[0] in {
            "VISUAL_PROFILE_UNVERIFIED", "VISUAL_PROFILE_INVALID",
            "INTELLIGENT_REVIEW_SETUP", "AUDIO_REVIEW_ACCOUNT_INVALID",
        }
    if set(value) != {"profile_id", "profile_digest", "audio_account_id", "audio_account_digest"}:
        return False
    return (all(isinstance(value[k], str) and value[k].strip() and "\0" not in value[k]
                for k in ("profile_id", "audio_account_id"))
            and all(_digest(value[k]) for k in ("profile_digest", "audio_account_digest")))


def _signature(value):
    if (not isinstance(value, dict) or set(value) != {"size_bytes", "sha256"}
            or type(value["size_bytes"]) is not int or value["size_bytes"] < 0
            or not _digest(value["sha256"])):
        _fail("FROZEN_SIGNATURE_INVALID", "冻结文件缺少有效原始字节签名。")
    return value


def _address(value, root=None, allowed=(), relative_only=False):
    if not isinstance(value, str) or not value or "\0" in value:
        _fail("FROZEN_PATH_INVALID", "冻结地址无效。")
    path = Path(value)
    windows = PureWindowsPath(value)
    if ".." in path.parts or ".." in windows.parts:
        _fail("FROZEN_PATH_INVALID", "冻结地址不能越过登记目录。")
    if relative_only and (path.is_absolute() or windows.drive or windows.root):
        _fail("FROZEN_PATH_INVALID", "预览地址必须相对原项目目录。")
    if not path.is_absolute():
        if root is None or windows.drive or windows.root:
            _fail("FROZEN_PATH_INVALID", "冻结地址缺少有效归属。")
        path = root / path
    if path.name.lower() in {".env", "session-signing.key"}:
        _fail("FROZEN_PATH_INVALID", "冻结地址不能指向凭据。")
    path = _plain_path(path)
    if allowed and not any(path.is_relative_to(folder) for folder in allowed):
        _fail("FROZEN_PATH_INVALID", "冻结地址不属于指定素材或预览目录。")
    return path


def inspect_packaging_references(db, source_root, tables, bind, pending, *, recognized_voice_job_ids=(), recognized_workflow_job_ids=()) -> dict:
    """Inspect original records using ROOT's guarded binding callbacks.

    Does not read manifests to invent signatures, copy files, migrate records,
    recreate receipts, transfer approvals, or dispatch jobs. Results contain
    identifiers and evidence metadata only, never request copy or credentials.
    """
    root = _plain_path(source_root)
    report = {"phase": "packaging-reference-inspection", "previews": [], "jobs": [],
              "unresolved": [], "auditComplete": False}

    def gap(table, identity, code):
        report["unresolved"].append({"table": table, "rowId": identity, "code": code})
        pending(table, identity, code)

    def rows(table):
        if table not in tables:
            return []
        return [dict(row) for row in db.connection.execute(f'SELECT * FROM "{table}"')]

    previews = {row["preview_id"]: row for row in rows("packaging_preview_artifacts")}
    for identity, row in previews.items():
        table = "packaging_preview_artifacts"
        version = row.get("renderer_version")
        report["previews"].append({"previewId": identity, "taskId": row.get("task_id"),
                                   "planId": row.get("plan_id"), "status": row.get("status"),
                                   "rendererVersion": version})
        if version not in _PREVIEW_VERSIONS:
            gap(table, identity, "PREVIEW_RENDERER_UNSUPPORTED")
            continue
        rel = row.get("rel_path")
        if rel:
            path = _address(rel, root, (root / "runtime/workbench/cache/packaging-previews",), True)
            if path.suffix.lower() != ".mp4":
                _fail("FROZEN_PATH_INVALID", "预览地址不是 MP4。")
            if not row.get("output_signature_json"):
                gap(table, identity, "PREVIEW_SIGNATURE_UNAVAILABLE")
            else:
                expected = _signature(_object(row["output_signature_json"]))
                if row.get("size_bytes") is not None and row["size_bytes"] != expected["size_bytes"]:
                    _fail("FROZEN_BINDING_INVALID", "预览大小与原签名不一致。")
                bind(table, identity, "rel_path", str(path), expected)
        else:
            gap(table, identity, "PREVIEW_OUTPUT_UNAVAILABLE")
        if not row.get("preview_receipt_json"):
            gap(table, identity, "PREVIEW_RECEIPT_UNAVAILABLE")
            continue
        receipt = _object(row["preview_receipt_json"])
        if set(receipt) - {"recipe_digest", "recipe_content_digest", "covered_output_intervals", "asset_versions"}:
            gap(table, identity, "PREVIEW_RECEIPT_VERSION_UNSUPPORTED")
            continue
        if not _digest(receipt.get("recipe_digest")) or (
                version.startswith("digital-packaging-preview-") and
                not _digest(receipt.get("recipe_content_digest"))):
            gap(table, identity, "PREVIEW_RECIPE_SIGNATURE_UNAVAILABLE")
            continue
        versions = receipt.get("asset_versions")
        if not isinstance(versions, list) or not versions:
            gap(table, identity, "PREVIEW_ASSET_SIGNATURE_UNAVAILABLE")
            continue
        for index, asset in enumerate(versions):
            if not isinstance(asset, dict) or set(asset) != {"kind", "path", "size_bytes", "sha256"}:
                gap(table, identity, "PREVIEW_ASSET_SIGNATURE_UNAVAILABLE")
                continue
            address = _address(asset["path"])
            expected = _signature({key: asset[key] for key in ("size_bytes", "sha256")})
            bind(table, identity, f"preview_receipt_json/asset_versions/{index}/path", str(address), expected)

    for row in rows("packaging_plan_items"):
        identity = row["item_id"]
        if str(row.get("asset_ref") or "").startswith("local-bgm-"):
            gap("packaging_plan_items", identity, "LOCAL_BGM_ADAPTER_PENDING")
        elif row.get("asset_file"):
            _address(row["asset_file"], root, tuple(root / name for name in ("public", "assets", "asset-packs")))
            # asset_version is a manifest/version label, not a byte signature.
            gap("packaging_plan_items", identity, "PACKAGING_ITEM_SIGNATURE_UNAVAILABLE")
        else:
            gap("packaging_plan_items", identity, "PACKAGING_MANIFEST_RESOLUTION_PENDING")
    for row in rows("packaging_plan_instances"):
        if str(row.get("asset_ref") or "").startswith("local-bgm-"):
            gap("packaging_plan_instances", row["instance_id"], "LOCAL_BGM_ADAPTER_PENDING")
    for row in rows("packaging_assets"):
        if row.get("source_path"):
            gap("packaging_assets", row["asset_id"], "PACKAGING_SOURCE_OWNERSHIP_UNKNOWN")

    jobs = {row["job_id"]: row for row in rows("jobs")}
    tasks = {row["task_id"]: row for row in rows("tasks")}
    candidates = {row["candidate_id"]: row for row in rows("candidate_versions")}
    reworks = {row["rework_id"]: row for row in rows("rework_requests")}
    for row in rows("job_payloads"):
        identity, table = row["job_id"], "job_payloads"
        job = jobs.get(identity)
        if not job:
            _fail("FROZEN_BINDING_INVALID", "任务请求没有对应任务。")
        kind, task = job.get("job_type"), job.get("task_id")
        summary = {"jobId": identity, "taskId": task, "jobType": kind,
                   "status": job.get("status"), "cursor": job.get("cursor"),
                   "payloadDigest": row.get("payload_digest"), "adapter": None}
        report["jobs"].append(summary)
        if identity in recognized_workflow_job_ids:
            summary["adapter"] = "workflow-inventory/v1"
            continue
        if kind == "tts" and identity in recognized_voice_job_ids:
            summary["adapter"] = "voice-editing-request/v1"
            continue
        if job.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
            gap(table, identity, "JOB_NOT_TERMINAL")
            continue
        if kind not in _JOB_FIELDS:
            gap(table, identity, "JOB_TYPE_UNSUPPORTED")
            continue
        payload = _object(row.get("payload_json"))
        if set(payload) - _JOB_FIELDS[kind]:
            gap(table, identity, "JOB_PAYLOAD_VERSION_UNSUPPORTED")
            continue
        if not _digest(row.get("payload_digest")):
            gap(table, identity, "JOB_PAYLOAD_SIGNATURE_UNAVAILABLE")
            continue
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if digest != row["payload_digest"]:
            _fail("FROZEN_BINDING_INVALID", "任务请求与原摘要不一致。")
        if payload.get("action", kind) != kind or payload.get("task_id", task) != task:
            _fail("FROZEN_BINDING_INVALID", "任务请求类型或归属不一致。")
        if tasks.get(task, {}).get("task_type") not in {"ad_mix", "digital_human"}:
            gap(table, identity, "JOB_TASK_ROUTE_UNSUPPORTED")
            continue
        if kind == "render_and_qa" and "intelligent_review" in payload:
            if not _known_review_options(payload["intelligent_review"]):
                gap(table, identity, "JOB_REVIEW_OPTIONS_UNSUPPORTED")
                continue
        summary["adapter"] = _JOB_ADAPTERS[kind]
        cursors = {kind + ":complete"}
        if kind == "render_and_qa":
            cursors.add("render_and_qa:qa_blocked")
        if job.get("status") == "completed" and job.get("cursor") not in cursors:
            gap(table, identity, "JOB_RESULT_VERSION_UNSUPPORTED")
        if kind == "packaging_preview":
            preview = previews.get(payload.get("preview_id"))
            if not preview or preview.get("task_id") != task:
                _fail("FROZEN_BINDING_INVALID", "预览请求与原预览归属不一致。")
            summary["previewId"] = payload["preview_id"]
        elif kind == "render_and_qa" and payload.get("qa_rework_id"):
            if reworks.get(payload["qa_rework_id"], {}).get("task_id") != task:
                _fail("FROZEN_BINDING_INVALID", "质检重做引用归属不一致。")
            summary["qaReworkId"] = payload["qa_rework_id"]
        elif kind == "build_publish_package":
            candidate = candidates.get(payload.get("candidate_id"))
            if not candidate or candidate.get("task_id") != task:
                _fail("FROZEN_BINDING_INVALID", "导出请求与原候选版本归属不一致。")
            summary["candidateId"] = payload["candidate_id"]
            if payload.get("external_cover_path"):
                address = _address(payload["external_cover_path"])
                expected = payload.get("expected_external_cover_signature")
                if expected is None:
                    gap(table, identity, "JOB_COVER_SIGNATURE_UNAVAILABLE")
                else:
                    bind(table, identity, "payload_json/external_cover_path", str(address), _signature(expected))
            if payload.get("output_root"):
                _address(payload["output_root"])
                gap(table, identity, "JOB_OUTPUT_DIRECTORY_BINDING_UNRESOLVED")
        # Worker stores cursor/progress/phase only, not its transient result dict.
        # Absence of a never-persisted transient dictionary is not a missing
        # reference. Persisted preview/candidate/package evidence has its own
        # adapter; do not turn this known storage layout into an audit blocker.
        summary["resultPersistence"] = "cursor_progress_only"
    return report
