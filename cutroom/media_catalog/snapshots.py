"""P2-M7 渲染快照 + 局部重跑。

每次渲染（mix / ad / qa）都记录一份快照：阶段、参数、产物路径、父快照。
目的：
- 可追溯：成片对应哪次参数组合（与 packaging_plan / scene_cards 对照）。
- 局部重跑：改了某个参数后，只对受影响的阶段重跑，不必从头来（对照 parent_snapshot）。
"""

import hashlib
import json
from pathlib import Path
import uuid


def snapshot(
    db,
    task_id: str,
    stage: str,
    params: dict | None = None,
    output_path: str | None = None,
    parent_snapshot: str | None = None,
    producer: str | None = None,
    input_digest: str | None = None,
) -> str:
    if stage not in ("mix", "ad", "qa"):
        raise ValueError("stage 必须是 mix|ad|qa 之一")
    snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO render_snapshots
              (snapshot_id, task_id, stage, params_json, output_path, parent_snapshot,
               producer, input_digest)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id, task_id, stage,
                json.dumps(params or {}, ensure_ascii=False),
                output_path, parent_snapshot, producer, input_digest,
            ),
        )
    return snapshot_id


def list_snapshots(db, task_id: str, stage: str | None = None) -> list[dict]:
    sql = "SELECT * FROM render_snapshots WHERE task_id=?"
    params = [task_id]
    if stage is not None:
        sql += " AND stage=?"
        params.append(stage)
    sql += " ORDER BY rowid DESC LIMIT 100"
    return [dict(r) for r in db.connection.execute(sql, params).fetchall()]


def file_content_signature(path: str | Path) -> dict:
    """Return a stable content identity without trusting path or timestamp alone."""
    resolved = Path(path).resolve()
    digest = hashlib.sha256()
    with resolved.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return {"size_bytes": resolved.stat().st_size, "sha256": digest.hexdigest()}


def record_render_dependency_snapshot(
    db, task_id: str, job: dict, *, producer: str = "assemble-v1"
) -> dict:
    """Record dependencies and rendered bytes before QA begins."""
    output_path = str(Path(job["output_path"]).resolve())
    output_signature = file_content_signature(output_path)
    mix_params = {
        "dependency_digest": job["dependency_digest"],
        "dependency_snapshot": job["dependency_snapshot"],
        "plan_path": job["plan_path"],
        "ass_path": job.get("ass_path"),
        "packaging": job["packaging"],
        "output_signature": output_signature,
    }
    mix_snapshot_id = snapshot(
        db,
        task_id,
        "mix",
        mix_params,
        output_path,
        producer=producer,
        input_digest=job["dependency_digest"],
    )
    return {
        "mix_snapshot_id": mix_snapshot_id,
        "dependency_digest": job["dependency_digest"],
        "output_signature": output_signature,
        "output_path": output_path,
        "producer": producer,
    }


def record_qa_review_snapshot(
    db,
    task_id: str,
    render_snapshot: dict,
    qa_result: dict,
    *,
    producer: str | None = None,
) -> dict:
    """Parent QA to the exact rendered bytes and dependency snapshot it checked."""
    output_path = render_snapshot["output_path"]
    actual_producer = producer or render_snapshot.get("producer") or "assemble-v1"
    qa_output_signature = file_content_signature(output_path)
    if qa_output_signature != render_snapshot["output_signature"]:
        raise RuntimeError("RENDER_OUTPUT_CHANGED_DURING_QA: QA 期间成片内容发生变化")
    qa_snapshot_id = snapshot(
        db,
        task_id,
        "qa",
        {
            "qa_report_id": qa_result["report_id"],
            "dependency_digest": render_snapshot["dependency_digest"],
            "output_signature": qa_output_signature,
        },
        output_path,
        render_snapshot["mix_snapshot_id"],
        producer=actual_producer,
        input_digest=render_snapshot["dependency_digest"],
    )
    return {
        **render_snapshot,
        "qa_snapshot_id": qa_snapshot_id,
        "qa_report_id": qa_result["report_id"],
    }


def record_render_review_candidate(db, task_id: str, job: dict, qa_result: dict) -> dict:
    """Backward-compatible combined helper; new orchestration snapshots before QA."""
    rendered = record_render_dependency_snapshot(db, task_id, job)
    snapshots = record_qa_review_snapshot(db, task_id, rendered, qa_result)
    from .candidate_versions import record_candidate_qa

    candidate = record_candidate_qa(
        db,
        task_id,
        rendered,
        qa_result,
        qa_snapshot_id=snapshots["qa_snapshot_id"],
    )
    return {**snapshots, **candidate}


