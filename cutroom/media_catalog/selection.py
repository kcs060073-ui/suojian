"""P1-C 候选管理：给素材打"要 / 不要 / 待定"并留理由与操作人。

这是人工门禁的落点（设计文档四个人工门禁中的"画面与顺序锁定"前一步）。
决策只写 selection_decisions 表，不碰原片、不自动删除，换人也能看到前因后果。
"""

from collections.abc import Callable

VALID = ("candidate", "rejected", "confirmed")


def set_decision(
    db,
    asset_id: str,
    decision: str,
    reason: str | None = None,
    decided_by: str | None = None,
) -> dict:
    if decision not in VALID:
        raise ValueError(f"决策必须是 {VALID} 之一")
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO selection_decisions(asset_id, decision, reason, decided_by)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
              decision=excluded.decision,
              reason=excluded.reason,
              decided_by=excluded.decided_by,
              decided_at=datetime('now')
            """,
            (asset_id, decision, reason, decided_by),
        )
    return get_decision(db, asset_id)


def get_decision(db, asset_id: str) -> dict | None:
    row = db.connection.execute(
        """
        SELECT a.asset_id, a.media_kind, al.relative_path, sd.decision,
               sd.reason, sd.decided_by, sd.decided_at
        FROM selection_decisions sd
        JOIN assets a USING(asset_id)
        JOIN asset_locations al USING(asset_id)
        WHERE sd.asset_id=?
        """,
        (asset_id,),
    ).fetchone()
    return None if row is None else dict(row)


def list_decisions(db, decision: str | None = None, limit: int = 200) -> list[dict]:
    if decision is not None and decision not in VALID:
        raise ValueError(f"决策过滤必须是 {VALID} 之一")
    sql = """
        SELECT a.asset_id, al.relative_path, sd.decision, sd.reason, sd.decided_at
        FROM selection_decisions sd
        JOIN assets a USING(asset_id)
        JOIN asset_locations al USING(asset_id)
    """
    params: list = []
    if decision is not None:
        sql += " WHERE sd.decision=? "
        params.append(decision)
    sql += " ORDER BY sd.decided_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in db.connection.execute(sql, params).fetchall()]
