"""Cached TTS voice catalogs, refresh state, and legacy profile import."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping

from .credentials import decode_provider_credentials, read_provider_secret
from .model_profiles import get_provider_account
from .volcengine_voice_catalog import (
    VoiceCatalogError,
    fetch_volcengine_voices,
)


def _iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required(value, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"{label}不能为空")
    return text


def _optional(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json(value, default) -> str:
    actual = default if value is None else value
    return json.dumps(
        actual,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load(value: str | None, default):
    try:
        decoded = json.loads(value or "")
    except json.JSONDecodeError:
        return default
    return decoded if isinstance(decoded, type(default)) else default


def _normalized_voice(voice: Mapping, resource_id: str) -> dict:
    if not isinstance(voice, Mapping):
        raise ValueError("音色目录项必须是对象")
    normalized_resource = _required(
        voice.get("resourceId") or resource_id,
        "音色服务版本",
    )
    if normalized_resource != resource_id:
        raise ValueError("音色服务版本与刷新目标不一致")
    metadata = voice.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ValueError("音色元数据必须是对象")
    result = {
        "voice_id": _required(voice.get("voiceId"), "音色 ID"),
        "display_name": _required(voice.get("displayName"), "音色名称"),
        "resource_id": normalized_resource,
        "gender": _optional(voice.get("gender")),
        "languages": voice.get("languages") or [],
        "styles": voice.get("styles") or [],
        "emotions": voice.get("emotions") or [],
        "preview_url": _optional(voice.get("previewUrl")),
        "short_preview_url": _optional(voice.get("shortPreviewUrl")),
        "metadata": dict(metadata),
    }
    for key in ("languages", "styles", "emotions"):
        if not isinstance(result[key], list) or not all(
            isinstance(item, str) for item in result[key]
        ):
            raise ValueError(f"音色 {key} 必须是文字列表")
        result[key] = [item.strip() for item in result[key] if item.strip()]
    return result


def replace_provider_voices(
    db,
    account_id: str,
    resource_id: str,
    voices: list[Mapping],
    *,
    observed_at: datetime | None = None,
) -> list[dict]:
    account_id = _required(account_id, "供应商账号")
    resource_id = _required(resource_id, "配音服务版本")
    if not isinstance(voices, list):
        raise ValueError("音色目录必须是列表")
    normalized = [_normalized_voice(voice, resource_id) for voice in voices]
    if len({voice["voice_id"] for voice in normalized}) != len(normalized):
        raise ValueError("音色目录包含重复 ID")
    observed = _iso(observed_at)
    with db.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE tts_voice_catalog "
            "SET selectable=0,updated_at=? "
            "WHERE account_id=? AND resource_id=? "
            "AND source='provider_catalog'",
            (observed, account_id, resource_id),
        )
        for voice in normalized:
            connection.execute(
                """
                INSERT INTO tts_voice_catalog(
                  account_id,resource_id,voice_id,display_name,gender,
                  languages_json,styles_json,emotions_json,preview_url,
                  short_preview_url,source,selectable,metadata_json,last_seen_at,
                  updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?, 'provider_catalog',1,?,?,?)
                ON CONFLICT(account_id,resource_id,voice_id) DO UPDATE SET
                  display_name=excluded.display_name,
                  gender=excluded.gender,
                  languages_json=excluded.languages_json,
                  styles_json=excluded.styles_json,
                  emotions_json=excluded.emotions_json,
                  preview_url=excluded.preview_url,
                  short_preview_url=excluded.short_preview_url,
                  source='provider_catalog',
                  selectable=1,
                  metadata_json=excluded.metadata_json,
                  last_seen_at=excluded.last_seen_at,
                  updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    resource_id,
                    voice["voice_id"],
                    voice["display_name"],
                    voice["gender"],
                    _json(voice["languages"], []),
                    _json(voice["styles"], []),
                    _json(voice["emotions"], []),
                    voice["preview_url"],
                    voice["short_preview_url"],
                    _json(voice["metadata"], {}),
                    observed,
                    observed,
                ),
            )
        connection.execute(
            """
            INSERT INTO tts_voice_catalog_state(
              account_id,resource_id,status,last_attempt_at,last_success_at,
              last_error_code,last_error_message,updated_at
            ) VALUES(?,?,'success',?,?,NULL,NULL,?)
            ON CONFLICT(account_id,resource_id) DO UPDATE SET
              status='success',
              last_attempt_at=excluded.last_attempt_at,
              last_success_at=excluded.last_success_at,
              last_error_code=NULL,
              last_error_message=NULL,
              updated_at=excluded.updated_at
            """,
            (account_id, resource_id, observed, observed, observed),
        )
    return list_provider_voices(db, account_id, resource_id)


