"""Immutable, credential-free planning artifacts submitted by local Agents."""

from __future__ import annotations

import hashlib
import json
import uuid


VALID_ARTIFACT_TYPES = frozenset({"digital_storyboard", "ad_storyboard"})


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _public_artifact(row) -> dict:
    result = dict(row)
    result["content"] = json.loads(result.pop("content_json"))
    return result


def save_agent_artifact(
    db,
    *,
    task_id: str,
    artifact_type: str,
    schema_version: int,
    content: dict,
    input_digest: str,
    source_agent: str,
    source_model_label: str | None,
) -> tuple[dict, bool]:
    if artifact_type not in VALID_ARTIFACT_TYPES:
        raise ValueError(
            "AGENT_ARTIFACT_TYPE_INVALID: Agent 产物类型不受支持"
        )
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        raise ValueError(
            "AGENT_ARTIFACT_SCHEMA_INVALID: Agent 产物版本必须是正整数"
        )
    if not isinstance(content, dict):
        raise ValueError("AGENT_ARTIFACT_CONTENT_INVALID: Agent 产物必须是对象")
    if not isinstance(input_digest, str) or not input_digest.strip():
        raise ValueError("AGENT_INPUT_DIGEST_REQUIRED: Agent 产物缺少输入摘要")
    if not isinstance(source_agent, str) or not source_agent.strip():
        raise ValueError("AGENT_SOURCE_REQUIRED: Agent 产物缺少来源")

    task = db.connection.execute(
        "SELECT orchestration_mode FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    if task["orchestration_mode"] != "agent":
        raise ValueError("AGENT_MODE_REQUIRED: 只有 Agent 模式任务可以接收 Agent 产物")

    content_json = _canonical_json(content)
    content_digest = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
    artifact_id = f"artifact_{uuid.uuid4().hex}"
    with db.transaction(immediate=True) as connection:
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO agent_artifacts(
              artifact_id, task_id, artifact_type, schema_version,
              content_json, content_digest, input_digest,
              source_agent, source_model_label
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                task_id,
                artifact_type,
                schema_version,
                content_json,
                content_digest,
                input_digest.strip(),
                source_agent.strip(),
                source_model_label.strip()
                if isinstance(source_model_label, str) and source_model_label.strip()
                else None,
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM agent_artifacts
            WHERE task_id=? AND artifact_type=? AND input_digest=? AND content_digest=?
            """,
            (task_id, artifact_type, input_digest.strip(), content_digest),
        ).fetchone()
    return _public_artifact(row), inserted.rowcount == 1


def get_agent_artifact(db, artifact_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM agent_artifacts WHERE artifact_id=?",
        (artifact_id,),
    ).fetchone()
    return None if row is None else _public_artifact(row)


def list_agent_artifacts(
    db,
    task_id: str,
    artifact_type: str | None = None,
) -> list[dict]:
    if artifact_type is None:
        rows = db.connection.execute(
            """
            SELECT * FROM agent_artifacts
            WHERE task_id=?
            ORDER BY created_at, rowid
            """,
            (task_id,),
        ).fetchall()
    else:
        if artifact_type not in VALID_ARTIFACT_TYPES:
            raise ValueError(
                "AGENT_ARTIFACT_TYPE_INVALID: Agent 产物类型不受支持"
            )
        rows = db.connection.execute(
            """
            SELECT * FROM agent_artifacts
            WHERE task_id=? AND artifact_type=?
            ORDER BY created_at, rowid
            """,
            (task_id, artifact_type),
        ).fetchall()
    return [_public_artifact(row) for row in rows]
