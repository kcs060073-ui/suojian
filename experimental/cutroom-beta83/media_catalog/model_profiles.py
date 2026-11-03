"""Named model profiles, provider accounts, and immutable task bindings."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Mapping

from .credentials import CAPABILITY_SPECS
from .visual_capabilities import visual_check_statuses


PROFILE_CHECK_MAX_AGE = timedelta(hours=24)
CANONICAL_MODEL_ROLES = ("planning", "broll", "tts")
ROLE_COMPATIBILITY = {
    "planning": (
        "planning",
        "copywriting",
        "storyboard",
        "packaging",
        "general_text",
    ),
    "broll": ("broll", "video_generation"),
    "tts": ("tts",),
}
USAGE_ROLE = {
    "copywriting": "planning",
    "storyboard": "planning",
    "packaging": "planning",
    "general_text": "planning",
    "video_generation": "broll",
}


def compatible_profile_capabilities(capability: str) -> tuple[str, ...]:
    """Return source profile capabilities accepted for one binding role."""
    return ROLE_COMPATIBILITY.get(capability, (capability,))


class ModelProfileUnavailable(ValueError):
    """Raised when a task attempts to bind a model profile that is not usable."""


class ProviderAccountInUse(ValueError):
    """Raised when model profiles still reference a provider account."""


class ModelProfileInUse(ValueError):
    """Raised when a frozen task binding still references a model profile."""


def _json_object(value, *, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}必须是对象")
    return dict(value)


def _required(value, *, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"{label}不能为空")
    return text


def _dump(value: Mapping) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None) -> dict:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def create_provider_account(
    db,
    *,
    account_id: str,
    name: str,
    provider: str,
    base_url: str,
    credential_ref: str,
    settings: Mapping | None = None,
) -> dict:
    account_id = _required(account_id, label="账号 ID")
    values = (
        account_id,
        _required(name, label="账号名称"),
        _required(provider, label="供应商"),
        _required(base_url, label="接口地址").rstrip("/"),
        _required(credential_ref, label="凭据引用"),
        _dump(_json_object(settings, label="账号设置")),
    )
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO model_provider_accounts(
              account_id,name,provider,base_url,credential_ref,settings_json
            ) VALUES(?,?,?,?,?,?)
            """,
            values,
        )
    return get_provider_account(db, account_id)


