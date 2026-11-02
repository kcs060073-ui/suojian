"""P1-C 任务目录：把"一条要做的片子"登记为任务，并把挑选好的素材挂到任务下。

对应设计文档里的"素材检查表"——本来是 Excel 给人勾，这里变成可查询的
task_assets 关系表，既能当检查表用，也能让程序直接读到"这条片子用了哪些素材"。
"""

import json
import hashlib
import uuid
from collections.abc import Callable

from .rules import current_version
from .provenance import canonical_digest, record_gate
from .task_sources import (
    scoped_asset_location,
    task_has_source_configuration,
)

VALID_STATUS = ("draft", "brief_locked", "selection_locked", "packaging_confirmed", "reviewed")
VALID_ORCHESTRATION_MODES = ("standalone", "agent")


def _is_locked_digital_human_source(db, task_id: str, asset_id: str) -> bool:
    row = db.connection.execute(
        "SELECT task_type, brief_json FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None or row["task_type"] != "digital_human":
        return False
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except json.JSONDecodeError:
        return False
    source = brief.get("digital_human_source")
    return isinstance(source, dict) and source.get("asset_id") == asset_id


def create_task(
    db,
    task_id: str,
    title: str,
    task_type: str | None = None,
    brief: dict | None = None,
    orchestration_mode: str = "standalone",
) -> dict:
    if orchestration_mode not in VALID_ORCHESTRATION_MODES:
        raise ValueError(
            "ORCHESTRATION_MODE_INVALID: 编排模式必须是 standalone 或 agent"
        )
    existing = db.connection.execute(
        "SELECT orchestration_mode FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if existing is not None and existing["orchestration_mode"] != orchestration_mode:
        raise ValueError(
            "ORCHESTRATION_MODE_IMMUTABLE: 已创建任务不能切换编排模式"
        )
    rule_version = current_version(db)  # 锁定创建时的最新规则版本
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO tasks(
              task_id, title, task_type, status, brief_json, rule_version,
              orchestration_mode
            )
            VALUES(?, ?, ?, 'draft', ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              title=excluded.title,
              task_type=excluded.task_type,
              brief_json=excluded.brief_json,
              updated_at=datetime('now')
            """,
            (
                task_id,
                title,
                task_type,
                json.dumps(brief, ensure_ascii=False) if brief else None,
                rule_version,
                orchestration_mode,
            ),
        )
    return get_task(db, task_id)


def get_task(db, task_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    if data.get("brief_json"):
        try:
            data["brief"] = json.loads(data["brief_json"])
        except json.JSONDecodeError:
            data["brief"] = None
    return data


def list_tasks(db, limit: int = 100) -> list[dict]:
    return [
        dict(r)
        for r in db.connection.execute(
            "SELECT task_id, title, task_type, status, orchestration_mode, updated_at FROM tasks "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    ]


def set_status(db, task_id: str, status: str) -> dict:
    if status not in VALID_STATUS:
        raise ValueError(f"任务状态必须是 {VALID_STATUS} 之一")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE task_id=?",
            (status, task_id),
        )
        if status == "brief_locked":
            row = connection.execute(
                "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"任务不存在：{task_id}")
            brief = json.loads(row["brief_json"] or "{}")
            record_gate(connection, task_id, "brief", canonical_digest(brief), brief)
    return get_task(db, task_id)


def add_asset(db, task_id: str, asset_id: str, role: str | None = None) -> None:
    if task_has_source_configuration(db, task_id):
        location = scoped_asset_location(db, task_id, asset_id)
        if location is None and not (
            role == "digital_human"
            and _is_locked_digital_human_source(db, task_id, asset_id)
        ):
            raise ValueError(
                f"素材 {asset_id} 不在任务已绑定的素材范围内，禁止从未绑定素材库混入"
            )
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_assets(task_id, asset_id, role)
            VALUES(?, ?, ?)
            ON CONFLICT(task_id, asset_id) DO UPDATE SET role=excluded.role
            """,
            (task_id, asset_id, role),
        )
        connection.execute(
            "UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?",
            (task_id,),
        )


def remove_asset(db, task_id: str, asset_id: str) -> None:
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM task_assets WHERE task_id=? AND asset_id=?",
            (task_id, asset_id),
        )
        connection.execute(
            "UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?",
            (task_id,),
        )


def list_task_assets(db, task_id: str) -> list[dict]:
    if task_has_source_configuration(db, task_id):
        result = []
        rows = db.connection.execute(
            """
            SELECT ta.asset_id, ta.role, sd.decision
            FROM task_assets ta
            LEFT JOIN selection_decisions sd USING(asset_id)
            WHERE ta.task_id=?
            ORDER BY ta.created_at, ta.asset_id
            """,
            (task_id,),
        ).fetchall()
        for row in rows:
            location = scoped_asset_location(db, task_id, row["asset_id"])
            if location is None:
                continue
            result.append(
                {
                    "asset_id": row["asset_id"],
                    "role": row["role"],
                    "relative_path": location["relative_path"],
                    "decision": row["decision"],
                }
            )
        return result
    return [
        dict(r)
        for r in db.connection.execute(
            """
            SELECT ta.asset_id, ta.role, al.relative_path, sd.decision
            FROM task_assets ta
            JOIN asset_locations al USING(asset_id)
            LEFT JOIN selection_decisions sd USING(asset_id)
            WHERE ta.task_id=?
            ORDER BY ta.created_at
            """,
            (task_id,),
        ).fetchall()
    ]