def latest_retryable_render_snapshot(db, task_id: str) -> dict | None:
    """Return the newest trusted mix whose QA did not create a review candidate."""
    row = db.connection.execute(
        """
        SELECT * FROM render_snapshots
        WHERE task_id=? AND stage='mix' AND producer IN ('assemble-v1','assemble-ad-v1')
          AND input_digest IS NOT NULL
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    qa_child = db.connection.execute(
        """
        SELECT 1 FROM render_snapshots
        WHERE task_id=? AND stage='qa' AND parent_snapshot=?
          AND producer=?
        LIMIT 1
        """,
        (task_id, row["snapshot_id"], row["producer"]),
    ).fetchone()
    if qa_child is not None:
        return None
    try:
        params = json.loads(row["params_json"] or "{}")
    except json.JSONDecodeError:
        return None
    if params.get("dependency_digest") != row["input_digest"]:
        return None
    required = ("dependency_snapshot", "output_signature", "packaging")
    if any(params.get(key) is None for key in required):
        return None
    return {
        "mix_snapshot_id": row["snapshot_id"],
        "dependency_digest": row["input_digest"],
        "dependency_snapshot": params["dependency_snapshot"],
        "output_signature": params["output_signature"],
        "output_path": row["output_path"],
        "packaging": params["packaging"],
        "producer": row["producer"],
    }

def latest_review_candidate(db, task_id: str) -> dict | None:
    """Return the current version only when its exact QA binding is reviewable."""
    version = db.connection.execute(
        """SELECT * FROM candidate_versions
        WHERE task_id=? ORDER BY version_no DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    if (
        version is None
        or version["status"] not in {"awaiting_review", "approved", "published"}
        or not version["qa_snapshot_id"]
        or not version["qa_report_id"]
    ):
        return None
    qa_row = db.connection.execute(
        """
        SELECT rowid, * FROM render_snapshots
        WHERE snapshot_id=? AND task_id=? AND stage='qa' AND parent_snapshot IS NOT NULL
          AND producer IN ('assemble-v1','assemble-ad-v1') AND input_digest IS NOT NULL
        """,
        (version["qa_snapshot_id"], task_id),
    ).fetchone()
    if qa_row is None:
        return None
    mix_row = db.connection.execute(
        """
        SELECT rowid, * FROM render_snapshots
        WHERE snapshot_id=? AND task_id=? AND stage='mix'
          AND producer=? AND input_digest IS NOT NULL
        """,
        (qa_row["parent_snapshot"], task_id, qa_row["producer"]),
    ).fetchone()
    if mix_row is None:
        return None
    if qa_row["input_digest"] != mix_row["input_digest"]:
        return None
    try:
        qa_params = json.loads(qa_row["params_json"] or "{}")
        mix_params = json.loads(mix_row["params_json"] or "{}")
    except json.JSONDecodeError:
        return None
    qa_report = db.connection.execute(
        "SELECT report_id,video_path,status FROM qa_reports WHERE report_id=? AND task_id=?",
        (qa_params.get("qa_report_id"), task_id),
    ).fetchone()
    if (
        qa_report is None or qa_report["status"] != "passed"
        or Path(qa_report["video_path"]).resolve() != Path(qa_row["output_path"]).resolve()
        or Path(mix_row["output_path"]).resolve() != Path(qa_row["output_path"]).resolve()
        or mix_params.get("dependency_digest") != mix_row["input_digest"]
        or qa_params.get("dependency_digest") != qa_row["input_digest"]
    ):
        return None
    result = {
        "qa_snapshot_id": qa_row["snapshot_id"],
        "mix_snapshot_id": mix_row["snapshot_id"],
        "qa_report_id": qa_params.get("qa_report_id"),
        "dependency_digest": mix_params.get("dependency_digest"),
        "dependency_snapshot": mix_params.get("dependency_snapshot"),
        "packaging": mix_params.get("packaging"),
        "output_signature": mix_params.get("output_signature"),
        "qa_output_signature": qa_params.get("output_signature"),
        "output_path": mix_row["output_path"],
        "qa_output_path": qa_row["output_path"],
        "producer": qa_row["producer"],
        "input_digest": qa_row["input_digest"],
    }
    if (
        version["mix_snapshot_id"] != mix_row["snapshot_id"]
        or version["qa_report_id"] != qa_params.get("qa_report_id")
    ):
        return None
    result.update({
        "candidate_id": version["candidate_id"],
        "version_no": version["version_no"],
        "version_label": f"candidate-v{version['version_no']}",
        "status": version["status"],
        "source_candidate_id": version["source_candidate_id"],
        "rework_request_id": version["rework_request_id"],
        "change_summary": version["change_summary"],
    })
    return result
