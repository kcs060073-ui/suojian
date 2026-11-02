"""P1-D 包装资产库：把团队共用的"包装件"登记成可浏览清单。

包装件 = 水印、标题卡模板、BGM、字幕样式、转场等。它们本身是已入册的素材，
这里只给一个"这是包装件、属于哪一类"的标签，方便挑片时快速找到、也方便
程序在渲染阶段直接读取（取代到处硬编码路径）。
"""

import json
from collections.abc import Callable

VALID_KINDS = (
    "watermark",
    "title_card",
    "bgm",
    "subtitle_style",
    "transition",
    "font",
    "sticker",
    "other",
)


def register_packaging(
    db,
    asset_id: str,
    kind: str,
    name: str | None = None,
    notes: str | None = None,
) -> dict:
    if kind not in VALID_KINDS:
        raise ValueError(f"包装件类型必须是 {VALID_KINDS} 之一")
    row = db.connection.execute(
        "SELECT asset_id FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"素材未入册，不能登记为包装件：{asset_id}")
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO packaging_assets(asset_id, kind, name, notes)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
              kind=excluded.kind, name=excluded.name, notes=excluded.notes
            """,
            (asset_id, kind, name, notes),
        )
    return get_packaging(db, asset_id)


def get_packaging(db, asset_id: str) -> dict | None:
    row = db.connection.execute(
        """
        SELECT p.asset_id, p.kind, p.name, p.notes, al.relative_path
        FROM packaging_assets p
        JOIN asset_locations al USING(asset_id)
        WHERE p.asset_id=?
        """,
        (asset_id,),
    ).fetchone()
    return None if row is None else dict(row)


def list_packaging(db, kind: str | None = None, limit: int = 200) -> list[dict]:
    if kind is not None and kind not in VALID_KINDS:
        raise ValueError(f"包装件类型过滤必须是 {VALID_KINDS} 之一")
    sql = """
        SELECT p.asset_id, p.kind, p.name, al.relative_path
        FROM packaging_assets p
        JOIN asset_locations al USING(asset_id)
    """
    params: list = []
    if kind is not None:
        sql += " WHERE p.kind=? "
        params.append(kind)
    sql += " ORDER BY p.kind, p.name LIMIT ?"
    params.append(limit)
    return [dict(r) for r in db.connection.execute(sql, params).fetchall()]


def unregister(db, asset_id: str) -> None:
    with db.transaction() as connection:
        connection.execute("DELETE FROM packaging_assets WHERE asset_id=?", (asset_id,))
