"""Read-only inventory of frozen outputs; never rewrite paths or approval evidence.

Only explicit output/QA/committed-package fields are supported here. Explicit adapters inspect nested render, packaging, catalog and job references.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .path_utils import filesystem_path
from .publish_integrity import inspect_package, manifest_files, safe_name
from .upgrade_recovery import _fail, _plain_path


def _object(value):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        _fail("FROZEN_RECORD_INVALID", "冻结产物记录不是有效对象，请保留旧安装核对。")
    if not isinstance(result, dict):
        _fail("FROZEN_RECORD_INVALID", "冻结产物记录不是有效对象，请保留旧安装核对。")
    return result


def _signature(value):
    if (not isinstance(value, dict) or set(value) != {"size_bytes", "sha256"}
            or type(value["size_bytes"]) is not int or value["size_bytes"] < 0
            or not isinstance(value["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None):
        _fail("FROZEN_SIGNATURE_INVALID", "冻结产物的已登记内容摘要不完整或无效。")
    return value


def _absolute(value):
    if (not isinstance(value, str) or not value or "\0" in value
            or not Path(value).is_absolute() or ".." in Path(value).parts
            or Path(value).name.lower() in {".env", "session-signing.key"}):
        _fail("FROZEN_PATH_INVALID", "冻结产物缺少明确原地址或包含不安全路径。")
    return _plain_path(value)


def _observe(path):
    path = _plain_path(path)
    native = filesystem_path(path)
    try:
        before = native.lstat()
        if not stat.S_ISREG(before.st_mode):
            _fail("FROZEN_FILE_UNAVAILABLE", "冻结产物不是可读的普通文件。")
        digest = hashlib.sha256()
        with native.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                _fail("SOURCE_CHANGED", "冻结产物在打开时被替换，请先停止原安装。")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        _plain_path(path)
        after = native.lstat()
    except OSError:
        _fail("FROZEN_FILE_UNAVAILABLE", "冻结产物缺失或无法读取，请恢复原文件后核对。")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        _fail("SOURCE_CHANGED", "冻结产物在盘点期间变化，请先停止原安装。")
    return {"size_bytes": before.st_size, "sha256": digest.hexdigest()}


def inspect_frozen_references(db, source_root, tables, *, original_reference_root=None):
    """Inspect all historical rows, including expired packages, without writes.

    A successful partial report never grants snapshot/switch permission. The
    original addresses are retained on each binding even for identical bytes.
    """
    source_root = Path(source_root)
    files, bindings, unresolved = {}, [], []

    def pending(table, identity, code):
        unresolved.append({"table": table, "rowId": identity, "code": code})

    def bind(table, identity, field, value, expected):
        path = _absolute(value)
        expected = _signature(expected)
        key = str(path)
        if key not in files:
            actual = _observe(path)
            if actual != expected:
                _fail("FROZEN_FILE_CHANGED", "冻结产物与原登记内容不一致，不能重新签名替代。")
            files[key] = {"path": key, "bytes": actual["size_bytes"], "sha256": actual["sha256"],
                          "scope": "old_installation" if path.is_relative_to(source_root) else "external"}
        elif (files[key]["bytes"], files[key]["sha256"]) != (expected["size_bytes"], expected["sha256"]):
            _fail("FROZEN_BINDING_INVALID", "同一冻结文件地址绑定了不一致的版本摘要。")
        bindings.append({"table": table, "rowId": identity, "field": field,
                         "recordedPath": value, "path": key, "sha256": expected["sha256"],
                         "evidenceGrade": "original_recorded_signature"})
        return path

    def bind_render(table, identity, field, value, expected):
        # Frozen render identity and physical backup hashes are distinct.
        from .runtime_artifacts import compiled_signature, PY_MARKER, TS_MARKER
        path = _absolute(value)
        expected = _signature(expected)
        before = {path: _observe(path)}
        marker = PY_MARKER if path.suffix == '.py' else TS_MARKER if path.suffix == '.ts' else None
        if marker is not None:
            with filesystem_path(path).open('rb') as stream:
                marked = stream.readline(100).rstrip(b'\r\n') == marker.rstrip(b'\n')
            if marked:
                payload = _absolute(str(path.with_suffix('.pyc' if path.suffix == '.py' else '.js')))
                before[payload] = _observe(payload)
        try:
            logical = compiled_signature(path)
        except OSError:
            _fail('FROZEN_FILE_UNAVAILABLE', '渲染编译组件缺失或不可读取。')
        if logical is None:
            return bind(table, identity, field, value, expected)
        if _signature({key:logical[key] for key in ('size_bytes','sha256')}) != expected:
            _fail('FROZEN_FILE_CHANGED', '渲染运行组件与原冻结身份不一致。')
        for physical, signature in before.items():
            if _observe(physical) != signature:
                _fail('SOURCE_CHANGED', '渲染组件在校验期间变化。')
            bind(table, identity, field if physical == path else field+'/compiledPayload', str(physical), signature)
            bindings[-1].update(evidenceGrade='verified_render_identity_raw_backup', renderSignature=expected)
        return path

    rows = lambda table: [dict(r) for r in db.connection.execute(f"SELECT * FROM {table}")] if table in tables else []
    snapshots = {r["snapshot_id"]: r for r in rows("render_snapshots")}
    candidates = {r["candidate_id"]: r for r in rows("candidate_versions")}
    qa_reports = {r["report_id"]: r for r in rows("qa_reports")}
    signatures = {}
    owners = {}
    for identity, row in snapshots.items():
        if row.get("producer") not in {"assemble-v1", "assemble-ad-v1"} or row.get("stage") not in {"mix", "qa"}:
            pending("render_snapshots", identity, "SNAPSHOT_PRODUCER_UNSUPPORTED")
            continue
        params = _object(row.get("params_json"))
        if params.get("dependency_digest") != row.get("input_digest") or not row.get("input_digest"):
            _fail("FROZEN_BINDING_INVALID", "冻结快照与原依赖摘要不一致。")
        signature = params.get("output_signature")
        path = bind("render_snapshots", identity, "output_path", row.get("output_path"), signature)
        signatures[str(path)] = signature
        owners.setdefault(str(path), set()).add(row["task_id"])

    for identity, row in candidates.items():
        mix = snapshots.get(row["mix_snapshot_id"])
        if (mix is None or mix["task_id"] != row["task_id"] or mix["stage"] != "mix"
                or mix.get("input_digest") != row["input_digest"]
                or _absolute(mix.get("output_path")) != _absolute(row["output_path"])):
            _fail("FROZEN_BINDING_INVALID", "候选成片未绑定同任务、同版本的原渲染快照。")
        signature = _object(row["output_signature_json"])
        path = bind("candidate_versions", identity, "output_path", row["output_path"], signature)
        signatures[str(path)] = signature
        owners.setdefault(str(path), set()).add(row["task_id"])

        if row.get("qa_report_id"):
            report = qa_reports.get(row["qa_report_id"])
            if (report is None or report["task_id"] != row["task_id"]
                    or _absolute(report.get("video_path")) != path):
                _fail("FROZEN_BINDING_INVALID", "候选质检报告未绑定本版成片，不能串用历史报告。")

        if row.get("qa_snapshot_id"):
            qa = snapshots.get(row["qa_snapshot_id"])
            if (qa is None or qa["task_id"] != row["task_id"] or qa["stage"] != "qa"
                    or qa.get("parent_snapshot") != row["mix_snapshot_id"]
                    or qa.get("producer") != mix.get("producer")
                    or qa.get("input_digest") != row["input_digest"]
                    or _absolute(qa.get("output_path")) != path
                    or _object(qa.get("params_json")).get("qa_report_id") != row.get("qa_report_id")
                    or not row.get("qa_report_id")):
                _fail("FROZEN_BINDING_INVALID", "候选质检快照未保留原成片版本与父快照。")

    for row in qa_reports.values():
        identity = row["report_id"]
        raw = row.get("video_path")
        report = _object(row.get("report_json"))
        if not raw or not isinstance(raw, str) or not Path(raw).is_absolute():
            pending("qa_reports", identity, "QA_ADDRESS_BASE_UNSUPPORTED")
            continue
        path = _absolute(raw)
        if report.get("video_path") != raw:
            _fail("FROZEN_BINDING_INVALID", "质检记录与报告内原视频地址不一致。")
        signature = signatures.get(str(path))
        if signature is None:
            pending("qa_reports", identity, "QA_OUTPUT_SIGNATURE_UNAVAILABLE")
        else:
            if row["task_id"] not in owners.get(str(path), set()):
                _fail("FROZEN_BINDING_INVALID", "质检记录的视频地址只属于另一任务。")
            bind("qa_reports", identity, "video_path", raw, signature)

    receipts = {r["record_id"]: r for r in rows("publish_manifests")}
    for record in rows("publish_records"):
        identity = record["record_id"]
        receipt = receipts.get(identity)
        if receipt is None:
            pending("publish_records", identity, "PUBLISH_RECEIPT_UNAVAILABLE")
            continue
        cover_path = _absolute(record.get("cover_path"))
        folder = cover_path.parent
        manifest = _object(receipt["manifest_json"])
        try:
            names = manifest_files(manifest)
            if (manifest.get("delivery_type") not in {"standard", "editable"}
                    or any(manifest.get(key) != record[key] for key in ("record_id", "task_id", "candidate_id"))
                    or cover_path.name != manifest["cover"]["path"]):
                raise ValueError("package identity mismatch")
            # Guard every path before the existing reader opens any content.
            for name in names:
                _plain_path(folder / name)
            integrity = inspect_package(db, record, filesystem_path(folder))
            if not integrity["manifest_registered"] or integrity["missing_files"] or integrity["changed_files"]:
                raise ValueError("package changed or missing")
            candidate = candidates.get(record.get("candidate_id"))
            if candidate is None or candidate["task_id"] != record["task_id"]:
                raise ValueError("package candidate mismatch")
            candidate_signature = _object(candidate["output_signature_json"])
            if manifest["file_signatures"]["video-final.mp4"] != candidate_signature:
                raise ValueError("package video is not the original candidate")
            if _absolute(manifest.get("qa_video_path")) != _absolute(candidate["output_path"]):
                raise ValueError("package QA address mismatch")
            for name in names:
                signature = _object(receipt["manifest_signature_json"]) if name == "publish-check.json" else manifest["file_signatures"].get(name)
                bind("publish_manifests", identity, "files/" + name, str(folder / name), signature)
            if manifest["cover"].get("mode") == "external":
                bind("publish_manifests", identity, "cover/source_path", manifest["cover"].get("source_path"), manifest["cover"].get("source_signature"))
            if manifest["delivery_type"] == "editable":
                editable = _object(filesystem_path(folder / "delivery-manifest.json").read_text(encoding="utf8"))
                if editable.get("schema") != "cutroom.editable-delivery/v1":
                    pending("publish_manifests", identity, "EDITABLE_DELIVERY_VERSION_UNSUPPORTED")
                    continue
                components = editable.get("components")
                allowed = {"final_video", "base_video", "subtitles", "voice_or_original_audio", "bgm_stem", "sfx_stem", "material_timecodes"}
                if (editable.get("candidate_id") != candidate["candidate_id"]
                        or not isinstance(components, dict) or set(components) != allowed):
                    raise ValueError("editable identity mismatch")
                final = components["final_video"]
                if (not isinstance(final, dict) or final.get("status") != "included"
                        or final.get("path") != "video-final.mp4"
                        or final.get("signature") != candidate_signature):
                    raise ValueError("editable final video mismatch")
                for component, item in components.items():
                    if not isinstance(item, dict) or item.get("status") not in {"included", "not_available"}:
                        raise ValueError("editable status unsupported")
                    if item["status"] == "not_available":
                        continue
                    name = item.get("path")
                    if not safe_name(name) or name not in names or item.get("signature") != manifest["file_signatures"].get(name):
                        raise ValueError("editable component mismatch")
                    if item.get("source_path"):
                        bind("publish_manifests", identity, "components/" + component + "/source_path", item["source_path"], item["signature"])
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            _fail("FROZEN_PACKAGE_INVALID", "交付包缺项、内容或原绑定变化，不能作为完整恢复材料。")

    from .upgrade_packaging_references import inspect_packaging_references
    from .upgrade_catalog_references import inspect_catalog_references
    from .upgrade_render_references import inspect_render_references, RenderReferenceError
    from .upgrade_current_references import complete_current_references
    from .upgrade_voice_job_references import inspect_voice_job_references
    from .upgrade_workflow_job_references import inspect_workflow_job_references
    from .upgrade_source_continue_references import inspect_source_continue_jobs
    workflow = inspect_workflow_job_references(
        db, source_root, tables, bind, pending, original_reference_root=original_reference_root)
    source_continue = inspect_source_continue_jobs(db, source_root, tables, bind, pending)
    from .upgrade_catalog_job_references import inspect_catalog_job_references
    catalog_jobs = inspect_catalog_job_references(db, source_root, tables, bind, pending)
    from .upgrade_storyboard_job_references import inspect_storyboard_job_references
    from .upgrade_packaging_job_references import inspect_packaging_job_references
    storyboard_jobs = inspect_storyboard_job_references(db, source_root, tables, bind, pending)
    packaging_jobs = inspect_packaging_job_references(db, source_root, tables, bind, pending)
    from .upgrade_creative_job_references import inspect_creative_job_references
    creative_jobs = inspect_creative_job_references(db, source_root, tables, bind, pending)
    from .upgrade_review_job_references import inspect_review_job_references
    review_jobs = inspect_review_job_references(db, source_root, tables, bind, pending)
    workflow_reports = {"reviewJobs": review_jobs, "creativeJobs": creative_jobs, "workflow": workflow, "sourceContinue": source_continue, "catalogJobs": catalog_jobs,
                        "storyboardJobs": storyboard_jobs, "packagingJobs": packaging_jobs}
    recognized_workflow = {identity for report in workflow_reports.values() for identity in report["recognizedJobIds"]}
    voice = inspect_voice_job_references(db, source_root, tables, bind, pending)
    packaging = inspect_packaging_references(
        db, source_root, tables, bind, pending,
        recognized_voice_job_ids=frozenset(voice["recognizedJobIds"]),
        recognized_workflow_job_ids=recognized_workflow,
    )
    catalog = inspect_catalog_references(db, source_root, tables, bind, pending)
    try:
        render = inspect_render_references(db, original_reference_root or source_root, tables, bind_render, pending)
    except RenderReferenceError as error:
        _fail(error.code, str(error))

    def observe_current(table, identity, field, value):
        path = _absolute(value)
        actual = _observe(path)
        result = bind(table, identity, field, value, actual)
        bindings[-1]['evidenceGrade'] = 'current_backup_observation'
        return result

    for report in [*workflow_reports.values(), voice]:
        for ref in report.get("managedFileReferences", []):
            if "table" not in ref:  # Registered voice inventory is read by its formal reader.
                continue
            actual = _observe(_absolute(ref["path"]))
            if ref.get("sha256") is not None and actual["sha256"] != ref["sha256"]:
                _fail("FROZEN_FILE_CHANGED", "Recorded workflow artifact changed during inventory.")
            observe_current(ref["table"], ref["rowId"], ref["field"], ref["path"])
    unresolved = complete_current_references(db, source_root, tables, catalog, packaging,
                                             unresolved, observe_current)
    # Stable ordering makes source-plan comparison independent of SELECT order.
    unresolved = sorted(unresolved, key=lambda item: (item['table'], str(item['rowId']), item['code']))
    from .upgrade_installed_references import classify_installed_files
    installed = classify_installed_files(source_root, files)
    for path, row in files.items():
        if _observe(Path(path)) != {"size_bytes": row["bytes"], "sha256": row["sha256"]}:
            _fail("SOURCE_CHANGED", "冻结产物在盘点期间变化，请保留原安装。")
    return {"phase": "complete-reference-inspection", "files": [files[k] for k in sorted(files)],
            "bindings": bindings, "installedRelease": installed, "unresolved": unresolved, "auditComplete": not unresolved,
            "adapters": {"packaging": packaging, "catalog": catalog, "render": render, "voice": voice, **workflow_reports},
            "supported": ["candidate_outputs", "render_dependencies", "packaging_previews_and_current_assets",
                          "catalog_and_local_bgm", "terminal_job_requests", "historical_review_relations",
                          "registered_standard_and_editable_packages"],
            "remainingAdapters": sorted({item['code'] for item in unresolved})}