def mark_voice_catalog_failure(
    db,
    account_id: str,
    resource_id: str,
    code: str,
    message: str,
) -> dict:
    account_id = _required(account_id, "供应商账号")
    resource_id = _required(resource_id, "配音服务版本")
    code = _required(code, "错误码")
    message = _required(message, "错误信息")[:500]
    attempted = _iso()
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO tts_voice_catalog_state(
              account_id,resource_id,status,last_attempt_at,last_error_code,
              last_error_message,updated_at
            ) VALUES(?,?,'failed',?,?,?,?)
            ON CONFLICT(account_id,resource_id) DO UPDATE SET
              status='failed',
              last_attempt_at=excluded.last_attempt_at,
              last_error_code=excluded.last_error_code,
              last_error_message=excluded.last_error_message,
              updated_at=excluded.updated_at
            """,
            (
                account_id,
                resource_id,
                attempted,
                code,
                message,
                attempted,
            ),
        )
    return voice_catalog_state(db, account_id, resource_id)


def _public_voice(row) -> dict:
    return {
        "accountId": row["account_id"],
        "resourceId": row["resource_id"],
        "voiceId": row["voice_id"],
        "displayName": row["display_name"],
        "gender": row["gender"],
        "languages": _load(row["languages_json"], []),
        "styles": _load(row["styles_json"], []),
        "emotions": _load(row["emotions_json"], []),
        "previewUrl": row["preview_url"],
        "shortPreviewUrl": row["short_preview_url"],
        "source": row["source"],
        "selectable": bool(row["selectable"]),
        "metadata": _load(row["metadata_json"], {}),
        "lastSeenAt": row["last_seen_at"],
        "lastTestedAt": row["last_tested_at"],
        "testStatus": row["test_status"],
        "lastTestErrorCode": row["last_test_error_code"],
        "lastTestErrorMessage": row["last_test_error_message"],
    }


def list_provider_voices(
    db,
    account_id: str,
    resource_id: str,
    *,
    selectable_only: bool = False,
) -> list[dict]:
    where = " AND selectable=1" if selectable_only else ""
    rows = db.connection.execute(
        "SELECT * FROM tts_voice_catalog "
        "WHERE account_id=? AND resource_id=?"
        + where
        + " ORDER BY voice_id",
        (account_id, resource_id),
    ).fetchall()
    return [_public_voice(row) for row in rows]


def get_provider_voice(
    db,
    account_id: str,
    resource_id: str,
    voice_id: str,
) -> dict | None:
    row = db.connection.execute(
        "SELECT * FROM tts_voice_catalog "
        "WHERE account_id=? AND resource_id=? AND voice_id=?",
        (account_id, resource_id, voice_id),
    ).fetchone()
    return None if row is None else _public_voice(row)


def mark_voice_test_result(
    db,
    account_id: str,
    resource_id: str,
    voice_id: str,
    *,
    available: bool,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict:
    tested = _iso()
    disable_voice = not available and error_code == "TTS_VOICE_TEST_FAILED"
    with db.transaction() as connection:
        cursor = connection.execute(
            "UPDATE tts_voice_catalog SET test_status=?,"
            "selectable=CASE WHEN ? THEN 1 WHEN ? THEN 0 ELSE selectable END,"
            "last_tested_at=?,last_test_error_code=?,last_test_error_message=?,"
            "updated_at=? WHERE account_id=? AND resource_id=? AND voice_id=?",
            (
                "available" if available else "unavailable",
                int(available),
                int(disable_voice),
                tested,
                None if available else error_code,
                None if available else (error_message or "音色试听失败")[:500],
                tested,
                account_id,
                resource_id,
                voice_id,
            ),
        )
    if cursor.rowcount != 1:
        raise ValueError("VOICE_NOT_SELECTABLE: 音色不在当前配置目录中")
    return get_provider_voice(db, account_id, resource_id, voice_id)


def voice_catalog_state(db, account_id: str, resource_id: str) -> dict:
    row = db.connection.execute(
        "SELECT * FROM tts_voice_catalog_state "
        "WHERE account_id=? AND resource_id=?",
        (account_id, resource_id),
    ).fetchone()
    if row is None:
        return {
            "accountId": account_id,
            "resourceId": resource_id,
            "status": "never",
            "lastAttemptAt": None,
            "lastSuccessAt": None,
            "lastErrorCode": None,
            "lastErrorMessage": None,
        }
    return {
        "accountId": row["account_id"],
        "resourceId": row["resource_id"],
        "status": row["status"],
        "lastAttemptAt": row["last_attempt_at"],
        "lastSuccessAt": row["last_success_at"],
        "lastErrorCode": row["last_error_code"],
        "lastErrorMessage": row["last_error_message"],
    }


def refresh_provider_voice_catalog(
    db,
    account_id: str,
    *,
    secret_reader=read_provider_secret,
    fetcher=fetch_volcengine_voices,
) -> dict:
    account = get_provider_account(db, account_id, include_credential=True)
    if account is None:
        raise VoiceCatalogError(
            "VOICE_CATALOG_ACCOUNT_NOT_FOUND",
            "豆包配音账号不存在。",
        )
    if account.get("provider") != "doubao_tts":
        raise VoiceCatalogError(
            "VOICE_CATALOG_UNSUPPORTED_PROVIDER",
            "该供应商不支持自动读取豆包音色。",
        )
    settings = account.get("settings") or {}
    resource_id = _optional(settings.get("tts_resource_id"))
    if resource_id is None:
        raise VoiceCatalogError(
            "VOICE_CATALOG_RESOURCE_MISSING",
            "请先选择豆包配音服务版本。",
        )

    try:
        credentials = decode_provider_credentials(
            secret_reader(account["credentialRef"]) or ""
        )
        access_key_id = _optional(settings.get("access_key_id"))
        secret_access_key = credentials.get("secret_access_key")
        if not access_key_id or not secret_access_key:
            raise VoiceCatalogError(
                "VOICE_CATALOG_CREDENTIALS_MISSING",
                "请先补充 Access Key ID 与 Secret Access Key。",
            )
        voices = fetcher(access_key_id, secret_access_key, resource_id)
        if not isinstance(voices, list):
            raise VoiceCatalogError(
                "VOICE_CATALOG_INVALID_RESPONSE",
                "音色目录返回了无法识别的数据。",
            )
        if any(
            not isinstance(voice, Mapping)
            or voice.get("resourceId") != resource_id
            for voice in voices
        ):
            raise VoiceCatalogError(
                "VOICE_CATALOG_INVALID_RESPONSE",
                "音色目录返回了不匹配的服务版本。",
            )
        persisted = replace_provider_voices(
            db,
            account_id,
            resource_id,
            voices,
        )
    except VoiceCatalogError as error:
        mark_voice_catalog_failure(
            db,
            account_id,
            resource_id,
            error.code,
            error.safe_message,
        )
        raise
    except (TypeError, ValueError) as error:
        safe = VoiceCatalogError(
            "VOICE_CATALOG_INVALID_RESPONSE",
            "音色目录返回了无法识别的数据。",
        )
        mark_voice_catalog_failure(
            db,
            account_id,
            resource_id,
            safe.code,
            safe.safe_message,
        )
        raise safe from error
    return {
        "voices": persisted,
        "state": voice_catalog_state(db, account_id, resource_id),
    }


def _table_exists(db, name: str) -> bool:
    return db.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def import_legacy_tts_voices(db) -> int:
    required_tables = (
        "model_provider_accounts",
        "model_profiles",
        "tts_voice_catalog",
    )
    if not all(_table_exists(db, name) for name in required_tables):
        return 0
    rows = db.connection.execute(
        """
        SELECT p.profile_id,p.account_id,p.model,p.settings_json,
               p.availability_status,a.provider,
               a.settings_json AS account_settings_json
        FROM model_profiles p
        JOIN model_provider_accounts a ON a.account_id=p.account_id
        WHERE p.capability='tts' AND a.provider='doubao_tts'
        """
    ).fetchall()
    imported = 0
    observed = _iso()
    with db.transaction(immediate=True) as connection:
        for row in rows:
            profile_settings = _load(row["settings_json"], {})
            account_settings = _load(row["account_settings_json"], {})
            voice_id = _optional(profile_settings.get("voice"))
            if voice_id is None:
                continue
            resource_id = _optional(
                profile_settings.get("resource_id")
                or account_settings.get("tts_resource_id")
                or row["model"]
            )
            if resource_id is None:
                continue
            exists = connection.execute(
                "SELECT 1 FROM tts_voice_catalog "
                "WHERE account_id=? AND resource_id=? AND voice_id=?",
                (row["account_id"], resource_id, voice_id),
            ).fetchone()
            if exists is None:
                connection.execute(
                    """
                    INSERT INTO tts_voice_catalog(
                      account_id,resource_id,voice_id,display_name,source,
                      selectable,last_seen_at,updated_at
                    ) VALUES(?,?,?,'原有音色','legacy_saved',?,?,?)
                    """,
                    (
                        row["account_id"],
                        resource_id,
                        voice_id,
                        int(row["availability_status"] == "available"),
                        observed,
                        observed,
                    ),
                )
                imported += 1
            changed = False
            if profile_settings.get("default_voice_id") != voice_id:
                profile_settings["default_voice_id"] = voice_id
                changed = True
            if profile_settings.get("resource_id") != resource_id:
                profile_settings["resource_id"] = resource_id
                changed = True
            if changed:
                connection.execute(
                    "UPDATE model_profiles "
                    "SET settings_json=?,updated_at=datetime('now') "
                    "WHERE profile_id=?",
                    (_json(profile_settings, {}), row["profile_id"]),
                )
    return imported
