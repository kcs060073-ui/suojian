"""Task-local music comparison drafts, preserving the rest of the packaging."""
from . import packaging_plan as plans
from .packaging_music_review import music_feedback_context, rejected_music_hashes
from .packaging_recommendations import available_assets, digest


def candidates(db, task_id, plan_id):
    plan = plans.get_plan(db, plan_id)
    if not plan or plan["task_id"] != task_id:
        raise PermissionError("MUSIC_PLAN_NOT_OWNED: 方案不属于当前任务")
    lock = plans._require_picture_lock(db, db.connection, task_id)
    feedback = music_feedback_context(db, task_id)
    rejected = rejected_music_hashes(feedback)
    # The shared catalog filters route, license and actual file availability.
    items = [a for a in available_assets(plan["route"], db) if a["slot"] == "bgm"]
    options = [{**a, "rejected": a.get("source_sha256") in rejected,
                "basis": "曲库描述，未经本片听审；不代表适配评分"} for a in items]
    return {"contextDigest": digest({"plan": plan, "lock": lock, "feedback": feedback, "assets": items}),
            "options": options, "feedback": feedback,
            "limitations": "候选未排序；请结合本片混音比较。音量沿用当前设置，不代表另一首音乐也同样合适。"}


def create_comparison(db, task_id, base_plan_id, asset_ref, context_digest, request_id):
    if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 120
            or not isinstance(context_digest, str) or len(context_digest) != 64
            or (asset_ref is not None and not isinstance(asset_ref, str))):
        raise ValueError("MUSIC_COMPARISON_INVALID: 对比请求无效")
    with db.transaction(immediate=True) as conn:
        old = conn.execute("SELECT * FROM packaging_music_comparisons WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if (old["task_id"], old["base_plan_id"], old["asset_ref"], old["context_digest"]) != (task_id, base_plan_id, asset_ref, context_digest):
                raise RuntimeError("MUSIC_COMPARISON_CONFLICT: 此对比请求已用于其他选择")
            return {"planId": old["plan_id"], "requestId": request_id}
        current = candidates(db, task_id, base_plan_id)
        if current["contextDigest"] != context_digest:
            raise RuntimeError("MUSIC_COMPARISON_STALE: 包装、曲库或试听反馈已变化，请刷新候选")
        item = next((a for a in current["options"] if a["ref"] == asset_ref), None)
        if asset_ref is not None and (item is None or item["rejected"]):
            raise ValueError("MUSIC_CANDIDATE_UNAVAILABLE: 曲目不可用或已被本片否定")
        name = item.get("name", asset_ref) if item else "不新增音乐"
        clone = plans.clone_plan(db, base_plan_id, task_id=task_id, plan_name="音乐对比 · "+name, connection=conn)
        if item:
            plans.replace_item(db, clone, "bgm", asset_ref, connection=conn)
        else:
            plans.disable_optional_slot(db, clone, "bgm", connection=conn)
        conn.execute("INSERT INTO packaging_music_comparisons(request_id,task_id,base_plan_id,context_digest,asset_ref,plan_id) VALUES(?,?,?,?,?,?)",
                     (request_id, task_id, base_plan_id, context_digest, asset_ref, clone))
        return {"planId": clone, "requestId": request_id}
