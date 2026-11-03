"""Attach checked defaults to unbound drafts when their paid job is submitted.

This runs inside the queue transaction: either both bindings and job persist,
or neither does. Existing task snapshots remain authoritative.
"""
from datetime import datetime, timezone
import json

from .model_profiles import (
    ModelProfileUnavailable, get_task_profile_binding_for_usage,
    resolve_task_profile_snapshots,
)
from .voice_ownership import read_voice_ownership


def bind_missing_storyboard_profiles(db, task):
    if not db.connection.in_transaction:
        raise RuntimeError("Storyboard binding requires the queue transaction")
    task_id = task["task_id"]
    planning = (get_task_profile_binding_for_usage(db, task_id, "storyboard")
                or get_task_profile_binding_for_usage(db, task_id, "general_text"))
    needs_tts = (task["task_type"] == "ad_mix"
                 and read_voice_ownership(db, task_id).branch == "legacy")
    tts = get_task_profile_binding_for_usage(db, task_id, "tts") if needs_tts else None
    missing = ([] if planning else ["planning"]) + (["tts"] if needs_tts and not tts else [])
    if not missing:
        return
    # Keep the pre-profile legacy configuration route compatible. Once modern
    # profiles or any task binding exist, never fall back to unrelated settings.
    if not planning and not tts and not db.connection.execute(
        "SELECT 1 FROM model_profiles LIMIT 1"
    ).fetchone():
        return
    snapshots = resolve_task_profile_snapshots(db, {}, required_capabilities=tuple(missing))
    new_tts = snapshots.get("tts")
    if new_tts and not new_tts.get("profileSettings", {}).get("voice"):
        raise ModelProfileUnavailable("配音配置尚未选择音色")
    for capability, snapshot in snapshots.items():
        # Insert only: this path must never replace an existing frozen choice.
        db.connection.execute(
            "INSERT INTO task_model_bindings "
            "(task_id,capability,profile_id,snapshot_json,snapshot_digest,bound_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, capability, snapshot["profileId"],
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             snapshot["configDigest"], datetime.now(timezone.utc).isoformat()),
        )