def get_provider_account(db, account_id: str, *, include_credential: bool = False) -> dict | None:
    row = db.connection.execute(
        """
        SELECT account_id,name,provider,base_url,credential_ref,settings_json,
               created_at,updated_at
        FROM model_provider_accounts WHERE account_id=?
        """,
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    settings = _load(row["settings_json"])
    result = {
        "accountId": row["account_id"],
        "name": row["name"],
        "provider": row["provider"],
        "baseUrl": row["base_url"],
        "settings": settings,
        "preset": settings.get("preset") or row["provider"],
        "note": settings.get("note"),
        "defaultModel": settings.get("default_model"),
        "channelId": settings.get("channel_id"),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_credential:
        result["credentialRef"] = row["credential_ref"]
    return result


def list_provider_accounts(db) -> list[dict]:
    rows = db.connection.execute(
        "SELECT account_id FROM model_provider_accounts ORDER BY name,account_id"
    ).fetchall()
    return [get_provider_account(db, row["account_id"]) for row in rows]


def update_provider_account_settings(
    db,
    account_id: str,
    settings: Mapping,
) -> dict:
    account_id = _required(account_id, label="账号 ID")
    if get_provider_account(db, account_id) is None:
        raise ValueError("供应商账号不存在")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE model_provider_accounts "
            "SET settings_json=?,updated_at=datetime('now') "
            "WHERE account_id=?",
            (
                _dump(_json_object(settings, label="账号设置")),
                account_id,
            ),
        )
    return get_provider_account(db, account_id)


def delete_provider_account(db, account_id: str, *, credential_deleter) -> dict | None:
    account_id = _required(account_id, label="账号 ID")
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT credential_ref FROM model_provider_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        profile_count = connection.execute(
            "SELECT COUNT(*) FROM model_profiles WHERE account_id=?",
            (account_id,),
        ).fetchone()[0]
        if profile_count:
            raise ProviderAccountInUse(
                f"该供应商仍被 {profile_count} 个模型配置使用，请先处理关联配置。"
            )
        credential_deleter(row["credential_ref"])
        connection.execute(
            "DELETE FROM model_provider_accounts WHERE account_id=?",
            (account_id,),
        )
    return {"accountId": account_id, "deleted": True}


def delete_model_profile(db, profile_id: str) -> dict | None:
    profile_id = _required(profile_id, label="配置 ID")
    with db.transaction(immediate=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        if exists is None:
            return None
        binding_count = connection.execute(
            "SELECT COUNT(*) FROM task_model_bindings WHERE profile_id=?",
            (profile_id,),
        ).fetchone()[0]
        if binding_count:
            raise ModelProfileInUse(
                f"该模型配置仍被 {binding_count} 个任务使用，不能删除。"
            )
        connection.execute(
            "DELETE FROM model_profiles WHERE profile_id=?",
            (profile_id,),
        )
    return {"profileId": profile_id, "deleted": True}


def _checked_profile_settings(capability, settings):
    value = _json_object(settings, label="模型设置")
    if capability == "visual_analysis":
        from .visual_protocol import visual_protocol_mode
        visual_protocol_mode(value)
        from .visual_contact_sheet import transport_mode
        transport_mode(value)
    return value


def create_model_profile(
    db,
    *,
    profile_id: str,
    capability: str,
    display_name: str,
    account_id: str,
    model: str,
    settings: Mapping | None = None,
    is_default: bool = False,
) -> dict:
    if capability not in CAPABILITY_SPECS:
        raise ValueError("未知能力")
    profile_id = _required(profile_id, label="配置 ID")
    account_id = _required(account_id, label="供应商账号")
    if get_provider_account(db, account_id) is None:
        raise ValueError("供应商账号不存在")
    checked_settings = _checked_profile_settings(capability, settings)
    with db.transaction(immediate=True) as connection:
        if is_default:
            connection.execute(
                "UPDATE model_profiles SET is_default=0,updated_at=datetime('now') "
                "WHERE capability=?",
                (capability,),
            )
        connection.execute(
            """
            INSERT INTO model_profiles(
              profile_id,capability,display_name,account_id,model,settings_json,is_default
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                profile_id,
                capability,
                _required(display_name, label="配置名称"),
                account_id,
                _required(model, label="模型"),
                _dump(checked_settings),
                int(bool(is_default)),
            ),
        )
    return get_model_profile(db, profile_id)


def _profile_row(db, profile_id: str):
    return db.connection.execute(
        """
        SELECT p.*,a.name AS account_name,a.provider,a.base_url,a.credential_ref,
               a.settings_json AS account_settings_json
        FROM model_profiles p
        JOIN model_provider_accounts a ON a.account_id=p.account_id
        WHERE p.profile_id=?
        """,
        (profile_id,),
    ).fetchone()


def _public_profile(row) -> dict:
    account_settings = _load(row["account_settings_json"])
    return {
        "profileId": row["profile_id"],
        "capability": row["capability"],
        "displayName": row["display_name"],
        "accountId": row["account_id"],
        "accountName": row["account_name"],
        "provider": row["provider"],
        "baseUrl": row["base_url"],
        "model": row["model"],
        "settings": _load(row["settings_json"]),
        "channelId": account_settings.get("channel_id"),
        "isDefault": bool(row["is_default"]),
        "enabled": bool(row["enabled"]),
        "availabilityStatus": row["availability_status"],
        "lastCheckedAt": row["last_checked_at"],
        "lastErrorCode": row["last_error_code"],
        "lastErrorMessage": row["last_error_message"],
    }


def get_model_profile(db, profile_id: str) -> dict | None:
    row = _profile_row(db, profile_id)
    if row is None:
        return None
    result = _public_profile(row)
    if row["capability"] == "visual_analysis":
        result["visualChecks"] = visual_check_statuses(db, row)
        if result["availabilityStatus"] == "available" and any(
            value != "available" for value in result["visualChecks"].values()
        ):
            result["availabilityStatus"] = "untested"
    return result


def list_model_profiles(db, *, capability: str | None = None) -> list[dict]:
    parameters = ()
    where = ""
    if capability is not None:
        if capability not in CAPABILITY_SPECS:
            raise ValueError("未知能力")
        where = "WHERE p.capability=?"
        parameters = (capability,)
    rows = db.connection.execute(
        f"""
        SELECT p.profile_id
        FROM model_profiles p
        {where}
        ORDER BY p.capability,p.created_at,p.profile_id
        """,
        parameters,
    ).fetchall()
    return [get_model_profile(db, row["profile_id"]) for row in rows]


def set_default_profile(db, profile_id: str) -> dict:
    row = _profile_row(db, profile_id)
    if row is None:
        raise ValueError("模型配置不存在")
    with db.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE model_profiles SET is_default=0,updated_at=datetime('now') "
            "WHERE capability=?",
            (row["capability"],),
        )
        connection.execute(
            "UPDATE model_profiles SET is_default=1,updated_at=datetime('now') "
            "WHERE profile_id=?",
            (profile_id,),
        )
    return get_model_profile(db, profile_id)


def update_model_profile_settings(
    db,
    profile_id: str,
    settings: Mapping,
) -> dict:
    profile_id = _required(profile_id, label="配置 ID")
    row = _profile_row(db, profile_id)
    if row is None:
        raise ValueError("模型配置不存在")
    checked_settings = _checked_profile_settings(row["capability"], settings)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE model_profiles "
            "SET settings_json=?,updated_at=datetime('now') "
            "WHERE profile_id=?",
            (
                _dump(checked_settings),
                profile_id,
            ),
        )
    return get_model_profile(db, profile_id)


def mark_profile_check(
    db,
    profile_id: str,
    *,
    available: bool,
    error_code: str | None = None,
    error_message: str | None = None,
    checked_at: datetime | None = None,
) -> dict:
    if _profile_row(db, profile_id) is None:
        raise ValueError("模型配置不存在")
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE model_profiles
            SET availability_status=?,last_checked_at=?,last_error_code=?,
                last_error_message=?,updated_at=datetime('now')
            WHERE profile_id=?
            """,
            (
                "available" if available else "unavailable",
                _iso(checked_at),
                None if available else error_code,
                None if available else error_message,
                profile_id,
            ),
        )
    return get_model_profile(db, profile_id)


def _binding_snapshot(row, *, bound_capability: str | None = None) -> dict:
    account_settings = _load(row["account_settings_json"])
    profile_settings = _load(row["settings_json"])
    config = {
        "profileId": row["profile_id"],
        "capability": bound_capability or row["capability"],
        "displayName": row["display_name"],
        "accountId": row["account_id"],
        "provider": row["provider"],
        "baseUrl": row["base_url"],
        "credentialRef": row["credential_ref"],
        "model": row["model"],
        "channelId": account_settings.get("channel_id"),
        "accountSettings": account_settings,
        "profileSettings": profile_settings,
        "checkedAt": row["last_checked_at"],
    }
    public_config = {key: value for key, value in config.items() if key != "checkedAt"}
    config["configDigest"] = hashlib.sha256(_dump(public_config).encode("utf-8")).hexdigest()
    return config


def override_snapshot_profile_settings(
    snapshot: Mapping,
    updates: Mapping,
) -> dict:
    """Return a detached task snapshot with a fresh integrity digest."""
    if not isinstance(snapshot, Mapping) or not isinstance(updates, Mapping):
        raise ValueError("模型快照与覆盖设置必须是对象")
    cloned = json.loads(json.dumps(dict(snapshot), ensure_ascii=False))
    settings = cloned.get("profileSettings")
    settings = dict(settings) if isinstance(settings, Mapping) else {}
    settings.update(dict(updates))
    cloned["profileSettings"] = settings
    digest_payload = {
        key: value
        for key, value in cloned.items()
        if key not in {"checkedAt", "configDigest"}
    }
    cloned["configDigest"] = hashlib.sha256(
        _dump(digest_payload).encode("utf-8")
    ).hexdigest()
    return cloned


def resolve_task_tts_voice(db, snapshot: Mapping, voice_id: str) -> dict:
    voice_id = _required(voice_id, label="本条视频音色")
    settings = snapshot.get("profileSettings")
    settings = settings if isinstance(settings, Mapping) else {}
    resource_id = str(
        settings.get("resource_id") or snapshot.get("model") or ""
    ).strip()
    if snapshot.get("provider") == "doubao_tts":
        from .tts_voice_catalog import get_provider_voice

        account_id = str(snapshot.get("accountId") or "").strip()
        voice = get_provider_voice(db, account_id, resource_id, voice_id)
        if voice is None or not voice["selectable"]:
            raise ValueError(
                "VOICE_NOT_SELECTABLE: 请选择当前配音服务目录中的可用音色"
            )
        return {
            "voiceId": voice["voiceId"],
            "displayName": voice["displayName"],
            "resourceId": voice["resourceId"],
        }
    configured = str(
        settings.get("voice") or settings.get("default_voice_id") or ""
    ).strip()
    if voice_id != configured:
        raise ValueError(
            "VOICE_NOT_SELECTABLE: 高级兼容配音只能使用配置中已验证的音色"
        )
    return {
        "voiceId": configured,
        "displayName": str(settings.get("voice_name") or configured),
        "resourceId": resource_id,
    }


def bind_task_profiles(
    db,
    task_id: str,
    selections: Mapping[str, str] | None,
    *,
    required_capabilities: tuple[str, ...],
    now: datetime | None = None,
    max_age: timedelta = PROFILE_CHECK_MAX_AGE,
) -> dict[str, dict]:
    snapshots = resolve_task_profile_snapshots(
        db,
        selections,
        required_capabilities=required_capabilities,
        now=now,
        max_age=max_age,
    )
    save_task_profile_bindings(db, task_id, snapshots, bound_at=now)
    return snapshots


def resolve_task_profile_snapshots(
    db,
    selections: Mapping[str, str] | None,
    *,
    required_capabilities: tuple[str, ...],
    now: datetime | None = None,
    max_age: timedelta = PROFILE_CHECK_MAX_AGE,
) -> dict[str, dict]:
    selections = dict(selections or {})
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    snapshots: dict[str, dict] = {}
    for capability in required_capabilities:
        if capability not in CAPABILITY_SPECS:
            raise ValueError("未知能力")
        compatible_capabilities = compatible_profile_capabilities(capability)
        profile_id = selections.get(capability)
        if not profile_id:
            for compatible_capability in compatible_capabilities:
                default = db.connection.execute(
                    """
                    SELECT profile_id FROM model_profiles
                    WHERE capability=? AND is_default=1 AND enabled=1
                    """,
                    (compatible_capability,),
                ).fetchone()
                if default is not None:
                    profile_id = default["profile_id"]
                    break
        if not profile_id:
            raise ModelProfileUnavailable(f"{capability}没有可用的默认配置")
        row = _profile_row(db, profile_id)
        if (
            row is None
            or row["capability"] not in compatible_capabilities
            or not row["enabled"]
        ):
            raise ModelProfileUnavailable(f"{capability}选择的模型配置无效")
        if row["availability_status"] == "untested" or not row["last_checked_at"]:
            raise ModelProfileUnavailable(f"{row['display_name']}尚未通过可用性检测")
        if row["availability_status"] != "available":
            raise ModelProfileUnavailable(f"{row['display_name']}当前不可用")
        checked = _parse_iso(row["last_checked_at"])
        if checked is None or current.astimezone(timezone.utc) - checked > max_age:
            raise ModelProfileUnavailable(f"{row['display_name']}检测结果已经过期")
        snapshots[capability] = _binding_snapshot(
            row,
            bound_capability=capability,
        )

    return snapshots


def save_task_profile_bindings(
    db,
    task_id: str,
    snapshots: Mapping[str, Mapping],
    *,
    bound_at: datetime | None = None,
) -> None:
    with db.transaction(immediate=True) as connection:
        for capability, snapshot in snapshots.items():
            connection.execute(
                """
                INSERT INTO task_model_bindings(
                  task_id,capability,profile_id,snapshot_json,snapshot_digest,bound_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(task_id,capability) DO UPDATE SET
                  profile_id=excluded.profile_id,
                  snapshot_json=excluded.snapshot_json,
                  snapshot_digest=excluded.snapshot_digest,
                  bound_at=excluded.bound_at
                """,
                (
                    task_id,
                    capability,
                    snapshot["profileId"],
                    _dump(snapshot),
                    snapshot["configDigest"],
                    _iso(bound_at),
                ),
            )


def get_task_profile_binding(db, task_id: str, capability: str) -> dict | None:
    row = db.connection.execute(
        "SELECT snapshot_json,snapshot_digest FROM task_model_bindings "
        "WHERE task_id=? AND capability=?",
        (task_id, capability),
    ).fetchone()
    if row is None:
        return None
    snapshot = _load(row["snapshot_json"])
    stored_digest = row["snapshot_digest"]
    digest_payload = {
        key: value
        for key, value in snapshot.items()
        if key not in {"checkedAt", "configDigest"}
    }
    actual_digest = hashlib.sha256(
        _dump(digest_payload).encode("utf-8")
    ).hexdigest()
    if (
        snapshot.get("configDigest") != stored_digest
        or actual_digest != stored_digest
    ):
        raise ModelProfileUnavailable("任务模型快照完整性校验失败")
    return snapshot


def get_task_profile_binding_for_usage(
    db,
    task_id: str,
    usage: str,
) -> dict | None:
    """Prefer an old exact binding, then fall back to its canonical role."""
    exact = get_task_profile_binding(db, task_id, usage)
    if exact is not None:
        return exact
    role = USAGE_ROLE.get(usage)
    if role is None:
        return None
    return get_task_profile_binding(db, task_id, role)


def list_task_profile_bindings(db, task_id: str) -> list[dict]:
    rows = db.connection.execute(
        "SELECT capability,bound_at FROM task_model_bindings "
        "WHERE task_id=? ORDER BY capability",
        (task_id,),
    ).fetchall()
    result = []
    for row in rows:
        snapshot = get_task_profile_binding(db, task_id, row["capability"])
        settings = snapshot.get("profileSettings")
        settings = settings if isinstance(settings, dict) else {}
        result.append({
            "capability": snapshot.get("capability"),
            "profileId": snapshot.get("profileId"),
            "displayName": snapshot.get("displayName"),
            "provider": snapshot.get("provider"),
            "model": snapshot.get("model"),
            "channelId": snapshot.get("channelId"),
            "voice": settings.get("voice"),
            "voiceName": settings.get("voice_name"),
            "configDigest": snapshot.get("configDigest"),
            "boundAt": row["bound_at"],
        })
    return result


class TaskVoiceSelectionError(ValueError):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def select_task_tts_voice(db, task_id, *, profile_id, voice_id, expected_binding_digest):
    """Atomically change unlocked ad voice selection and its frozen model binding."""
    from .tasks import get_task
    from .workflow import project_gates

    with db.transaction(immediate=True) as connection:
        current = get_task(db, task_id)
        if current is None:
            raise TaskVoiceSelectionError(404, "NOT_FOUND", "任务不存在。")
        if current["task_type"] != "ad_mix":
            raise TaskVoiceSelectionError(409, "VOICE_ROUTE_INVALID", "数字人沿用口播底片声音。")
        if project_gates(db, task_id).get("brief_locked"):
            raise TaskVoiceSelectionError(409, "BRIEF_ALREADY_LOCKED", "文案已确认；修改声音请先进入返工流程。")
        if connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND job_type!='initial_source_check' AND status IN ('queued','running','pending') LIMIT 1", (task_id,)).fetchone():
            raise TaskVoiceSelectionError(409, "TASK_BUSY", "任务正在处理，稍后再修改声音。")
        previous = get_task_profile_binding(db, task_id, "tts")
        if (previous["configDigest"] if previous else None) != expected_binding_digest:
            raise TaskVoiceSelectionError(409, "VOICE_SELECTION_CHANGED", "配音设置已变化，请刷新后核对。")
        try:
            snapshot = resolve_task_profile_snapshots(db, {"tts": profile_id}, required_capabilities=("tts",))["tts"]
            voice = resolve_task_tts_voice(db, snapshot, voice_id)
            snapshot = override_snapshot_profile_settings(snapshot, {
                "voice": voice["voiceId"], "voice_name": voice["displayName"], "resource_id": voice["resourceId"],
            })
        except ValueError as error:
            raise TaskVoiceSelectionError(400, "VOICE_NOT_SELECTABLE", str(error)) from error
        brief = dict(current["brief"])
        brief.update(tts_voice_id=voice["voiceId"], tts_voice_name=voice["displayName"])
        connection.execute("UPDATE tasks SET brief_json=?, updated_at=datetime('now') WHERE task_id=?",
                           (json.dumps(brief, ensure_ascii=False), task_id))
        connection.execute("""INSERT INTO task_model_bindings
            (task_id,capability,profile_id,snapshot_json,snapshot_digest,bound_at)
            VALUES(?,'tts',?,?,?,datetime('now'))
            ON CONFLICT(task_id,capability) DO UPDATE SET
            profile_id=excluded.profile_id,snapshot_json=excluded.snapshot_json,
            snapshot_digest=excluded.snapshot_digest,bound_at=excluded.bound_at""",
            (task_id, profile_id, json.dumps(snapshot, ensure_ascii=False), snapshot["configDigest"]))
    return {**voice, "bindingDigest": snapshot["configDigest"], "profileId": profile_id}
