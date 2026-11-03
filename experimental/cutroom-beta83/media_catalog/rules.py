"""P3-M1 规则版本化。

设计固定边界：新任务默认最新规则，原任务锁定创建时的规则和资产版本。
本模块提供「规则集 → 版本 → payload(JSON)」的读写，并把版本锁定到任务上。

payload 目前承载 qa 阈值；后续可扩展 packaging 路由覆盖、匹配权重等。
所有阈值变更都通过 bump 生成新版本，旧任务不受新版本影响。
"""

import json

DEFAULT_RULE_SET = "default"

# P2 验收基线：与 qa.py 历史硬编码值一致，作为 v1 种子
DEFAULT_PAYLOAD: dict = {
    "qa": {
        "black": {"min_dur": 0.3, "pix_th": 0.10, "block_above": 1.0},
        "freeze": {"noise": 0.003, "min_dur": 2, "warn_above": 3.0},
        "silence": {"noise": -50, "dur": 1.0, "block_above": 3.0, "warn_total_above": 8.0},
        "clipping": {"warn_above": -0.1},
        "loudness": {
            "target_lufs": -16.0,
            "tolerance_lu": 4.0,
            "max_true_peak_dbfs": -1.0,
        },
    }
}


def ensure_seed(db, rule_set_id: str = DEFAULT_RULE_SET) -> None:
    """保证至少有一个版本（v1 基线）。直接写 v1，避免与 add_version 互递归。"""
    row = db.connection.execute(
        "SELECT MAX(version) v FROM rule_versions WHERE rule_set_id=?", (rule_set_id,)
    ).fetchone()
    if (row["v"] or 0) == 0:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO rule_versions(rule_set_id, version, payload, notes) "
                "VALUES(?, 1, ?, ?)",
                (rule_set_id, json.dumps(DEFAULT_PAYLOAD, ensure_ascii=False),
                 "P2 验收基线阈值"),
            )


def add_version(
    db, payload: dict, notes: str = "", rule_set_id: str = DEFAULT_RULE_SET
) -> int:
    """写入新版本（version = 当前最大+1），返回新版本号。"""
    ensure_seed(db, rule_set_id)
    row = db.connection.execute(
        "SELECT COALESCE(MAX(version),0)+1 v FROM rule_versions WHERE rule_set_id=?",
        (rule_set_id,),
    ).fetchone()
    v = row["v"]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO rule_versions(rule_set_id, version, payload, notes) "
            "VALUES(?, ?, ?, ?)",
            (rule_set_id, v, json.dumps(payload, ensure_ascii=False), notes),
        )
    return v


def current_version(db, rule_set_id: str = DEFAULT_RULE_SET) -> int:
    ensure_seed(db, rule_set_id)
    row = db.connection.execute(
        "SELECT MAX(version) v FROM rule_versions WHERE rule_set_id=?", (rule_set_id,)
    ).fetchone()
    return row["v"]


def get_payload(
    db, version: int, rule_set_id: str = DEFAULT_RULE_SET
) -> dict:
    ensure_seed(db, rule_set_id)
    row = db.connection.execute(
        "SELECT payload FROM rule_versions WHERE rule_set_id=? AND version=?",
        (rule_set_id, version),
    ).fetchone()
    return json.loads(row["payload"]) if row else DEFAULT_PAYLOAD


def rules_for_task(db, task_id: str) -> tuple[dict, int]:
    """返回 (payload, version)：任务锁定的版本；无锁定则取当前最新。"""
    row = db.connection.execute(
        "SELECT rule_version FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    v = row["rule_version"] if row and row["rule_version"] else current_version(db)
    return get_payload(db, v), v


def list_versions(db, rule_set_id: str = DEFAULT_RULE_SET) -> list[dict]:
    ensure_seed(db, rule_set_id)
    return [
        dict(r)
        for r in db.connection.execute(
            "SELECT version, notes, applied_at FROM rule_versions "
            "WHERE rule_set_id=? ORDER BY version",
            (rule_set_id,),
        ).fetchall()
    ]


def show_version(db, version: int, rule_set_id: str = DEFAULT_RULE_SET) -> dict | None:
    row = db.connection.execute(
        "SELECT version, notes, applied_at, payload FROM rule_versions "
        "WHERE rule_set_id=? AND version=?",
        (rule_set_id, version),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d
