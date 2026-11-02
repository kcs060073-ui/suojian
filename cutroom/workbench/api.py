"""Gate-safe business API composed exclusively from media_catalog domains."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from urllib.parse import quote
import uuid

from media_catalog.ad_workflow import (
    AdTaskPreflightError,
    create_ad_task,
    preflight_ad_task,
    require_ad_picture_assets,
)
from media_catalog.asset_index import list_asset_index
from media_catalog.digital_workflow import (
    DigitalTaskPreflightError,
    create_digital_task,
    preflight_digital_task,
)
from media_catalog.credentials import (
    CAPABILITY_SPECS,
    clear_capability_setting,
    decode_provider_credentials,
    delete_provider_secret,
    delete_secret,
    encode_provider_credentials,
    legacy_environment,
    list_capability_settings,
    read_secret,
    read_provider_secret,
    save_capability_setting,
    save_provider_secret,
    save_secret,
)
from media_catalog.model_profile_probe import (
    ModelProfileProbeError,
    create_tts_voice_preview,
    list_provider_models,
    probe_model_profile,
)
from media_catalog.model_profiles import (
    ModelProfileInUse,
    ProviderAccountInUse,
    create_model_profile,
    create_provider_account,
    delete_provider_account,
    delete_model_profile,
    get_model_profile,
    get_provider_account,
    list_model_profiles,
    list_provider_accounts,
    list_task_profile_bindings,
    set_default_profile,
    update_model_profile_settings,
    update_provider_account_settings,
)
from media_catalog.provider_presets import provider_preset
from media_catalog.tts_voice_catalog import (
    get_provider_voice,
    list_provider_voices,
    refresh_provider_voice_catalog,
    voice_catalog_state,
)
from media_catalog.volcengine_voice_catalog import VoiceCatalogError
from media_catalog.candidate_versions import get_candidate, list_candidates
from media_catalog.full_review import (
    approve_after_full_watch,
    complete_watch,
    get_watch_session,
    latest_watch_session,
    record_watch_tick,
    start_watch,
)
from media_catalog.libraries import list_libraries, register_library, update_mount
from media_catalog.packaging import list_packaging
from media_catalog.packaging_plan import (
    clone_plan,
    create_blank_plan,
    disable_optional_slot,
    find_item,
    get_plan,
    list_personal_templates,
    list_plans,
    propose as propose_packaging,
    replace_item,
    load_manifest,
    resolve_manifest_asset_path,
    save_personal_template,
)
from media_catalog.packaging_preview import (
    confirm_plan_after_preview,
    current_preview_for_plan,
    mark_preview_viewed,
    request_preview,
)
from media_catalog.packaging_catalog_check import (
    latest_packaging_catalog_check,
    request_packaging_catalog_check,
)
from media_catalog.storage_status import (
    CacheCleanupPlanStore,
    clean_workbench_preview_cache,
    preview_workbench_cache_cleanup,
    workbench_storage_status,
)
from media_catalog.storage_settings import (
    configure_personal_packaging_root,
    default_personal_packaging_root,
    personal_packaging_root,
)
from media_catalog.publish import list_publish
from media_catalog.reviews import reject_review
from media_catalog.rework import (
    approve_rework_cost,
    execute_rework,
    list_rework_requests,
    preview_rework_impact,
    reject_with_rework,
)
from media_catalog.rough_cut import (
    apply_edit,
    preview_edit_impact,
    redo_last_edit,
    undo_last_edit,
)
from media_catalog.rough_media import (
    RoughMediaError,
    assert_picture_media_available,
    describe_rough_media,
    resolve_rough_media,
)
from media_catalog.material_authorization import (
    confirm_material_authorization,
    confirm_self_produced_material,
    get_material_authorization,
)
from media_catalog.scene_cards import list_scene_cards, set_picture_lock
from media_catalog.storyboard_feedback import (
    apply_feedback_candidate,
    record_feedback,
    rerank_scene_candidates,
)
from media_catalog.job_timing import job_timing
from media_catalog.snapshots import latest_review_candidate
from media_catalog.task_sources import (
    get_task_sources,
    list_eligible_assets,
    preview_source_sync,
)
from media_catalog.tasks import get_task, list_tasks, set_status
from media_catalog.workbench_worker import (
    JobRequestConflict,
    enqueue_job,
    get_job,
    list_jobs,
    request_cancel,
    request_job_pause,
    resume_job,
)
from media_catalog.workflow import WORKFLOW_STAGES, project_gates, project_workflow


STAGE_LABELS = (
    "创建与预检", "文案锁定", "素材增量同步", "AI分镜推荐", "粗剪",
    "调整画面顺序", "画面锁定", "包装方案确认", "精剪成片",
    "自动质检", "完整审片", "发布包",
)
ROUTES = (
    {"key": "digital_human", "label": "数字人混剪", "availability": "available"},
    {"key": "park_video", "label": "园区宣传片", "availability": "coming_later"},
    {"key": "ad_mix", "label": "信息流广告混剪", "availability": "available"},
)
@dataclass(frozen=True)
class ApiResult:
    status: int
    payload: dict | list


@dataclass(frozen=True)
class ApiError(Exception):
    status: int
    code: str
    message: str
    details: dict | None = None


def _read_task_script(project_root: Path, task: dict) -> str | None:
    """Project the saved script for review without accepting arbitrary file paths."""
    task_id = str(task.get("task_id") or "")
    script = (task.get("brief") or {}).get("final_script") or {}
    if (
        not task_id
        or Path(task_id).name != task_id
        or script.get("relative_path") != "inputs/final-script.txt"
    ):
        return None
    tasks_root = Path(project_root) / "runtime" / "workbench" / "tasks"
    task_root = tasks_root / task_id
    path = task_root / "inputs" / "final-script.txt"
    try:
        if not task_root.resolve(strict=True).is_relative_to(tasks_root.resolve(strict=True)):
            return None
        if not path.resolve(strict=True).is_relative_to(task_root.resolve(strict=True)):
            return None
        if path.stat().st_size > 200_000:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return None


def _active_route_adapter(route, digital_handler, ad_handler):
    if route == "digital_human":
        return digital_handler
    if route == "ad_mix":
        return ad_handler
    raise ApiError(400, "ROUTE_UNAVAILABLE", "该视频路线尚未开放。")


class WorkbenchAPI:
    def __init__(self, db, project_root: Path) -> None:
        self.db = db
        self.project_root = Path(project_root)

    def dispatch(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        cache_cleanup_plans: CacheCleanupPlanStore | None = None,
        query: dict[str, list[str]] | None = None,
    ) -> ApiResult:
        body = body or {}
        query = query or {}
        cleanup_plans = cache_cleanup_plans or CacheCleanupPlanStore()
        parts = _path_parts(path)
        if method == "GET" and parts == ["api", "bootstrap"]:
            return ApiResult(200, self.bootstrap())
        if method == "GET" and parts == ["api", "assets"]:
            try:
                return ApiResult(200, list_asset_index(
                    self.db,
                    task_id=_query_value(query, "taskId"),
                    query=_query_value(query, "query"),
                    library_id=_query_value(query, "libraryId"),
                    media_kind=_query_value(query, "mediaKind"),
                    availability=_query_value(query, "availability"),
                    authorization=_query_value(query, "authorization"),
                    limit=_query_integer(query, "limit", 100),
                    offset=_query_integer(query, "offset", 0),
                ))
            except ValueError as error:
                raise ApiError(
                    400,
                    "ASSET_INDEX_QUERY_INVALID",
                    "素材索引筛选条件无效，请检查后重试。",
                ) from error
        if parts == ["api", "libraries"]:
            if method == "GET":
                return ApiResult(200, {"libraries": self._libraries()})
            if method == "POST":
                return ApiResult(200, self._register_library(body))
        if (
            len(parts) == 5
            and parts[:3] == ["api", "settings", "provider-accounts"]
            and parts[4] == "credentials"
            and method == "PUT"
        ):
            return ApiResult(
                200,
                self._update_provider_credentials(parts[3], body),
            )
        if (
            len(parts) == 5
            and parts[:3] == ["api", "settings", "provider-accounts"]
            and parts[4] == "voices"
            and method == "GET"
        ):
            return ApiResult(200, self._provider_voices(parts[3]))
        if (
            len(parts) == 6
            and parts[:3] == ["api", "settings", "provider-accounts"]
            and parts[4:] == ["voices", "refresh"]
            and method == "POST"
        ):
            return ApiResult(200, self._refresh_provider_voices(parts[3]))
        if (
            len(parts) == 4
            and parts[:2] == ["api", "libraries"]
            and method == "POST"
        ):
            if parts[3] == "check":
                return self._check_library(parts[2])
            if parts[3] == "mount":
                return self._reconnect_library(parts[2], body)
        if parts == ["api", "packaging-catalog"] and method == "GET":
            return ApiResult(200, {
                "categories": _packaging_catalog(self.db, self.project_root)
            })
        if parts == ["api", "packaging-catalog", "check"] and method == "POST":
            result = request_packaging_catalog_check(self.db, self.project_root)
            return ApiResult(202, _public_catalog_check(self.db, result))
        if parts == ["api", "packaging-catalog", "checks", "latest"] and method == "GET":
            result = latest_packaging_catalog_check(self.db)
            return ApiResult(200, _public_catalog_check(self.db, result))
        if parts == ["api", "packaging-templates"] and method == "GET":
            route = (query.get("route") or [None])[0]
            if not isinstance(route, str) or not route.strip():
                raise ApiError(400, "PACKAGING_ROUTE_REQUIRED", "请选择模板适用的视频路线。")
            return ApiResult(200, {
                "templates": list_personal_templates(self.db, route.strip())
            })
        if parts == ["api", "settings", "storage"] and method == "GET":
            return ApiResult(200, workbench_storage_status(
                self.db, self.project_root
            ))
        if (
            parts == ["api", "settings", "storage", "personal-packaging"]
            and method == "PUT"
        ):
            try:
                setting = configure_personal_packaging_root(
                    self.db,
                    self.project_root,
                    _required_string(body, "path"),
                )
            except (OSError, ValueError):
                raise ApiError(
                    400,
                    "PERSONAL_PACKAGING_PATH_INVALID",
                    "请选择当前可访问且可写入的文件夹。",
                ) from None
            return ApiResult(200, setting)
        if (
            parts == ["api", "settings", "storage", "cache", "preview"]
            and method == "POST"
        ):
            return ApiResult(200, preview_workbench_cache_cleanup(
                self.db, cleanup_plans
            ))
        if (
            parts == ["api", "settings", "storage", "cache", "clean"]
            and method == "POST"
        ):
            impact_token = _optional_string(body, "impactToken")
            if impact_token is None:
                raise ApiError(
                    400,
                    "CACHE_PREVIEW_REQUIRED",
                    "请先预览本次缓存清理范围，再确认执行。",
                )
            return ApiResult(200, clean_workbench_preview_cache(
                self.db, cleanup_plans, impact_token=impact_token
            ))
        if parts == ["api", "settings", "capabilities"] and method == "GET":
            return ApiResult(200, {"capabilities": self._capabilities()})
        if parts == ["api", "settings", "model-profiles"] and method == "GET":
            return ApiResult(200, self._model_settings())
        if parts == ["api", "settings", "provider-accounts"] and method == "POST":
            return ApiResult(200, self._create_provider_account(body))
        if (
            len(parts) == 4
            and parts[:3] == ["api", "settings", "provider-accounts"]
            and method == "DELETE"
        ):
            return ApiResult(200, self._delete_provider_account(parts[3]))
        if (
            len(parts) == 5
            and parts[:3] == ["api", "settings", "provider-accounts"]
            and parts[4] == "models"
            and method == "POST"
        ):
            return ApiResult(200, self._provider_models(parts[3]))
        if parts == ["api", "settings", "model-profiles"] and method == "POST":
            return ApiResult(200, self._create_model_profile(body))
        if (
            len(parts) == 4
            and parts[:3] == ["api", "settings", "model-profiles"]
            and method == "DELETE"
        ):
            return ApiResult(200, self._delete_model_profile(parts[3]))
        if (
            len(parts) == 5
            and parts[:3] == ["api", "settings", "model-profiles"]
        ):
            if parts[4] == "voices" and method == "GET":
                return ApiResult(200, self._profile_voices(parts[3]))
            if parts[4] == "default-voice" and method == "PUT":
                return ApiResult(
                    200,
                    self._update_profile_default_voice(parts[3], body),
                )
            if parts[4] == "test" and method == "POST":
                return ApiResult(200, self._test_model_profile(parts[3]))
            if parts[4] == "default" and method == "POST":
                return ApiResult(200, set_default_profile(self.db, parts[3]))
            if parts[4] == "voice-test" and method == "POST":
                return ApiResult(200, self._test_profile_voice(parts[3], body))
        if (
            len(parts) == 4
            and parts[:3] == ["api", "settings", "capabilities"]
        ):
            if method == "PUT":
                return ApiResult(200, self._save_capability(parts[3], body))
            if method == "DELETE":
                return ApiResult(200, self._delete_capability(parts[3]))
        if parts == ["api", "tasks"]:
            if method == "GET":
                return ApiResult(200, {"tasks": self.task_summaries()})
            if method == "POST":
                creator = _active_route_adapter(
                    body.get("route"), create_digital_task, create_ad_task
                )
                return ApiResult(200, creator(self.db, body, self.project_root))
        if parts == ["api", "jobs"] and method == "GET":
            return ApiResult(200, {"jobs": [_public_job(self.db, item) for item in list_jobs(self.db)]})
        if method == "POST" and parts == ["api", "tasks", "preflight"]:
            preflight = _active_route_adapter(
                body.get("route"), preflight_digital_task, preflight_ad_task
            )
            return ApiResult(200, preflight(self.db, body, self.project_root))
        if len(parts) >= 3 and parts[:2] == ["api", "tasks"]:
            return self._task_route(method, parts[2], parts[3:], body)
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
            if len(parts) == 3 and method == "GET":
                return ApiResult(200, _public_job(self.db, get_job(self.db, parts[2])))
            if len(parts) == 4 and method == "POST":
                return self._job_action(parts[2], parts[3], body)
        raise ApiError(404, "NOT_FOUND", "未找到该接口。")

    def bootstrap(self) -> dict:
        return {
            "tasks": self.task_summaries(),
            "workflowStages": [
                {"key": key, "label": label}
                for key, label in zip(WORKFLOW_STAGES, STAGE_LABELS, strict=True)
            ],
            "routes": [dict(item) for item in ROUTES],
            "libraries": self._libraries(),
            "packagingAssets": list_packaging(self.db),
            "packagingCatalog": _packaging_catalog(self.db, self.project_root),
            "settingsCapabilities": self._capabilities(),
            "modelProfiles": list_model_profiles(self.db),
            "providerAccounts": self._provider_accounts(),
            "jobs": [_public_job(self.db, item) for item in list_jobs(self.db)],
        }

    def _libraries(self) -> list[dict]:
        return [_public_library(item) for item in list_libraries(self.db)]

    def _register_library(self, body: dict) -> dict:
        name = _required_string(body, "name")
        mount_path = Path(_required_string(body, "mountPath"))
        if not mount_path.is_dir():
            raise ApiError(409, "LIBRARY_OFFLINE", "所选素材目录当前不可访问。")
        purpose = _required_string(body, "purpose")
        kind = _optional_string(body, "kind") or "local"
        library_id = _optional_string(body, "libraryId") or f"lib-{uuid.uuid4().hex}"
        register_library(
            self.db,
            library_id,
            name,
            kind,
            str(mount_path),
            purpose=purpose,
            is_default_generic=bool(body.get("isDefaultGeneric", False)),
        )
        item = next(
            item for item in list_libraries(self.db)
            if item["library_id"] == library_id
        )
        return _public_library(item)

    def _check_library(self, library_id: str) -> ApiResult:
        libraries = {
            item["library_id"]: item for item in list_libraries(self.db)
        }
        if library_id not in libraries:
            raise ApiError(404, "NOT_FOUND", "素材目录不存在。")
        job_id = enqueue_job(
            self.db,
            job_id=f"job-{uuid.uuid4().hex}",
            job_type="scan",
            library_id=library_id,
            payload={"action": "scan", "library_id": library_id, "catalog_only": True},
        )
        return ApiResult(202, _public_job(self.db, get_job(self.db, job_id)))

    def _reconnect_library(self, library_id: str, body: dict) -> ApiResult:
        libraries = {
            item["library_id"]: item for item in list_libraries(self.db)
        }
        if library_id not in libraries:
            raise ApiError(404, "NOT_FOUND", "素材目录不存在。")
        mount_path = Path(_required_string(body, "mountPath"))
        if not mount_path.is_dir():
            raise ApiError(409, "LIBRARY_OFFLINE", "所选素材目录当前不可访问。")
        update_mount(self.db, library_id, str(mount_path))
        job_id = enqueue_job(
            self.db,
            job_id=f"job-{uuid.uuid4().hex}",
            job_type="scan",
            library_id=library_id,
            payload={
                "action": "scan",
                "library_id": library_id,
                "catalog_only": True,
            },
        )
        library = next(
            item for item in list_libraries(self.db)
            if item["library_id"] == library_id
        )
        return ApiResult(202, {
            "library": _public_library(library),
            "job": _public_job(self.db, get_job(self.db, job_id)),
        })

    def _capability(self, capability: str) -> dict:
        rows = {
            item["capability"]: item
            for item in list_capability_settings(self.db)
        }
        environment = legacy_environment(self.project_root)
        spec = CAPABILITY_SPECS[capability]
        row = rows.get(capability, {})
        model = row.get("model") or _first_environment(
            environment, spec["model_env"]
        ) or spec["default_model"]
        provider = row.get("provider") or spec["provider"]
        configured = (
            True
            if not spec["credential_required"]
            else bool(read_secret(capability, env=environment))
        )
        return {
            "capability": capability,
            "label": spec["label"],
            "provider": provider,
            "model": model,
            "configured": configured,
            "lastCheckedAt": row.get("last_checked_at"),
        }

    def _capabilities(self) -> list[dict]:
        result = []
        for capability, spec in CAPABILITY_SPECS.items():
            if not spec.get("visible", True):
                continue
            result.append(self._capability(capability))
        return result

    def _save_capability(self, capability: str, body: dict) -> dict:
        if capability not in CAPABILITY_SPECS:
            raise ApiError(404, "NOT_FOUND", "模型能力不存在。")
        provider = _required_string(body, "provider")
        model = _required_string(body, "model")
        secret = _optional_string(body, "secret")
        current = {
            item["capability"]: item
            for item in list_capability_settings(self.db)
        }.get(capability, {})
        credential_ref = current.get("credential_ref")
        if secret is not None:
            credential_ref = save_secret(capability, secret)
        save_capability_setting(
            self.db,
            capability,
            provider,
            model,
            credential_ref,
        )
        result = self._capability(capability)
        if secret is not None:
            result["configured"] = True
        return result

    def _delete_capability(self, capability: str) -> dict:
        if capability not in CAPABILITY_SPECS:
            raise ApiError(404, "NOT_FOUND", "模型能力不存在。")
        delete_secret(capability)
        clear_capability_setting(self.db, capability)
        return self._capability(capability)

    def _model_settings(self) -> dict:
        return {
            "accounts": self._provider_accounts(),
            "profiles": list_model_profiles(self.db),
        }

    def _provider_accounts(self) -> list[dict]:
        return [
            self._provider_account_status(account)
            for account in list_provider_accounts(self.db)
        ]

    def _provider_account_status(
        self,
        account: dict,
        *,
        raw_secret: str | None = None,
        secret_supplied: bool = False,
    ) -> dict:
        result = dict(account)
        if account.get("provider") != "doubao_tts":
            return result
        if not secret_supplied:
            internal = get_provider_account(
                self.db,
                account["accountId"],
                include_credential=True,
            )
            raw_secret = (
                None
                if internal is None
                else read_provider_secret(internal["credentialRef"])
            )
        credentials = decode_provider_credentials(raw_secret or "")
        settings = account.get("settings") or {}
        result["ttsApiKeyConfigured"] = bool(credentials.get("api_key"))
        result["legacyTtsConfigured"] = bool(
            credentials.get("access_token") and settings.get("app_id")
        )
        result["ttsConfigured"] = bool(
            result["ttsApiKeyConfigured"] or result["legacyTtsConfigured"]
        )
        result["voiceCatalogConfigured"] = bool(
            credentials.get("secret_access_key")
            and settings.get("access_key_id")
            and settings.get("tts_resource_id")
        )
        return result

    def _create_provider_account(self, body: dict) -> dict:
        account_id = _optional_string(body, "accountId") or (
            "account-" + uuid.uuid4().hex
        )
        preset_id = (
            _optional_string(body, "preset")
            or _optional_string(body, "provider")
            or "openai_compatible"
        )
        try:
            preset = provider_preset(preset_id)
        except ValueError as error:
            raise ApiError(400, "INVALID_REQUEST", str(error)) from error
        tts_resource_id = _optional_string(body, "ttsResourceId")
        if preset_id == "doubao_tts":
            supported_resources = {
                item["id"] for item in preset["tts_resources"]
            }
            if tts_resource_id not in supported_resources:
                raise ApiError(
                    400,
                    "INVALID_REQUEST",
                    "请选择受支持的豆包配音服务版本。",
                )
        if preset_id == "doubao_tts":
            access_token = _optional_string(body, "secret")
            api_key = _optional_string(body, "ttsApiKey")
            app_id = _optional_string(body, "appId")
            if not api_key and not (access_token and app_id):
                raise ApiError(
                    400,
                    "INVALID_REQUEST",
                    "请填写新版豆包 API Key，或同时填写旧版 AppID 和 Access Token。",
                )
            stored_secret = encode_provider_credentials(
                access_token,
                _optional_string(body, "secretAccessKey"),
                api_key,
            )
        else:
            stored_secret = _required_string(body, "secret")
        name = _required_string(body, "name")
        channel_id = body.get("channelId")
        if channel_id == "":
            channel_id = None
        if isinstance(channel_id, bool):
            raise ApiError(400, "INVALID_REQUEST", "渠道 ID 必须是正整数。")
        if channel_id is not None:
            try:
                channel_id = int(channel_id)
            except (TypeError, ValueError) as error:
                raise ApiError(
                    400, "INVALID_REQUEST", "渠道 ID 必须是正整数。"
                ) from error
            if channel_id <= 0:
                raise ApiError(400, "INVALID_REQUEST", "渠道 ID 必须是正整数。")
        base_url = (
            _optional_string(body, "baseUrl")
            or preset["default_base_url"]
        )
        if not base_url:
            raise ApiError(400, "INVALID_REQUEST", "API 请求地址不能为空。")
        settings = {
            "preset": preset_id,
            "protocol": preset["protocol"],
            "note": _optional_string(body, "note"),
            "default_model": _optional_string(body, "defaultModel"),
            "app_id": _optional_string(body, "appId"),
            "cluster": _optional_string(body, "cluster"),
            "access_key_id": _optional_string(body, "accessKeyId"),
            "tts_resource_id": tts_resource_id,
        }
        if channel_id is not None:
            settings["channel_id"] = channel_id
        # Serialize the existence check and credential write with other account
        # creators. A duplicate request must never overwrite/delete the live key.
        account = None
        with self.db.transaction(immediate=True):
            if get_provider_account(self.db, account_id) is None:
                reference = save_provider_secret(account_id, stored_secret)
                try:
                    account = create_provider_account(
                        self.db,
                        account_id=account_id,
                        name=name,
                        provider=preset_id,
                        base_url=base_url,
                        credential_ref=reference,
                        settings=settings,
                    )
                except BaseException:
                    delete_provider_secret(reference)
                    raise
        if account is None:
            raise ApiError(409, "ACCOUNT_ALREADY_EXISTS", "供应商账号已存在。")
        return self._provider_account_status(
            account, raw_secret=stored_secret, secret_supplied=True,
        )

    def _update_provider_credentials(self, account_id: str, body: dict) -> dict:
        account = get_provider_account(
            self.db,
            account_id,
            include_credential=True,
        )
        if account is None:
            raise ApiError(404, "NOT_FOUND", "供应商账号不存在。")
        if account.get("provider") != "doubao_tts":
            raise ApiError(
                400,
                "INVALID_REQUEST",
                "只有豆包配音账号支持更新目录凭据。",
            )
        current = decode_provider_credentials(
            read_provider_secret(account["credentialRef"]) or ""
        )
        access_token = _optional_string(body, "secret") or current.get(
            "access_token"
        )
        api_key = _optional_string(body, "ttsApiKey") or current.get(
            "api_key"
        )
        secret_access_key = _optional_string(
            body, "secretAccessKey"
        ) or current.get("secret_access_key")
        settings = dict(account.get("settings") or {})
        for request_key, settings_key in (
            ("accessKeyId", "access_key_id"),
            ("appId", "app_id"),
            ("ttsResourceId", "tts_resource_id"),
        ):
            value = _optional_string(body, request_key)
            if value is not None:
                settings[settings_key] = value

        preset = provider_preset("doubao_tts")
        resource_ids = {item["id"] for item in preset["tts_resources"]}
        resource_id = settings.get("tts_resource_id")
        if resource_id is not None and resource_id not in resource_ids:
            raise ApiError(
                400,
                "INVALID_REQUEST",
                "请选择受支持的豆包配音服务版本。",
            )
        if not api_key and not (
            access_token and str(settings.get("app_id") or "").strip()
        ):
            raise ApiError(
                400,
                "INVALID_REQUEST",
                "请填写新版豆包 API Key，或同时填写旧版 AppID 和 Access Token。",
            )

        stored_secret = encode_provider_credentials(
            access_token,
            secret_access_key,
            api_key,
        )
        save_provider_secret(account_id, stored_secret)
        updated = update_provider_account_settings(
            self.db,
            account_id,
            settings,
        )
        return self._provider_account_status(
            updated,
            raw_secret=stored_secret,
            secret_supplied=True,
        )

    def _create_model_profile(self, body: dict) -> dict:
        capability = _required_string(body, "capability")
        account_id = _required_string(body, "accountId")
        account = get_provider_account(self.db, account_id)
        if account is None:
            raise ApiError(400, "INVALID_REQUEST", "供应商账号不存在。")
        protocol = _optional_string(body, "protocol")
        if protocol is None:
            protocol = _optional_string(account.get("settings", {}), "protocol")
        if protocol is None:
            raise ApiError(400, "INVALID_REQUEST", "供应商协议未配置。")
        if account.get("provider") == "doubao_tts":
            if capability != "tts":
                raise ApiError(
                    400,
                    "INVALID_REQUEST",
                    "豆包配音账号只能用于配音配置。",
                )
            resource_id = _required_string(body, "resourceId")
            account_resource = str(
                (account.get("settings") or {}).get("tts_resource_id") or ""
            ).strip()
            if resource_id != account_resource:
                raise ApiError(
                    400,
                    "VOICE_RESOURCE_MISMATCH",
                    "配音服务版本与账号当前配置不一致。",
                )
            voice = self._selectable_voice(
                account_id,
                resource_id,
                _required_string(body, "defaultVoiceId"),
            )
            settings = {
                "protocol": "volcengine_tts",
                "resource_id": resource_id,
                "default_voice_id": voice["voiceId"],
                "voice": voice["voiceId"],
            }
            model = resource_id
        else:
            settings = {"protocol": protocol}
            voice_id = _optional_string(body, "voice")
            if voice_id is not None:
                settings["voice"] = voice_id
            model = _required_string(body, "model")
        return create_model_profile(
            self.db,
            profile_id=_optional_string(body, "profileId") or (
                "profile-" + uuid.uuid4().hex
            ),
            capability=capability,
            display_name=_required_string(body, "displayName"),
            account_id=account_id,
            model=model,
            settings=settings,
            is_default=bool(body.get("isDefault", False)),
        )

    def _provider_voice_scope(self, account_id: str) -> tuple[dict, str]:
        account = get_provider_account(self.db, account_id)
        if account is None:
            raise ApiError(404, "NOT_FOUND", "供应商账号不存在。")
        if account.get("provider") != "doubao_tts":
            raise ApiError(
                400,
                "VOICE_CATALOG_UNSUPPORTED_PROVIDER",
                "该供应商不支持自动读取豆包音色。",
            )
        resource_id = str(
            (account.get("settings") or {}).get("tts_resource_id") or ""
        ).strip()
        if not resource_id:
            raise ApiError(
                409,
                "VOICE_CATALOG_RESOURCE_MISSING",
                "请先选择豆包配音服务版本。",
            )
        return account, resource_id

    def _provider_voices(self, account_id: str) -> dict:
        _account, resource_id = self._provider_voice_scope(account_id)
        return {
            "voices": list_provider_voices(self.db, account_id, resource_id),
            "state": voice_catalog_state(self.db, account_id, resource_id),
        }

    def _refresh_provider_voices(self, account_id: str) -> dict:
        self._provider_voice_scope(account_id)
        try:
            return refresh_provider_voice_catalog(self.db, account_id)
        except VoiceCatalogError as error:
            raise ApiError(409, error.code, error.safe_message) from error

    def _selectable_voice(
        self,
        account_id: str,
        resource_id: str,
        voice_id: str,
    ) -> dict:
        voice = get_provider_voice(
            self.db,
            account_id,
            resource_id,
            voice_id,
        )
        if voice is None or not voice["selectable"]:
            raise ApiError(
                400,
                "VOICE_NOT_SELECTABLE",
                "请选择当前配音服务目录中的可用音色。",
            )
        return voice

    def _profile_voice_scope(self, profile_id: str) -> tuple[dict, str]:
        profile = get_model_profile(self.db, profile_id)
        if profile is None:
            raise ApiError(404, "NOT_FOUND", "模型配置不存在。")
        if profile.get("capability") != "tts" or profile.get("provider") != "doubao_tts":
            raise ApiError(
                400,
                "VOICE_CATALOG_UNSUPPORTED_PROFILE",
                "该配音配置不支持自动音色目录。",
            )
        settings = profile.get("settings") or {}
        resource_id = str(settings.get("resource_id") or profile.get("model") or "").strip()
        return profile, resource_id

    def _profile_voices(self, profile_id: str) -> dict:
        profile, resource_id = self._profile_voice_scope(profile_id)
        settings = profile.get("settings") or {}
        return {
            "profileId": profile_id,
            "resourceId": resource_id,
            "defaultVoiceId": settings.get("default_voice_id") or settings.get("voice"),
            "voices": list_provider_voices(
                self.db,
                profile["accountId"],
                resource_id,
                selectable_only=True,
            ),
            "state": voice_catalog_state(
                self.db,
                profile["accountId"],
                resource_id,
            ),
        }

    def _update_profile_default_voice(
        self,
        profile_id: str,
        body: dict,
    ) -> dict:
        profile, resource_id = self._profile_voice_scope(profile_id)
        voice = self._selectable_voice(
            profile["accountId"],
            resource_id,
            _required_string(body, "voiceId"),
        )
        settings = dict(profile.get("settings") or {})
        settings.update(
            {
                "default_voice_id": voice["voiceId"],
                "voice": voice["voiceId"],
                "voice_name": voice["displayName"],
                "resource_id": resource_id,
            }
        )
        return update_model_profile_settings(
            self.db,
            profile_id,
            settings,
        )

    def _test_profile_voice(self, profile_id: str, body: dict) -> dict:
        profile, resource_id = self._profile_voice_scope(profile_id)
        voice_id = _required_string(body, "voiceId")
        self._selectable_voice(profile["accountId"], resource_id, voice_id)
        try:
            return create_tts_voice_preview(
                self.db,
                profile_id,
                voice_id,
                self.project_root,
            )
        except ModelProfileProbeError as error:
            raise ApiError(
                422,
                "TTS_VOICE_TEST_FAILED",
                str(error),
            ) from error

    def _provider_models(self, account_id: str) -> dict:
        try:
            return {"models": list_provider_models(self.db, account_id)}
        except ModelProfileProbeError as error:
            raise ApiError(
                409,
                "PROVIDER_MODEL_CATALOG_UNAVAILABLE",
                str(error),
            ) from error

    def _delete_provider_account(self, account_id: str) -> dict:
        try:
            deleted = delete_provider_account(
                self.db,
                account_id,
                credential_deleter=delete_provider_secret,
            )
        except ProviderAccountInUse as error:
            raise ApiError(409, "PROVIDER_ACCOUNT_IN_USE", str(error)) from error
        if deleted is None:
            raise ApiError(404, "NOT_FOUND", "供应商账号不存在。")
        return deleted

    def _test_model_profile(self, profile_id: str) -> dict:
        try:
            return probe_model_profile(self.db, profile_id)
        except ModelProfileProbeError as error:
            profile = get_model_profile(self.db, profile_id)
            if profile is not None and profile.get("lastErrorCode"):
                raise ApiError(
                    422,
                    profile["lastErrorCode"],
                    profile.get("lastErrorMessage") or str(error),
                    {
                        "profileId": profile["profileId"],
                        "availabilityStatus": profile["availabilityStatus"],
                        "lastCheckedAt": profile.get("lastCheckedAt"),
                        "lastErrorMessage": profile.get("lastErrorMessage"),
                    },
                ) from error
            raise ApiError(
                409,
                "MODEL_PROFILE_UNAVAILABLE",
                str(error),
            ) from error

    def _delete_model_profile(self, profile_id: str) -> dict:
        try:
            deleted = delete_model_profile(self.db, profile_id)
        except ModelProfileInUse as error:
            raise ApiError(409, "MODEL_PROFILE_IN_USE", str(error)) from error
        if deleted is None:
            raise ApiError(404, "NOT_FOUND", "模型配置不存在。")
        return deleted

    def task_summaries(self) -> list[dict]:
        result = []
        for task in list_tasks(self.db):
            workflow = project_workflow(self.db, task["task_id"])
            result.append({
                "taskId": task["task_id"],
                "title": task["title"],
                "route": task["task_type"],
                "orchestrationMode": task["orchestration_mode"],
                "updatedAt": task["updated_at"],
                "currentStage": workflow["current_stage"],
                "runStatus": workflow["run_status"],
                "nextAction": workflow["next_action"],
                "gates": workflow["gates"],
            })
        return result

    def _task_route(self, method: str, task_id: str, rest: list[str], body: dict) -> ApiResult:
        task = get_task(self.db, task_id)
        if task is None:
            raise ApiError(404, "NOT_FOUND", "任务不存在。")
        if method == "GET" and not rest:
            return ApiResult(200, self._task_detail(task))
        key = tuple(rest)
        if (
            method == "GET"
            and len(key) == 3
            and key[0] == "packaging"
            and key[2] == "preview"
        ):
            _owned_plan(self.db, task_id, key[1])
            return ApiResult(200, _public_packaging_preview(
                current_preview_for_plan(self.db, task_id, key[1]),
                task_id,
                key[1],
            ))
        if method != "POST":
            raise ApiError(404, "NOT_FOUND", "未找到该接口。")

        if key == ("brief", "lock"):
            if not isinstance(task.get("brief"), dict) or not task["brief"].get("final_script"):
                raise ApiError(409, "FINAL_SCRIPT_REQUIRED", "必须先提供本次最终口播文案。")
            set_status(self.db, task_id, "brief_locked")
            if task.get("task_type") == "ad_mix":
                return ApiResult(200, self._task_detail(get_task(self.db, task_id)))
            return self._enqueue(task_id, "transcribe", {})
        if key == ("sources", "preview"):
            return ApiResult(200, preview_source_sync(self.db, task_id))
        if key == ("sources", "sync"):
            _require_gate(self.db, task_id, "brief_locked", "BRIEF_GATE_MISSING")
            token = _required_string(body, "impactToken")
            return self._enqueue(task_id, "sync_sources", {"impact_token": token})
        if key == ("storyboard", "recommend"):
            _require_gate(self.db, task_id, "brief_locked", "BRIEF_GATE_MISSING")
            if task.get("orchestration_mode") == "agent":
                raise ApiError(
                    409,
                    "AGENT_PLANNING_REQUIRED",
                    "Agent 模式必须由当前 Agent 提交规划，不能调用 CUTROOM 文案模型。",
                )
            if body.get("confirmPaidCall") is not True:
                raise ApiError(409, "PAID_CONFIRMATION_REQUIRED", "本次 AI 推荐可能产生费用，请确认后继续。")
            request_id = _required_string(body, "requestId")
            return self._enqueue(
                task_id,
                "recommend_storyboard",
                {"request_id": request_id},
                cost_status="approved",
                provider_request_id=f"storyboard:{task_id}:{request_id}",
            )
        if key == ("storyboard", "feedback"):
            decision = _required_string(body, "decision")
            if decision not in {"rejected", "correction"}:
                raise ApiError(
                    400,
                    "STORYBOARD_FEEDBACK_INVALID",
                    "这里只能记录拒绝或纠正说明。",
                )
            return ApiResult(200, record_feedback(
                self.db,
                task_id,
                _required_string(body, "cardId"),
                decision,
                _required_string(body, "instruction"),
            ))
        if key == ("storyboard", "rerank"):
            return ApiResult(200, rerank_scene_candidates(
                self.db,
                task_id,
                _required_string(body, "cardId"),
                _required_string(body, "instruction"),
            ))
        if key == ("storyboard", "apply-candidate"):
            return ApiResult(200, apply_feedback_candidate(
                self.db,
                task_id,
                _required_string(body, "cardId"),
                _required_string(body, "feedbackId"),
                _required_string(body, "assetId"),
                impact_token=_optional_string(body, "impactToken"),
            ))
        if key == ("rough-cut", "impact"):
            return ApiResult(200, preview_edit_impact(self.db, task_id, _required_dict(body, "edit")))
        if key == ("rough-cut", "apply"):
            return ApiResult(200, apply_edit(
                self.db,
                task_id,
                _required_dict(body, "edit"),
                impact_token=_optional_string(body, "impactToken"),
            ))
        if key == ("rough-cut", "undo"):
            return ApiResult(200, undo_last_edit(self.db, task_id))
        if key == ("rough-cut", "redo"):
            return ApiResult(200, redo_last_edit(self.db, task_id))
        if len(key) == 3 and key[0] == "rough-media" and key[2] == "preview":
            asset_id = key[1]
            # Resolve through the task-scoped media boundary before queueing any
            # local transcoding work. This rejects unrelated library assets.
            resolve_rough_media(self.db, task_id, asset_id, "preview")
            return self._enqueue(
                task_id,
                "generate_preview",
                {"asset_id": asset_id},
            )
        if key == ("picture", "lock"):
            media_versions = _required_dict(body, "mediaVersions")
            assert_picture_media_available(self.db, task_id, media_versions)
            if task.get("task_type") == "ad_mix":
                require_ad_picture_assets(self.db, task_id)
            return ApiResult(200, set_picture_lock(self.db, task_id, confirm_all=True))
        if key == ("packaging", "propose"):
            _require_gate(self.db, task_id, "picture_lock", "PICTURE_LOCK_MISSING")
            ids = propose_packaging(self.db, task_id, None)
            return ApiResult(200, {"planIds": ids, "plans": list_plans(self.db, task_id)})
        if key == ("packaging", "compose"):
            _require_gate(self.db, task_id, "picture_lock", "PICTURE_LOCK_MISSING")
            return ApiResult(200, self._compose_packaging(task, body))
        if len(key) == 3 and key[0] == "packaging" and key[2] == "preview":
            _owned_plan(self.db, task_id, key[1])
            result = request_preview(self.db, task_id, key[1])
            artifact = current_preview_for_plan(self.db, task_id, key[1])
            return ApiResult(
                202 if result.get("job") else 200,
                _public_preview_request(self.db, result, task_id, artifact),
            )
        if len(key) == 3 and key[0] == "packaging-previews" and key[2] == "viewed":
            return ApiResult(200, _public_packaging_preview(
                mark_preview_viewed(self.db, task_id, key[1]),
                task_id,
            ))
        if len(key) == 3 and key[0] == "packaging" and key[2] == "save-template":
            _owned_plan(self.db, task_id, key[1])
            return ApiResult(200, save_personal_template(
                self.db, key[1], _required_string(body, "templateName")
            ))
        if len(key) == 3 and key[0] == "packaging" and key[2] == "confirm":
            _owned_plan(self.db, task_id, key[1])
            return ApiResult(200, confirm_plan_after_preview(
                self.db,
                task_id,
                key[1],
                _optional_string(body, "previewId"),
                project_root=self.project_root,
            ))
        if key == ("render",):
            _require_gate(self.db, task_id, "packaging_confirmed", "PACKAGING_PLAN_MISSING")
            return self._enqueue(task_id, "render_and_qa", {})
        if key == ("material-authorization",):
            basis = body.get("basis", "task_only")
            if basis not in ("task_only", "self_produced"):
                raise ValueError("MATERIAL_AUTHORIZATION_BASIS_INVALID: 素材来源类型无效")
            confirm = (
                confirm_self_produced_material
                if basis == "self_produced"
                else confirm_material_authorization
            )
            return ApiResult(200, confirm(
                self.db,
                task_id,
                _required_string(body, "confirmedBy"),
            ))
        if key == ("watch", "start"):
            _require_candidate_binding(
                self.db,
                task_id,
                body,
                snapshot_id=_required_string(body, "snapshotId"),
                qa_report_id=_required_string(body, "qaReportId"),
            )
            return ApiResult(200, start_watch(
                self.db,
                task_id,
                _required_string(body, "snapshotId"),
                _required_string(body, "qaReportId"),
            ))
        if len(key) == 3 and key[0] == "watch" and key[2] in {"tick", "complete"}:
            session = get_watch_session(self.db, key[1])
            if session is None or session["task_id"] != task_id:
                raise ApiError(404, "NOT_FOUND", "观看记录不存在。")
            if key[2] == "tick":
                position = body.get("positionSeconds")
                return ApiResult(200, record_watch_tick(self.db, key[1], position))
            return ApiResult(200, complete_watch(self.db, key[1]))
        if key == ("review", "approve"):
            _require_candidate_binding(
                self.db,
                task_id,
                body,
                snapshot_id=_required_string(body, "snapshotId"),
                qa_report_id=_required_string(body, "qaReportId"),
            )
            return ApiResult(200, approve_after_full_watch(
                self.db, task_id,
                _required_string(body, "snapshotId"),
                _required_string(body, "qaReportId"),
                _required_string(body, "watchSessionId"),
                _required_string(body, "reviewedBy"),
                _optional_string(body, "notes"),
            ))
        if key == ("review", "reject"):
            return ApiResult(200, reject_review(
                self.db, task_id,
                _required_string(body, "snapshotId"),
                _required_string(body, "qaReportId"),
                _required_string(body, "reviewedBy"),
                _required_string(body, "notes"),
            ))
        if key == ("review", "rework", "preview"):
            return ApiResult(200, _public_rework_impact(preview_rework_impact(
                _required_string(body, "category"),
                technical_scope=_optional_string(body, "technicalScope"),
            )))
        if key == ("review", "rework"):
            candidate = _require_candidate_binding(self.db, task_id, body)
            result = reject_with_rework(
                self.db,
                task_id,
                candidate["candidate_id"],
                reviewed_by=_required_string(body, "reviewedBy"),
                category=_required_string(body, "category"),
                notes=_required_string(body, "notes"),
                position_seconds=_optional_number(body, "positionSeconds"),
                qa_issue_ref=_optional_string(body, "qaIssueRef"),
                idempotency_key=_required_string(body, "idempotencyKey"),
                technical_scope=_optional_string(body, "technicalScope"),
            )
            return ApiResult(200, {
                "review": _public_review(result["review"]),
                "rework": _public_rework(result["rework"]),
            })
        if (
            len(key) == 4
            and key[:2] == ("review", "rework")
            and key[3] == "execute"
        ):
            if body.get("confirmCost") is True:
                approve_rework_cost(self.db, task_id, key[2])
            result = execute_rework(
                self.db,
                task_id,
                key[2],
                idempotency_key=_required_string(body, "idempotencyKey"),
            )
            return ApiResult(200, _public_rework(result))
        if key == ("publish",):
            candidate = None
            if _optional_string(body, "candidateId") is not None:
                candidate = _require_candidate_binding(self.db, task_id, body)
            _require_gate(self.db, task_id, "human_reviewed", "REVIEW_NOT_APPROVED")
            if candidate is None:
                candidate = _require_candidate_binding(self.db, task_id, body)
            cover_choice = _optional_string(body, "coverChoice") or "candidate-1"
            delivery_type = _optional_string(body, "deliveryType") or "standard"
            external_cover_path = _optional_string(body, "externalCoverPath")
            if cover_choice not in {
                "candidate-1", "candidate-2", "candidate-3", "external", "pending",
            }:
                raise ApiError(400, "INVALID_REQUEST", "coverChoice 不受支持")
            if delivery_type not in {"standard", "editable"}:
                raise ApiError(400, "INVALID_REQUEST", "deliveryType 不受支持")
            if cover_choice == "external" and external_cover_path is None:
                raise ApiError(400, "INVALID_REQUEST", "外部封面模式必须提供 externalCoverPath")
            if cover_choice != "external" and external_cover_path is not None:
                raise ApiError(400, "INVALID_REQUEST", "只有外部封面模式可提供 externalCoverPath")
            return self._enqueue(task_id, "build_publish_package", {
                "candidate_id": candidate["candidate_id"],
                "platform": _required_string(body, "platform"),
                "title": _optional_string(body, "title"),
                "description": _optional_string(body, "description"),
                "tags": _string_list(body, "tags"),
                "cover_choice": cover_choice,
                "external_cover_path": external_cover_path,
                "delivery_type": delivery_type,
            })
        raise ApiError(404, "NOT_FOUND", "未找到该接口。")

    def _task_detail(self, task: dict) -> dict:
        task_id = task["task_id"]
        review_candidate = latest_review_candidate(self.db, task_id)
        candidates = list_candidates(self.db, task_id)
        current = candidates[0] if candidates else None
        current_id = None if current is None else current["candidate_id"]
        scene_cards = self._with_rough_media(
            task_id,
            list_scene_cards(self.db, task_id),
        )
        eligible_assets = self._with_rough_media(
            task_id,
            list_eligible_assets(self.db, task_id),
        )
        return {
            "task": task,
            "finalScriptText": _read_task_script(self.project_root, task),
            "workflow": project_workflow(self.db, task_id),
            "sources": get_task_sources(self.db, task_id),
            "sceneCards": scene_cards,
            "eligibleAssets": eligible_assets,
            "modelBindings": list_task_profile_bindings(self.db, task_id),
            "packagingPlans": [
                get_plan(self.db, item["plan_id"])
                for item in list_plans(self.db, task_id)
            ],
            "materialAuthorization": get_material_authorization(self.db, task_id),
            "reviewCandidate": _legacy_review_candidate(review_candidate),
            "candidateVersions": [
                _public_candidate(item, task_id, current_id)
                for item in candidates
            ],
            "currentCandidate": (
                None if current is None else _public_candidate(current, task_id, current_id)
            ),
            "reworkRequests": [
                _public_rework(item) for item in list_rework_requests(self.db, task_id)
            ],
            "watchSession": (
                None
                if review_candidate is None
                else latest_watch_session(
                    self.db,
                    task_id,
                    review_candidate["qa_snapshot_id"],
                    review_candidate["qa_report_id"],
                )
            ),
            "publishPackages": [
                _public_publish(item) for item in list_publish(self.db, task_id)
            ],
            "jobs": [_public_job(self.db, item) for item in list_jobs(self.db, task_id=task_id)],
        }

    def _with_rough_media(self, task_id: str, items: list[dict]) -> list[dict]:
        projected = []
        for item in items:
            copy = dict(item)
            asset_id = copy.get("asset_id")
            if asset_id:
                copy.update(describe_rough_media(self.db, task_id, str(asset_id)))
            if copy.get("card_id"):
                copy.update(_rough_source_projection(copy))
            projected.append(copy)
        return projected

    def _compose_packaging(self, task: dict, body: dict) -> dict:
        task_id = task["task_id"]
        clone_from = _optional_string(body, "cloneFrom")
        plan_id = _optional_string(body, "planId")
        if clone_from:
            new_id = clone_plan(
                self.db, clone_from, task_id=task_id,
                plan_name=_optional_string(body, "planName"),
            )
            return {"planId": new_id, "status": "draft", "plan": get_plan(self.db, new_id)}
        if plan_id:
            _owned_plan(self.db, task_id, plan_id)
            slot = _required_string(body, "slot")
            if body.get("enabled") is False:
                return disable_optional_slot(self.db, plan_id, slot)
            asset_ref = _required_string(body, "assetRef")
            category = "title_card" if slot == "brand" else slot
            if find_item(category, asset_ref) is None:
                raise ApiError(400, "PACKAGING_MANIFEST_UNKNOWN", "所选包装素材不存在，请重新选择。")
            return replace_item(
                self.db, plan_id, slot, asset_ref,
                asset_file=_optional_string(body, "assetFile"),
                asset_version=_optional_string(body, "assetVersion"),
                notes=_optional_string(body, "notes"),
            )
        plan_name = _optional_string(body, "planName") or "自选组合"
        new_id = create_blank_plan(self.db, task_id, task["task_type"], plan_name)
        return {"planId": new_id, "status": "draft", "plan": get_plan(self.db, new_id)}

    def _enqueue(
        self,
        task_id: str,
        kind: str,
        payload: dict,
        *,
        cost_status: str = "unknown",
        provider_request_id: str | None = None,
    ) -> ApiResult:
        job_id = f"job-{uuid.uuid4().hex}"
        actual_id = enqueue_job(
            self.db,
            job_id=job_id,
            job_type=kind,
            task_id=task_id,
            payload={"action": kind, "task_id": task_id, **payload},
            cost_status=cost_status,
            provider_request_id=provider_request_id,
        )
        public = _public_job(self.db, get_job(self.db, actual_id))
        status = 202 if public["status"] in {"queued", "running"} else 200
        return ApiResult(status, public)

    def _job_action(self, job_id: str, action: str, body: dict) -> ApiResult:
        if get_job(self.db, job_id) is None:
            raise ApiError(404, "NOT_FOUND", "后台任务不存在。")
        if action == "cancel":
            request_cancel(self.db, job_id, reason=body.get("reason") or "使用人取消")
        elif action == "pause":
            request_job_pause(self.db, job_id)
        elif action == "resume":
            resume_job(self.db, job_id)
        else:
            raise ApiError(404, "NOT_FOUND", "未找到该接口。")
        return ApiResult(200, _public_job(self.db, get_job(self.db, job_id)))


def _rough_source_projection(card: dict) -> dict:
    narrative_start = float(card.get("narrative_start") or 0)
    narrative_end = float(card.get("narrative_end") or narrative_start)
    duration = max(0.0, narrative_end - narrative_start)
    segment_start = card.get("segment_start")
    segment_end = card.get("segment_end")
    source_range_start = None if segment_start is None else float(segment_start)
    source_range_end = None if segment_end is None else float(segment_end)

    if card.get("media_kind") == "image":
        resolved_start = resolved_end = 0.0
    elif card.get("mix_form") == "数字人全屏":
        resolved_start, resolved_end = narrative_start, narrative_end
    else:
        raw_start = card.get("enter_tc")
        raw_end = card.get("exit_tc")
        explicit = (
            isinstance(raw_start, (int, float))
            and isinstance(raw_end, (int, float))
            and float(raw_end) > float(raw_start)
        )
        if explicit:
            resolved_start, resolved_end = float(raw_start), float(raw_end)
        else:
            resolved_start = source_range_start or 0.0
            resolved_end = resolved_start + duration
    return {
        "source_range_start": source_range_start,
        "source_range_end": source_range_end,
        "resolved_source_start": resolved_start,
        "resolved_source_end": resolved_end,
    }


def translate_exception(error: BaseException) -> ApiError:
    if isinstance(error, ApiError):
        return error
    if isinstance(error, RoughMediaError):
        return ApiError(error.status, error.code, error.public_message, error.details)
    if isinstance(error, (DigitalTaskPreflightError, AdTaskPreflightError)):
        return ApiError(409, error.code, "创建前预检存在阻断项。", error.preflight)
    if isinstance(error, JobRequestConflict):
        return ApiError(409, error.code, "这次模型请求与已有请求不一致，请刷新任务后重试。")
    if isinstance(error, KeyError):
        return ApiError(404, "NOT_FOUND", "请求的任务或记录不存在。")
    if isinstance(error, ValueError):
        text = str(error)
        prefix, separator, message = text.partition(":")
        code = prefix if separator and re.fullmatch(r"[A-Z][A-Z0-9_]+", prefix) else "INVALID_REQUEST"
        public = message.strip() if code != "INVALID_REQUEST" else text
        return ApiError(400, code, _safe_value_message(public))
    if isinstance(error, RuntimeError):
        text = str(error)
        prefix, separator, message = text.partition(":")
        if separator and re.fullmatch(r"[A-Z][A-Z0-9_]+", prefix):
            public = _safe_value_message(message.strip() or "当前条件尚未满足。")
            return ApiError(409, prefix, public)
        if text.startswith(("任务当前不可", "没有可", "这个任务已经")):
            return ApiError(409, "OPERATION_CONFLICT", "当前状态不能执行该操作，请刷新后查看下一步。")
    diagnostic_id = f"diag-{uuid.uuid4().hex[:16]}"
    return ApiError(
        500,
        "INTERNAL_ERROR",
        "本机工作台暂时无法完成该操作。",
        {"diagnosticId": diagnostic_id},
    )


def _path_parts(path: str) -> list[str]:
    parts = path.strip("/").split("/") if path.strip("/") else []
    if any(not part or part in {".", ".."} or "\\" in part for part in parts):
        raise ApiError(404, "NOT_FOUND", "未找到该接口。")
    return parts


def _query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if values is None:
        return None
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError(f"{key} 查询参数无效")
    return values[0].strip() or None


def _query_integer(query: dict[str, list[str]], key: str, default: int) -> int:
    value = _query_value(query, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{key} 查询参数必须是整数") from error


def _required_string(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "INVALID_REQUEST", f"缺少必填内容：{key}")
    return value.strip()


def _optional_string(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "INVALID_REQUEST", f"{key} 必须是文字")
    return value.strip() or None


def _optional_number(body: dict, key: str) -> float | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, "INVALID_REQUEST", f"{key} 必须是数字")
    return float(value)


def _required_dict(body: dict, key: str) -> dict:
    value = body.get(key)
    if not isinstance(value, dict):
        raise ApiError(400, "INVALID_REQUEST", f"{key} 必须是对象")
    return value


def _string_list(body: dict, key: str) -> list[str]:
    value = body.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ApiError(400, "INVALID_REQUEST", f"{key} 必须是文字列表")
    return [item.strip() for item in value if item.strip()]


def _require_gate(db, task_id: str, gate: str, code: str) -> None:
    if not project_gates(db, task_id).get(gate):
        raise ApiError(409, code, "前置人工确认尚未完成。")


def _require_candidate_binding(
    db,
    task_id: str,
    body: dict,
    *,
    snapshot_id: str | None = None,
    qa_report_id: str | None = None,
) -> dict:
    """Resolve only the current candidate and reject cross-version identities."""
    candidates = list_candidates(db, task_id)
    current = candidates[0] if candidates else None
    supplied_id = _optional_string(body, "candidateId")
    if current is None:
        raise ApiError(409, "CANDIDATE_REQUIRED", "当前任务没有候选版本。")
    if supplied_id is not None:
        supplied = get_candidate(db, task_id, supplied_id)
        if supplied is None or supplied_id != current["candidate_id"]:
            raise ApiError(
                409,
                "CANDIDATE_BINDING_MISMATCH",
                "所选候选版本已不是当前版本，请刷新后重试。",
            )
    if (
        (snapshot_id is not None and current.get("qa_snapshot_id") != snapshot_id)
        or (qa_report_id is not None and current.get("qa_report_id") != qa_report_id)
    ):
        raise ApiError(
            409,
            "CANDIDATE_BINDING_MISMATCH",
            "候选版本、QA 快照与报告不属于同一当前版本。",
        )
    return current


def _safe_qa_issue(value) -> dict:
    item = value if isinstance(value, dict) else {}
    return {
        "severity": str(item.get("severity") or "warning"),
        "check": str(item.get("check") or "unknown"),
        "detail": str(item.get("detail") or ""),
        "ok": item.get("ok") is True,
        "startSeconds": item.get("start_seconds", item.get("startSeconds")),
        "endSeconds": item.get("end_seconds", item.get("endSeconds")),
    }


def _public_candidate(candidate: dict, task_id: str, current_id: str | None) -> dict:
    report = candidate.get("qa_report")
    valid_report = (
        isinstance(report, dict)
        and isinstance(report.get("issues"), list)
        and isinstance(report.get("blocking_count"), int)
        and not isinstance(report.get("blocking_count"), bool)
    )
    if not valid_report:
        qa = {
            "reportId": candidate.get("qa_report_id"),
            "status": "unavailable",
            "revisionNo": 0,
            "blockingCount": 0,
            "warningCount": 0,
            "issues": [],
        }
    else:
        issues = [
            _safe_qa_issue(item)
            for item in report["issues"]
            if isinstance(item, dict)
        ]
        qa = {
            "reportId": candidate.get("qa_report_id"),
            "status": str(report.get("status") or "unavailable"),
            "revisionNo": 1,
            "blockingCount": int(report.get("blocking_count") or 0),
            "warningCount": sum(
                1 for item in issues if item["severity"] != "blocking" and not item["ok"]
            ),
            "issues": issues,
        }
    duration = None
    if valid_report and isinstance(report.get("video_spec"), dict):
        try:
            parsed_duration = float(report["video_spec"].get("duration"))
            if math.isfinite(parsed_duration) and parsed_duration > 0:
                duration = parsed_duration
        except (TypeError, ValueError):
            pass
    candidate_id = candidate["candidate_id"]
    return {
        "candidateId": candidate_id,
        "versionNo": candidate["version_no"],
        "versionLabel": candidate["version_label"],
        "sourceCandidateId": candidate.get("source_candidate_id"),
        "reworkRequestId": candidate.get("rework_request_id"),
        "status": candidate["status"],
        "changeSummary": candidate.get("change_summary"),
        "createdAt": candidate.get("created_at"),
        "mixSnapshotId": candidate.get("mix_snapshot_id"),
        "qaSnapshotId": candidate.get("qa_snapshot_id"),
        "qaReportId": candidate.get("qa_report_id"),
        "mediaUrl": (
            f"/api/tasks/{quote(str(task_id), safe='')}/review-media/"
            f"{quote(str(candidate_id), safe='')}"
        ),
        "duration": duration,
        "isCurrent": candidate_id == current_id,
        "readOnly": candidate_id != current_id,
        "qa": qa,
    }


def _legacy_review_candidate(candidate: dict | None) -> dict | None:
    if candidate is None:
        return None
    allowed = (
        "mix_snapshot_id",
        "qa_snapshot_id",
        "qa_report_id",
        "producer",
        "candidate_id",
        "version_no",
        "version_label",
        "status",
        "source_candidate_id",
        "rework_request_id",
        "change_summary",
    )
    return {key: candidate.get(key) for key in allowed}


def _public_rework_impact(impact: dict) -> dict:
    return {
        "category": impact.get("category"),
        "technicalScope": impact.get("technical_scope"),
        "targetStage": impact.get("target_stage"),
        "preservedStages": list(impact.get("preserved_stages") or []),
        "invalidatedStages": list(impact.get("invalidated_stages") or []),
        "rerunStages": list(impact.get("rerun_stages") or []),
        "costConfirmationRequired": bool(impact.get("cost_confirmation_required")),
    }


def _public_rework(value: dict) -> dict:
    return {
        "reworkId": value.get("rework_id"),
        "taskId": value.get("task_id"),
        "sourceCandidateId": value.get("source_candidate_id"),
        "resultCandidateId": value.get("result_candidate_id"),
        "category": value.get("category"),
        "notes": value.get("notes"),
        "positionSeconds": value.get("position_seconds"),
        "qaIssueRef": value.get("qa_issue_ref"),
        "targetStage": value.get("target_stage"),
        "costStatus": value.get("cost_status"),
        "status": value.get("status"),
        "createdAt": value.get("created_at"),
        "updatedAt": value.get("updated_at"),
        "impact": _public_rework_impact(value.get("impact") or {}),
    }


def _public_review(value: dict) -> dict:
    return {
        "approvalId": value.get("approval_id"),
        "decision": value.get("decision"),
        "reviewedBy": value.get("reviewed_by"),
        "notes": value.get("notes"),
        "createdAt": value.get("created_at"),
    }


def _public_publish(value: dict) -> dict:
    """Add the camelCase API contract while retaining legacy UI aliases."""
    result = dict(value)
    result.update({
        "recordId": value.get("record_id"),
        "candidateId": value.get("candidate_id"),
        "candidateLabel": value.get("candidate_label"),
        "packageStatus": value.get("package_status"),
        "isCurrent": bool(value.get("is_current")),
        "packageComplete": bool(value.get("package_complete")),
        "coverPath": value.get("cover_path"),
        "coverChoice": value.get("cover_choice"),
        "deliveryType": value.get("delivery_type"),
        "createdAt": value.get("created_at"),
    })
    return result


def _owned_plan(db, task_id: str, plan_id: str) -> dict:
    plan = get_plan(db, plan_id)
    if plan is None or plan["task_id"] != task_id:
        raise ApiError(404, "NOT_FOUND", "包装方案不存在。")
    return plan


def _public_job(db, job: dict | None) -> dict:
    if job is None:
        raise ApiError(404, "NOT_FOUND", "后台任务不存在。")
    status = job.get("public_status") or job["status"]
    paused = status == "paused"
    can_resume = (
        (paused or (
            status in {"failed", "interrupted"}
            and job.get("retry_policy") != "never"
        ))
        and job.get("outcome_status") != "unknown"
    )
    manual_check = (
        job.get("outcome_status") == "unknown"
        or (status in {"failed", "interrupted"} and not can_resume)
    )
    timing = job_timing(db, job)
    return {
        "jobId": job["job_id"],
        "kind": job["job_type"],
        "taskId": job["task_id"],
        "libraryId": job.get("library_id"),
        "status": status,
        "progress": {"current": job["progress_current"], "total": job["progress_total"]},
        "phase": job["phase_label"],
        "queuePosition": job.get("queue_position"),
        "canPause": status in {"queued", "running"} and not job.get("pause_requested"),
        "pauseRequested": bool(job.get("pause_requested")),
        "canCancel": status in {"queued", "running", "paused", "interrupted"},
        "canResume": can_resume,
        "needsManualCheck": manual_check,
        "problem": "任务未完成，请按下一步处理。" if status in {"failed", "interrupted"} else None,
        "nextAction": "manual_check" if manual_check else ("resume" if can_resume else None),
        "estimatedStartAt": timing["estimated_start_at"],
        "estimatedRemainingSeconds": timing["estimated_remaining_seconds"],
        "estimateStatus": timing["estimate_status"],
        "observedAt": timing["observed_at"],
        "updatedAt": job["updated_at"],
    }


def _public_packaging_preview(
    preview: dict | None,
    task_id: str,
    plan_id: str | None = None,
) -> dict:
    if preview is None:
        return {
            "previewId": None,
            "planId": plan_id,
            "status": "not_requested",
            "mediaUrl": None,
            "coverage": [],
        }
    preview_id = preview["preview_id"]
    status = preview["status"]
    return {
        "previewId": preview_id,
        "planId": preview.get("plan_id") or plan_id,
        "status": status,
        "profile": preview.get("preview_profile"),
        "planDigest": preview.get("plan_digest"),
        "pictureLockDigest": preview.get("picture_lock_digest"),
        "viewedAt": preview.get("viewed_at"),
        "coverage": list(preview.get("coverage") or []),
        "errorCode": preview.get("error_code"),
        "mediaUrl": (
            f"/api/tasks/{task_id}/packaging-previews/{preview_id}/media"
            if status == "ready" else None
        ),
    }


def _public_preview_request(
    db,
    result: dict,
    task_id: str,
    artifact: dict | None,
) -> dict:
    preview = _public_packaging_preview(
        artifact,
        task_id,
        result.get("planId"),
    )
    job = result.get("job")
    return {
        **preview,
        "jobId": result.get("jobId"),
        "job": None if job is None else _public_job(db, job),
    }


def _public_catalog_check(db, value: dict | None) -> dict:
    if value is None:
        return {
            "checkId": None,
            "status": "not_checked",
            "counts": {},
            "items": [],
            "jobId": None,
            "job": None,
        }
    job = value.get("job")
    return {
        "checkId": value.get("checkId"),
        "scopeDigest": value.get("scopeDigest"),
        "status": value.get("status"),
        "counts": dict(value.get("counts") or {}),
        "items": [
            {
                key: item.get(key)
                for key in (
                    "layer", "category", "assetRef", "relativePath",
                    "status", "reasonCode", "selectable", "sizeBytes",
                    "mtimeNs", "quickFingerprint",
                )
                if key in item
            }
            for item in value.get("items") or []
        ],
        "errorCode": value.get("errorCode"),
        "completedAt": value.get("completedAt"),
        "jobId": value.get("jobId"),
        "job": None if job is None else _public_job(db, job),
    }


def _public_library(item: dict) -> dict:
    return {
        "libraryId": item["library_id"],
        "name": item["name"],
        "kind": item["kind"],
        "mountPath": item["mount_path"],
        "online": bool(item["online"]),
        "purpose": item["purpose"],
        "isDefaultGeneric": bool(item["is_default_generic"]),
        "assetCount": int(item.get("asset_count") or 0),
        "lastSyncAt": item.get("last_sync_at"),
        "updatedAt": item["updated_at"],
    }


_PACKAGING_CATEGORIES = (
    ("subtitle", "字幕", "subtitle"),
    ("voice", "音色", "voice"),
    ("bgm", "BGM", "bgm"),
    ("sfx", "音效", "sfx"),
    ("title_card", "文字卡", "title_card"),
    ("sticker", "贴纸", "sticker"),
    ("fancy_text", "花字", "fancy_text"),
    ("brand", "Logo / 水印", "title_card"),
)

_PACKAGING_DIRECTORY_NAMES = {
    "subtitle": ("fonts", "subtitle-presets"),
    "voice": ("voice-profiles", "voice-profiles"),
    "bgm": ("bgm-core", "bgm"),
    "sfx": ("sfx", "sfx"),
    "title_card": ("visual-cards", "visual-cards"),
    "sticker": ("stickers", "stickers"),
    "fancy_text": ("fancy-text-presets", "fancy-text-presets"),
    "brand": ("brand", "brand"),
}


def _packaging_source_layer(
    path: Path | None,
    project_root: Path | None = None,
    personal_root: Path | None = None,
) -> str:
    if path is None:
        return "core"
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    local_root = Path(
        personal_root or default_personal_packaging_root(root)
    ).resolve(strict=False)
    resolved = path.resolve()
    for layer, layer_root in (
        ("extension", (root / "asset-packs").resolve()),
        ("local", local_root),
    ):
        if resolved == layer_root or layer_root in resolved.parents:
            return layer
    return "core"


def _packaging_catalog(db=None, project_root: Path | None = None) -> list[dict]:
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    local_root = (
        default_personal_packaging_root(root)
        if db is None
        else personal_packaging_root(db, root)
    )
    result = []
    for key, label, manifest_key in _PACKAGING_CATEGORIES:
        manifest = load_manifest(manifest_key)
        assets = []
        for item in manifest.get("items", []):
            if key == "brand" and not _looks_like_brand(item):
                continue
            if key == "title_card" and _looks_like_brand(item):
                continue
            license_name = item.get("license")
            path = resolve_manifest_asset_path(manifest_key, str(item.get("id") or ""))
            suffix = "" if path is None else path.suffix.lower()
            preview_kind = (
                "audio" if suffix in {".mp3", ".wav", ".ogg", ".m4a", ".aac"}
                else "image" if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                else "video" if suffix in {".mp4", ".mov", ".webm"}
                else None
            )
            availability = (
                "external" if path is None
                else "available" if path.is_file()
                else "missing"
            )
            asset_ref = str(item.get("id") or "")
            source_layer = _packaging_source_layer(path, root, local_root)
            relative_path = None
            if path is not None:
                layer_root = (
                    local_root if source_layer == "local"
                    else root / "asset-packs" if source_layer == "extension"
                    else root
                ).resolve(strict=False)
                try:
                    relative_path = path.resolve().relative_to(layer_root).as_posix()
                except ValueError:
                    relative_path = path.name
            assets.append({
                "assetRef": asset_ref,
                "name": str(item.get("name") or item.get("id") or "未命名素材"),
                "authorizationStatus": (
                    "approved"
                    if license_name == "commercial-approved"
                    else "review_required"
                ),
                "license": str(license_name or "未记录"),
                "availabilityStatus": availability,
                "relativePath": relative_path,
                "sourceLayer": source_layer,
                "previewKind": preview_kind,
                "previewUrl": (
                    f"/api/packaging-assets/{key}/{asset_ref}/preview"
                    if availability == "available" and preview_kind else None
                ),
            })
        result.append({
            "key": key,
            "label": label,
            "count": len(assets),
            "assets": assets,
            "directories": _packaging_directories(key, root, local_root),
        })
    return result


def _packaging_directories(
    category: str,
    project_root: Path | None = None,
    personal_root: Path | None = None,
) -> list[dict]:
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    local_root = Path(
        personal_root or default_personal_packaging_root(root)
    ).resolve(strict=False)
    core_name, layer_name = _PACKAGING_DIRECTORY_NAMES[category]
    paths = (
        ("core", "核心素材", root / "assets" / core_name, f"assets/{core_name}"),
        ("extension", "团队扩展包", root / "asset-packs" / layer_name, f"asset-packs/{layer_name}"),
        ("local", "个人本地素材", local_root / layer_name / "inbox", f"个人素材/{layer_name}/inbox"),
    )
    return [
        {
            "layer": layer,
            "label": label,
            "path": relative,
            "exists": path.is_dir(),
        }
        for layer, label, path, relative in paths
    ]


def _looks_like_brand(item: dict) -> bool:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("id", "name", "kind", "type", "note")
    ).lower()
    return any(word in text for word in ("logo", "watermark", "水印", "品牌"))


def _first_environment(environment: dict[str, str], keys) -> str | None:
    for key in keys:
        value = environment.get(key)
        if value:
            return value
    return None


def _safe_value_message(message: str) -> str:
    if re.search(r"(?:[A-Za-z]:[\\/]|/[^ ]+/)", message):
        return "请求内容无效，请检查后重试。"
    return message[:240] or "请求内容无效。"
