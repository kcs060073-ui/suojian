"""P3-M4 视频脚本写作适配器。

设计：先接「园区无文案入口」——用户有素材但没文案时，由适配器产出草稿；
保持人工文案回退（草稿必须经人工 confirm 才生效，可随时 reject 改手写）。

生成策略（可插拔）：
- 默认 template：从任务素材文件名/元数据抽取地点与卖点，拼装「开场—要点—CTA」草稿。
- llm：预留 hook（generator 可注入真实 LLM/写作 Skill），本模块不绑定具体模型。

纯 Python，不依赖 ffmpeg / 外部服务，可独立验证。
"""

import json
import re
import uuid

from .match import candidate_text_for_asset

ROUTES = ("数字人混剪", "园区宣传", "信息流广告")


def _extract_features(text: str) -> list[str]:
    """从素材文本里抽取可能的地点/卖点词（中文二元组里出现频次高的实词近似）。"""
    # 简单策略：取文件名中的「地点_特征」切分，去掉通用前缀
    chunks = re.split(r"[_\-－/]", text)
    feats = [c.strip(" .。，、") for c in chunks if len(c.strip()) >= 2]
    return [f for f in feats if f not in {"seed", "public", "library", "mp4", "mov", "png"}][:6]


def template_draft(route: str, assets_text: list[str]) -> dict:
    """基于素材文本拼装园区/通用草稿。返回 {outline, script}。"""
    features = []
    for t in assets_text:
        features += _extract_features(t)
    features = list(dict.fromkeys(features))  # 去重保序
    if not features:
        features = ["园区核心优势"]

    if route == "园区宣传":
        opening = "走进这片园区，产业要素正在加速聚集。"
        points = [f"看{f}，是落地企业的硬支撑。" for f in features[:3]]
        cta = "选址找我们，少走弯路，快速落位。"
    elif route == "数字人混剪":
        opening = "今天用一组实拍，讲清这条赛道的底层逻辑。"
        points = [f"关键看{f}。" for f in features[:3]]
        cta = "关注我们，持续拆解产业机会。"
    else:  # 信息流广告
        opening = "一条视频，说清为什么选这里。"
        points = [f"{f}，正是你要的答案。" for f in features[:3]]
        cta = "立即咨询，获取专属选址方案。"

    script = "。".join([opening, *points, cta]) + "。"
    outline = {
        "opening": opening,
        "points": points,
        "cta": cta,
        "features": features,
    }
    return {"outline": outline, "script": script}


def draft_script(db, task_id: str, route: str, generator=None,
                 assets: list[str] | None = None) -> dict:
    """为任务生成脚本草稿（落库 draft）。generator 可注入 LLM；默认 template。"""
    if route not in ROUTES:
        raise ValueError(f"route 必须是 {ROUTES} 之一")
    if assets is None:
        rows = db.connection.execute(
            "SELECT asset_id FROM task_assets WHERE task_id=?", (task_id,)
        ).fetchall()
        assets = [r["asset_id"] for r in rows]
    texts = [candidate_text_for_asset(db, a, task_id) for a in assets]

    if callable(generator):
        out = generator(route, texts)
        source = "llm"
    else:
        out = template_draft(route, texts)
        source = "template"

    draft = {
        "draft_id": f"sd-{uuid.uuid4().hex[:12]}",
        "task_id": task_id,
        "route": route,
        "source": source,
        "status": "draft",
        "outline": json.dumps(out["outline"], ensure_ascii=False),
        "script": out["script"],
    }
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO script_drafts(draft_id, task_id, route, source, status, "
            "outline, script) VALUES(?,?,?,?,?,?,?)",
            (draft["draft_id"], task_id, route, source, "draft",
             draft["outline"], draft["script"]),
        )
    return draft


def confirm_draft(db, draft_id: str) -> dict:
    """人工确认（文案回退门禁）：草稿生效。"""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE script_drafts SET status='confirmed', "
            "confirmed_at=datetime('now') WHERE draft_id=?",
            (draft_id,),
        )
    return get_draft(db, draft_id)


def reject_draft(db, draft_id: str) -> dict:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE script_drafts SET status='rejected' WHERE draft_id=?",
            (draft_id,),
        )
    return get_draft(db, draft_id)


def get_draft(db, draft_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM script_drafts WHERE draft_id=?", (draft_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["outline"] = json.loads(d["outline"]) if d["outline"] else None
    except json.JSONDecodeError:
        pass
    return d


def list_drafts(db, task_id: str | None = None) -> list[dict]:
    sql = "SELECT draft_id, task_id, route, source, status, created_at FROM script_drafts"
    args: tuple = ()
    if task_id:
        sql += " WHERE task_id=?"
        args = (task_id,)
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in db.connection.execute(sql, args).fetchall()]
