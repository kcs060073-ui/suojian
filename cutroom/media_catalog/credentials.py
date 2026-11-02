"""Local model credentials without storing secret values in SQLite or files."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
from typing import Mapping


TARGET_PREFIX = "cutroom/"
PROVIDER_TARGET_PREFIX = TARGET_PREFIX + "provider/"
PROVIDER_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CAPABILITY_SPECS = {
    "planning": {
        "label": "文案策划",
        "provider": "OpenAI 兼容",
        "model_env": ("ARK_MODEL", "LLM_MODEL"),
        "secret_env": ("ARK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"),
        "default_model": "未选择模型",
        "credential_required": True,
    },
    "broll": {
        "label": "B-roll 画面",
        "provider": "火山方舟视频生成",
        "model_env": ("SEEDANCE_MODEL",),
        "secret_env": ("SEEDANCE_API_KEY", "ARK_API_KEY"),
        "default_model": "未选择模型",
        "credential_required": True,
    },
    "copywriting": {
        "label": "文案分析与改写",
        "provider": "火山方舟",
        "model_env": ("ARK_MODEL", "LLM_MODEL"),
        "secret_env": ("ARK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"),
        "default_model": "未选择模型",
        "credential_required": True,
        "visible": False,
    },
    "storyboard": {
        "label": "分镜规划",
        "provider": "火山方舟",
        "model_env": ("ARK_MODEL", "LLM_MODEL"),
        "secret_env": ("ARK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"),
        "default_model": "未选择模型",
        "credential_required": True,
        "visible": False,
    },
    "packaging": {
        "label": "包装推荐",
        "provider": "火山方舟",
        "model_env": ("ARK_MODEL", "LLM_MODEL"),
        "secret_env": ("ARK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"),
        "default_model": "未选择模型",
        "credential_required": True,
        "visible": False,
    },
    "general_text": {
        "label": "旧版通用文本（兼容）",
        "provider": "火山方舟",
        "model_env": ("ARK_MODEL", "LLM_MODEL"),
        "secret_env": ("ARK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"),
        "default_model": "未选择模型",
        "credential_required": True,
        "visible": False,
    },
    "video_generation": {
        "label": "缺失 B-roll 生成",
        "provider": "火山方舟",
        "model_env": ("SEEDANCE_MODEL",),
        "secret_env": ("SEEDANCE_API_KEY", "ARK_API_KEY"),
        "default_model": "未选择模型",
        "credential_required": True,
        "visible": False,
    },
    "tts": {
        "label": "配音与试听",
        "provider": "火山豆包 TTS",
        "model_env": ("TTS_VOICE_TYPE",),
        "secret_env": ("TTS_ACCESS_TOKEN",),
        "default_model": "未选择音色",
        "credential_required": True,
    },
    "visual_matching": {
        "label": "素材语义匹配",
        "provider": "本地索引",
        "model_env": (),
        "secret_env": (),
        "default_model": "本地索引",
        "credential_required": False,
        "visible": False,
    },
}


class MemoryCredentialBackend:
    """Small injectable backend used by tests; repr deliberately hides values."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def write(self, reference: str, secret: str) -> None:
        self._values[reference] = secret

    def read(self, reference: str) -> str | None:
        return self._values.get(reference)

    def delete(self, reference: str) -> None:
        self._values.pop(reference, None)

    def references(self) -> list[str]:
        return sorted(self._values)

    def __repr__(self) -> str:
        return f"MemoryCredentialBackend(references={self.references()!r})"


