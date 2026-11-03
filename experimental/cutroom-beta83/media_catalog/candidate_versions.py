"""Durable candidate versions layered over trusted render and QA snapshots."""

from __future__ import annotations

from contextlib import nullcontext

import json
import os
from pathlib import Path
import uuid


def _required(value: dict, key: str):
    result = value.get(key)
    if result is None or result == "":
        raise ValueError(f"CANDIDATE_FIELD_REQUIRED: {key}")
    return result


def _decoded_json(value, fallback):
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _public_candidate(connection, row) -> dict:
    result = dict(row)
    result["output_signature"] = _decoded_json(
        result.pop("output_signature_json", None), {}
    )
    result["version_label"] = f"candidate-v{result['version_no']}"
    # Historical migration checkpoints may recover candidates before v079 exists.
    has_invalidations = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='candidate_invalidations' AND type='view'"
    ).fetchone() is not None
    invalidation = connection.execute(
        "SELECT stage_key,reason,created_at FROM candidate_invalidations WHERE candidate_id=?",
        (result["candidate_id"],),
    ).fetchone() if has_invalidations else None
    result["invalidation"] = None if invalidation is None else dict(invalidation)
    if result.get("qa_report_id"):
        qa_row = connection.execute(
            "SELECT report_json FROM qa_reports WHERE report_id=? AND task_id=?",
            (result["qa_report_id"], result["task_id"]),
        ).fetchone()
        result["qa_report"] = (
            None if qa_row is None else _decoded_json(qa_row["report_json"], None)
        )
    else:
        result["qa_report"] = None
    return result


def get_candidate(db, task_id: str, candidate_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM candidate_versions WHERE task_id=? AND candidate_id=?",
        (task_id, candidate_id),
    ).fetchone()
    return None if row is None else _public_candidate(db.connection, row)


def list_candidates(db, task_id: str) -> list[dict]:
    rows = db.connection.execute(
        """SELECT * FROM candidate_versions
        WHERE task_id=? ORDER BY version_no DESC""",
        (task_id,),
    ).fetchall()
    return [_public_candidate(db.connection, row) for row in rows]


def candidate_for_snapshot(db, task_id: str, snapshot_id: str) -> dict | None:
    row = db.connection.execute(
        """SELECT * FROM candidate_versions
        WHERE task_id=? AND (mix_snapshot_id=? OR qa_snapshot_id=?)
        ORDER BY version_no DESC LIMIT 1""",
        (task_id, snapshot_id, snapshot_id),
    ).fetchone()
    return None if row is None else _public_candidate(db.connection, row)


