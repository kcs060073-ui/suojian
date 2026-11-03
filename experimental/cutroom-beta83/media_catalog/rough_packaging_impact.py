"""Read-only packaging reminders for a proposed scene-card change."""

from __future__ import annotations


def affected_packaging(connection, task_id: str, before: dict, after: dict) -> list[dict]:
    old = {card["card_id"]: card for card in before["cards"]}
    new = {card["card_id"]: card for card in after["cards"]}
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    if not changed:
        return []
    windows = [(float(card["narrative_start"]), float(card["narrative_end"]))
               for cards in (old, new) for key, card in cards.items() if key in changed]
    # A task may retain several draft/confirmed plans. Name each explicitly;
    # this is a review reminder, never a claim that a historical plan is active.
    instances = [dict(row) for row in connection.execute(
        "SELECT i.instance_id,i.plan_id,p.plan_name,i.role,i.content_text,"
        "i.anchor_kind,i.anchor_ref,i.start_seconds,i.end_seconds "
        "FROM packaging_plan_instances i JOIN packaging_plans p USING(plan_id) "
        "WHERE p.task_id=? AND ("
        "NOT EXISTS (SELECT 1 FROM packaging_plan_items x WHERE x.plan_id=i.plan_id "
        "AND x.slot=i.role AND x.is_backup=0) OR "
        "EXISTS (SELECT 1 FROM packaging_plan_items x WHERE x.plan_id=i.plan_id "
        "AND x.slot=i.role AND x.is_backup=0 AND x.enabled=1)) "
        "ORDER BY p.created_at,p.rowid,i.start_seconds,i.instance_id", (task_id,)
    )]
    selected = {}
    for item in instances:
        start, end = item["start_seconds"], item["end_seconds"]
        reason = None
        if item["anchor_kind"] == "scene" and item["anchor_ref"] in changed:
            reason = "scene_anchor"
        elif start is not None and end is not None:
            if any(float(start) < stop and float(end) > begin for begin, stop in windows):
                reason = "overlapping_time"
        elif item["anchor_kind"] != "instance":
            reason = "timing_unknown"
        if reason:
            selected[item["instance_id"]] = {**item, "reason": reason}
    # Include events that follow a changed element, even for old records whose
    # resolved times are absent. Finite iteration also tolerates legacy cycles.
    for _ in instances:
        additions = [item for item in instances
                     if item["instance_id"] not in selected
                     and item["anchor_kind"] == "instance"
                     and item["anchor_ref"] in selected
                     and selected[item["anchor_ref"]]["plan_id"] == item["plan_id"]]
        if not additions:
            break
        for item in additions:
            selected[item["instance_id"]] = {**item, "reason": "follows_element"}
    return [selected[item["instance_id"]] for item in instances
            if item["instance_id"] in selected]