class WindowsCredentialBackend:
    """Windows Credential Manager generic credentials via Advapi32."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Credential Manager 仅在 Windows 可用")
        self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        credential_pointer = ctypes.POINTER(self.CREDENTIALW)
        self._advapi.CredWriteW.argtypes = [
            ctypes.POINTER(self.CREDENTIALW),
            wintypes.DWORD,
        ]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(credential_pointer),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    def write(self, reference: str, secret: str) -> None:
        blob = secret.encode("utf-16-le")
        if len(blob) > 2560:
            raise ValueError("密钥过长")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = self.CREDENTIALW(
            0,
            self.CRED_TYPE_GENERIC,
            reference,
            "CUTROOM 本机模型服务密钥",
            wintypes.FILETIME(),
            len(blob),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
            self.CRED_PERSIST_LOCAL_MACHINE,
            0,
            None,
            None,
            "CUTROOM",
        )
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise OSError(ctypes.get_last_error(), "无法保存本机凭据")

    def read(self, reference: str) -> str | None:
        pointer = ctypes.POINTER(self.CREDENTIALW)()
        if not self._advapi.CredReadW(
            reference,
            self.CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise OSError(error, "无法读取本机凭据")
        try:
            credential = pointer.contents
            raw = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            return raw.decode("utf-16-le")
        finally:
            self._advapi.CredFree(pointer)

    def delete(self, reference: str) -> None:
        if self._advapi.CredDeleteW(reference, self.CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != self.ERROR_NOT_FOUND:
            raise OSError(error, "无法删除本机凭据")


_DEFAULT_BACKEND = None


def _backend(backend=None):
    global _DEFAULT_BACKEND
    if backend is not None:
        return backend
    if _DEFAULT_BACKEND is None:
        _DEFAULT_BACKEND = WindowsCredentialBackend()
    return _DEFAULT_BACKEND


def _spec(capability: str) -> dict:
    if capability not in CAPABILITY_SPECS:
        raise ValueError("未知能力")
    return CAPABILITY_SPECS[capability]


def _reference(capability: str) -> str:
    _spec(capability)
    return TARGET_PREFIX + capability


def save_secret(capability: str, secret: str, backend=None) -> str:
    reference = _reference(capability)
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("密钥不能为空")
    _backend(backend).write(reference, secret.strip())
    return reference


def read_secret(
    capability: str,
    backend=None,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    spec = _spec(capability)
    stored = _backend(backend).read(_reference(capability))
    if stored:
        return stored
    values = os.environ if env is None else env
    for key in spec["secret_env"]:
        value = values.get(key)
        if value:
            return value
    return None


def delete_secret(capability: str, backend=None) -> None:
    _backend(backend).delete(_reference(capability))


def _provider_reference(account_id: str) -> str:
    if (
        not isinstance(account_id, str)
        or PROVIDER_ACCOUNT_ID_RE.fullmatch(account_id) is None
    ):
        raise ValueError("账号 ID 只能包含字母、数字、点、下划线和连字符")
    return PROVIDER_TARGET_PREFIX + account_id


def _validate_provider_reference(reference: str) -> str:
    if not isinstance(reference, str) or not reference.startswith(PROVIDER_TARGET_PREFIX):
        raise ValueError("供应商凭据引用无效")
    account_id = reference[len(PROVIDER_TARGET_PREFIX):]
    if _provider_reference(account_id) != reference:
        raise ValueError("供应商凭据引用无效")
    return reference


def save_provider_secret(account_id: str, secret: str, backend=None) -> str:
    reference = _provider_reference(account_id)
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("密钥不能为空")
    _backend(backend).write(reference, secret.strip())
    return reference


def read_provider_secret(reference: str, backend=None) -> str | None:
    return _backend(backend).read(_validate_provider_reference(reference))


def delete_provider_secret(reference: str, backend=None) -> None:
    _backend(backend).delete(_validate_provider_reference(reference))


def _optional_secret(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Secret Access Key 必须是文字")
    return value.strip() or None


def encode_provider_credentials(
    access_token: str | None,
    secret_access_key: str | None,
    api_key: str | None = None,
) -> str:
    """Encode local-only Doubao secrets without exposing them in account data."""
    access_token = _optional_secret(access_token)
    secret_access_key = _optional_secret(secret_access_key)
    api_key = _optional_secret(api_key)
    if not any((access_token, secret_access_key, api_key)):
        raise ValueError("豆包凭据不能为空")
    return json.dumps(
        {
            "schema": 2,
            "access_token": access_token,
            "secret_access_key": secret_access_key,
            "api_key": api_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_provider_credentials(raw: str) -> dict[str, str | None]:
    """Decode a versioned secret bundle or a legacy plain TTS token."""
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict) or value.get("schema") not in {1, 2}:
        access_token = raw.strip() if isinstance(raw, str) else ""
        return {
            "access_token": access_token or None,
            "secret_access_key": None,
            "api_key": None,
        }
    access_token = value.get("access_token")
    secret_access_key = value.get("secret_access_key")
    api_key = value.get("api_key") if value.get("schema") == 2 else None
    return {
        "access_token": (
            access_token.strip() if isinstance(access_token, str) else None
        ) or None,
        "secret_access_key": (
            secret_access_key.strip()
            if isinstance(secret_access_key, str)
            else None
        ) or None,
        "api_key": (
            api_key.strip() if isinstance(api_key, str) else None
        ) or None,
    }


def configured_capabilities(
    backend=None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, dict]:
    values = os.environ if env is None else env
    result = {}
    for capability, spec in CAPABILITY_SPECS.items():
        configured = (
            True
            if not spec["credential_required"]
            else bool(read_secret(capability, backend, env=values))
        )
        result[capability] = {
            "configured": configured,
            "source": "local" if configured else None,
        }
    return result


def legacy_environment(project_root: Path | str) -> dict[str, str]:
    """Read legacy .env as compatibility input; never write it."""
    values = dict(os.environ)
    path = Path(project_root) / ".env"
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip(), value.strip().strip("\"'"))
    return values


def list_capability_settings(db) -> list[dict]:
    return [
        dict(row)
        for row in db.connection.execute(
            "SELECT capability,provider,model,credential_ref,last_checked_at "
            "FROM capability_settings ORDER BY capability"
        ).fetchall()
    ]


def save_capability_setting(
    db,
    capability: str,
    provider: str,
    model: str,
    credential_ref: str | None,
) -> None:
    _spec(capability)
    if not provider.strip() or not model.strip():
        raise ValueError("供应商和模型不能为空")
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO capability_settings(
              capability,provider,model,credential_ref,last_checked_at,updated_at
            ) VALUES(?,?,?,?,datetime('now'),datetime('now'))
            ON CONFLICT(capability) DO UPDATE SET
              provider=excluded.provider,
              model=excluded.model,
              credential_ref=excluded.credential_ref,
              last_checked_at=excluded.last_checked_at,
              updated_at=excluded.updated_at
            """,
            (capability, provider.strip(), model.strip(), credential_ref),
        )


def clear_capability_setting(db, capability: str) -> None:
    _spec(capability)
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM capability_settings WHERE capability=?",
            (capability,),
        )