def ensure_candidate_version(
    db,
    task_id: str,
    render_snapshot: dict,
    *,
    change_summary: str | None = None,
) -> dict:
    """Create one immutable candidate identity for one trusted mix snapshot."""
    mix_snapshot_id = str(_required(render_snapshot, "mix_snapshot_id"))
    input_digest = str(_required(render_snapshot, "dependency_digest"))
    output_path = str(Path(_required(render_snapshot, "output_path")).resolve())
    output_signature = _required(render_snapshot, "output_signature")
    if not isinstance(output_signature, dict) or not output_signature.get("sha256"):
        raise ValueError("CANDIDATE_SIGNATURE_REQUIRED: 缺少候选成片签名")

    with (nullcontext(db.connection) if db.connection.in_transaction else db.transaction(immediate=True)) as connection:
        existing = connection.execute(
            "SELECT * FROM candidate_versions WHERE mix_snapshot_id=?",
            (mix_snapshot_id,),
        ).fetchone()
        if existing is not None:
            if existing["task_id"] != task_id:
                raise RuntimeError("CANDIDATE_TASK_MISMATCH: 渲染快照已绑定其他任务")
            return _public_candidate(connection, existing)

        snapshot_row = connection.execute(
            """SELECT task_id,stage,input_digest,output_path,producer
            FROM render_snapshots WHERE snapshot_id=?""",
            (mix_snapshot_id,),
        ).fetchone()
        if (
            snapshot_row is None
            or snapshot_row["task_id"] != task_id
            or snapshot_row["stage"] != "mix"
            or snapshot_row["input_digest"] != input_digest
            or Path(snapshot_row["output_path"]).resolve() != Path(output_path)
            or snapshot_row["producer"] not in {"assemble-v1", "assemble-ad-v1"}
        ):
            raise RuntimeError("UNTRUSTED_CANDIDATE_SNAPSHOT: 候选必须绑定可信 mix 快照")

        version_no = connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM candidate_versions WHERE task_id=?",
            (task_id,),
        ).fetchone()[0]
        rework = connection.execute(
            """SELECT rework_id,source_candidate_id,category,notes
            FROM rework_requests
            WHERE task_id=? AND status='applied' AND result_candidate_id IS NULL
            ORDER BY updated_at DESC, rowid DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        source_candidate_id = None if rework is None else rework["source_candidate_id"]
        rework_request_id = None if rework is None else rework["rework_id"]
        effective_summary = change_summary
        if effective_summary is None and rework is not None:
            effective_summary = f"{rework['category']}: {rework['notes']}"
        candidate_id = f"candidate-{uuid.uuid4().hex[:16]}"
        connection.execute(
            """INSERT INTO candidate_versions(
              candidate_id,task_id,version_no,source_candidate_id,rework_request_id,
              mix_snapshot_id,input_digest,output_path,output_signature_json,status,
              change_summary
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id,
                task_id,
                version_no,
                source_candidate_id,
                rework_request_id,
                mix_snapshot_id,
                input_digest,
                output_path,
                json.dumps(output_signature, sort_keys=True),
                "rendered",
                effective_summary,
            ),
        )
        if version_no > 1:
            connection.execute(
                """UPDATE publish_records SET package_status='expired'
                WHERE task_id=? AND package_status='current'""",
                (task_id,),
            )
        if rework is not None:
            claimed = connection.execute(
                """UPDATE rework_requests
                SET result_candidate_id=?, updated_at=datetime('now')
                WHERE rework_id=? AND status='applied' AND result_candidate_id IS NULL""",
                (candidate_id, rework_request_id),
            )
            if claimed.rowcount != 1:
                raise RuntimeError(
                    "REWORK_LINEAGE_CLAIM_FAILED: 返工单未能绑定新候选版本"
                )
        row = connection.execute(
            "SELECT * FROM candidate_versions WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return _public_candidate(connection, row)


def attach_candidate_qa(
    db,
    candidate_id: str,
    qa_result: dict,
    qa_snapshot_id: str | None = None,
    *,
    eligible_for_review: bool | None = None,
) -> dict:
    """Attach one authoritative QA report to an existing rendered candidate."""
    report_id = str(_required(qa_result, "report_id"))
    with (nullcontext(db.connection) if db.connection.in_transaction else db.transaction(immediate=True)) as connection:
        candidate = connection.execute(
            "SELECT * FROM candidate_versions WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if candidate is None:
            raise KeyError(f"CANDIDATE_NOT_FOUND: {candidate_id}")
        report = connection.execute(
            "SELECT * FROM qa_reports WHERE report_id=? AND task_id=?",
            (report_id, candidate["task_id"]),
        ).fetchone()
        if (
            report is None
            or Path(report["video_path"]).resolve()
            != Path(candidate["output_path"]).resolve()
            or report["status"] != qa_result.get("status")
            or int(report["blocking_count"]) != int(qa_result.get("blocking_count", -1))
        ):
            raise RuntimeError("CANDIDATE_QA_MISMATCH: QA 报告不属于该候选成片")

        report_passed = report["status"] == "passed"
        reviewable = report_passed if eligible_for_review is None else eligible_for_review
        if reviewable and not report_passed:
            raise RuntimeError(
                "CANDIDATE_QA_ELIGIBILITY_MISMATCH: 阻断 QA 不能进入人工审片"
            )

        if reviewable:
            if not qa_snapshot_id:
                raise RuntimeError("CANDIDATE_QA_SNAPSHOT_REQUIRED: 通过 QA 必须绑定快照")
            qa_snapshot = connection.execute(
                """SELECT * FROM render_snapshots
                WHERE snapshot_id=? AND task_id=? AND stage='qa'
                  AND parent_snapshot=?""",
                (qa_snapshot_id, candidate["task_id"], candidate["mix_snapshot_id"]),
            ).fetchone()
            if qa_snapshot is None:
                raise RuntimeError("CANDIDATE_QA_SNAPSHOT_MISMATCH: QA 快照不属于该候选")
            status = "awaiting_review"
        else:
            if qa_snapshot_id is not None:
                raise RuntimeError("CANDIDATE_BLOCKED_QA_SNAPSHOT: 阻断 QA 不创建审片快照")
            status = "qa_blocked"

        connection.execute(
            """UPDATE candidate_versions
            SET qa_snapshot_id=?, qa_report_id=?, status=?, updated_at=datetime('now')
            WHERE candidate_id=?""",
            (qa_snapshot_id, report_id, status, candidate_id),
        )
        updated = connection.execute(
            "SELECT * FROM candidate_versions WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return _public_candidate(connection, updated)


def record_candidate_qa(
    db,
    task_id: str,
    render_snapshot: dict,
    qa_result: dict,
    qa_snapshot_id: str | None = None,
    *,
    eligible_for_review: bool | None = None,
) -> dict:
    """Idempotently create a candidate and bind its authoritative QA result."""
    with (nullcontext(db.connection) if db.connection.in_transaction else db.transaction(immediate=True)) as connection:
        qa_rework_id = render_snapshot.get("qa_rework_id")
        if qa_rework_id:
            from .snapshots import latest_qa_only_rework
            request = latest_qa_only_rework(db, task_id)
            if (request is None or request["rework_id"] != qa_rework_id
                    or request["result_candidate_id"] is not None
                    or request["mix_snapshot_id"] != render_snapshot["mix_snapshot_id"]):
                raise RuntimeError("QA_RETRY_STALE: 质检返工已变化，旧检查结果不能覆盖当前版本")
        candidate = ensure_candidate_version(db, task_id, render_snapshot)
        result = attach_candidate_qa(db, candidate["candidate_id"], qa_result,
            qa_snapshot_id=qa_snapshot_id, eligible_for_review=eligible_for_review)
        if qa_rework_id and result["status"] == "awaiting_review":
            connection.execute("UPDATE rework_requests SET result_candidate_id=? WHERE rework_id=?",
                               (candidate["candidate_id"], qa_rework_id))
        return result


def recover_legacy_candidate_versions(db) -> int:
    """Idempotently recover verified pre-v035 render/QA pairs as candidates."""
    tables = {
        row[0]
        for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {
        "candidate_versions", "rework_requests", "render_snapshots",
        "qa_reports", "publish_records",
    }
    if not required.issubset(tables):
        return 0

    from .snapshots import file_content_signature

    mixes = db.connection.execute(
        """SELECT s.* FROM render_snapshots AS s
        LEFT JOIN candidate_versions AS c ON c.mix_snapshot_id=s.snapshot_id
        WHERE c.candidate_id IS NULL AND s.stage='mix'
          AND s.producer IN ('assemble-v1','assemble-ad-v1')
          AND s.input_digest IS NOT NULL
        ORDER BY s.task_id,s.rowid"""
    ).fetchall()
    recovered = 0
    for mix in mixes:
        try:
            params = json.loads(mix["params_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        signature = params.get("output_signature")
        output_path = Path(mix["output_path"])
        if (
            params.get("dependency_digest") != mix["input_digest"]
            or not isinstance(signature, dict)
            or not signature.get("sha256")
        ):
            continue
        try:
            if file_content_signature(output_path) != signature:
                continue
        except OSError:
            continue
        qa = db.connection.execute(
            """SELECT * FROM render_snapshots
            WHERE task_id=? AND stage='qa' AND parent_snapshot=? AND producer=?
              AND input_digest=? ORDER BY rowid DESC LIMIT 1""",
            (
                mix["task_id"], mix["snapshot_id"], mix["producer"],
                mix["input_digest"],
            ),
        ).fetchone()
        if qa is None or Path(qa["output_path"]).resolve() != output_path.resolve():
            continue
        try:
            qa_params = json.loads(qa["params_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        report = db.connection.execute(
            """SELECT report_id,status,blocking_count,video_path
            FROM qa_reports WHERE task_id=? AND report_id=?""",
            (mix["task_id"], qa_params.get("qa_report_id")),
        ).fetchone()
        if (
            report is None
            or report["status"] != "passed"
            or int(report["blocking_count"]) != 0
            or Path(report["video_path"]).resolve() != output_path.resolve()
            or qa_params.get("dependency_digest") != mix["input_digest"]
            or qa_params.get("output_signature") != signature
        ):
            continue
        candidate = ensure_candidate_version(
            db,
            mix["task_id"],
            {
                "mix_snapshot_id": mix["snapshot_id"],
                "dependency_digest": mix["input_digest"],
                "output_path": mix["output_path"],
                "output_signature": signature,
            },
            change_summary="从 v035 前可信渲染/QA 记录恢复",
        )
        attach_candidate_qa(
            db,
            candidate["candidate_id"],
            {
                "report_id": report["report_id"],
                "status": report["status"],
                "blocking_count": report["blocking_count"],
            },
            qa_snapshot_id=qa["snapshot_id"],
        )
        recovered += 1

    single_candidate_tasks = db.connection.execute(
        """SELECT task_id,MIN(candidate_id) AS candidate_id
        FROM candidate_versions GROUP BY task_id HAVING COUNT(*)=1"""
    ).fetchall()
    with db.transaction(immediate=True) as connection:
        for item in single_candidate_tasks:
            connection.execute(
                """UPDATE publish_records SET candidate_id=?
                WHERE task_id=? AND candidate_id IS NULL""",
                (item["candidate_id"], item["task_id"]),
            )
    return recovered


def resolve_candidate_media(db, task_id: str, candidate_id: str) -> Path:
    """Resolve exact candidate bytes after task ownership and signature checks."""
    from .snapshots import file_content_signature

    candidate = get_candidate(db, task_id, candidate_id)
    if candidate is None:
        raise KeyError(f"CANDIDATE_NOT_FOUND: {candidate_id}")
    stored = Path(candidate["output_path"])
    lexical = Path(os.path.abspath(stored))
    path = stored.resolve()
    if ".." in stored.parts or path != lexical:
        raise RuntimeError(
            "CANDIDATE_MEDIA_BOUNDARY_CHANGED: 候选成片路径已离开登记时边界"
        )
    try:
        actual = file_content_signature(path)
    except OSError as error:
        raise RuntimeError("CANDIDATE_MEDIA_MISSING: 候选成片不存在或无法读取") from error
    if actual != candidate["output_signature"]:
        raise RuntimeError("CANDIDATE_MEDIA_CHANGED: 候选成片内容已变化")
    return path
