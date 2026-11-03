"""Separate image, frame-sequence and structured-output checks for vision."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from .visual_protocol import PROTOCOL_VERSION, visual_protocol_mode, protocol_contract
from .visual_schema import visual_schema_digest
from .visual_request_policy import visual_request_contract


VISUAL_CHECK_KINDS = ("image", "frame_sequence", "structured_output")


def visual_profile_digest(row) -> str:
    payload = {
        "visual_protocol_version": PROTOCOL_VERSION,
        "visual_protocol_contract": protocol_contract(visual_protocol_mode(json.loads(row["settings_json"] or "{}"))),
        "visual_schema_digest": visual_schema_digest(),
        "provider": row["provider"],
        "base_url": row["base_url"],
        "account_settings": json.loads(row["account_settings_json"] or "{}"),
        "model": row["model"],
        "profile_settings": json.loads(row["settings_json"] or "{}"),
    }
    policy = visual_request_contract(row)
    if policy:
        payload["visual_request_policy"] = policy
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def visual_check_statuses(db, row) -> dict[str, str]:
    try:
        digest = visual_profile_digest(row)
    except ValueError:
        # Legacy malformed settings remain visible so they can be corrected.
        # They never confer a valid capability or change stored check evidence.
        return {kind: "stale" for kind in VISUAL_CHECK_KINDS}
    checks = {kind: "untested" for kind in VISUAL_CHECK_KINDS}
    for entry in db.connection.execute(
        "SELECT check_kind,status,config_digest FROM visual_profile_checks WHERE profile_id=?",
        (row["profile_id"],),
    ):
        checks[entry["check_kind"]] = (
            entry["status"] if entry["config_digest"] == digest else "stale"
        )
    return checks


def reset_visual_checks(db, profile_id: str) -> None:
    with db.transaction() as connection:
        connection.execute("DELETE FROM visual_profile_checks WHERE profile_id=?", (profile_id,))


def record_visual_check(db, row, kind: str, *, available: bool,
                        error_code: str | None = None,
                        error_message: str | None = None) -> None:
    if kind not in VISUAL_CHECK_KINDS:
        raise ValueError("VISUAL_CHECK_INVALID: 未知视觉能力")
    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO visual_profile_checks(
                 profile_id,check_kind,status,config_digest,checked_at,error_code,error_message
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(profile_id,check_kind) DO UPDATE SET
                 status=excluded.status,config_digest=excluded.config_digest,
                 checked_at=excluded.checked_at,error_code=excluded.error_code,
                 error_message=excluded.error_message""",
            (row["profile_id"], kind, "available" if available else "unavailable",
             visual_profile_digest(row), datetime.now(timezone.utc).isoformat(),
             error_code, error_message),
        )


def require_verified_visual_profile(db, profile_id: str) -> dict:
    from .model_profiles import (
        PROFILE_CHECK_MAX_AGE, _parse_iso, get_model_profile,
    )

    profile = get_model_profile(db, profile_id)
    if profile is None or profile["capability"] != "visual_analysis":
        raise ValueError("VISUAL_PROFILE_INVALID: 视觉模型配置不存在")
    checked_at = _parse_iso(profile["lastCheckedAt"])
    if not profile["enabled"] or profile["availabilityStatus"] != "available" or checked_at is None or \
            datetime.now(timezone.utc) - checked_at > PROFILE_CHECK_MAX_AGE or \
            any(profile["visualChecks"][kind] != "available" for kind in VISUAL_CHECK_KINDS):
        raise ValueError("VISUAL_PROFILE_UNVERIFIED: 图片、帧序列和结构输出均需近期验证")
    profile["visualProtocol"] = protocol_contract(visual_protocol_mode(profile["settings"]))
    return profile
