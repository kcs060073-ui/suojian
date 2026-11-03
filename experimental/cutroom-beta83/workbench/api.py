"""Gate-safe business API composed exclusively from media_catalog domains."""

from __future__ import annotations

from media_catalog.performance import observe_api

from media_catalog.visual_probe_receipts import (
    begin_visual_probe, finish_visual_probe, latest_visual_probe,
)

from dataclasses import dataclass
import math
from pathlib import Path
import re
from urllib.parse import quote
import uuid

from media_catalog.packaging_layout import inspect_packaging_layout


def _packaging_plan_views(db, task_id):
    from media_catalog.packaging_plan import manifest_read_snapshot
    from media_catalog.packaging_layout import layout_read_snapshot
    with manifest_read_snapshot(), layout_read_snapshot():
        return [_packaging_plan_view(db, item["plan_id"])
                for item in list_plans(db, task_id)]


def _packaging_plan_view(db, plan_id: str) -> dict:
    plan = get_plan(db, plan_id)
    if plan is not None:
        from media_catalog.packaging_preview import packaging_plan_confirmation_digest
        plan = {**plan, "layout": inspect_packaging_layout(db, plan),
                "confirmationDigest": packaging_plan_confirmation_digest(db, plan)}
    return plan


from media_catalog.ad_workflow import (
    AdTaskPreflightError,
    create_ad_task,
    preflight_ad_task,
    require_ad_picture_assets,
)
from media_catalog.asset_index import get_asset_detail, list_asset_index, with_technical_durations
from media_catalog.catalog_exchange import apply_catalog_import
from media_catalog.segment_marks import save_segment_mark
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
    voice_preview_status,
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
from media_catalog.delivery_copy import (
    DeliveryCopyConflict, get_delivery_copy_draft, save_delivery_copy_draft,
)
from media_catalog.delivery_copy_jobs import (
    latest_delivery_copy_suggestion, queue_delivery_copy_suggestion, delivery_copy_availability,
)
from media_catalog.delivery_copy_suggestions import accept_delivery_copy_suggestion
from media_catalog.packaging_suggestion_jobs import queue_packaging_suggestion, packaging_suggestion_state, packaging_suggestion_availability
from media_catalog.packaging_audio_jobs import queue_packaging_audio, packaging_audio_state
from media_catalog.task_create_idempotency import CreateRequestConflict, create_once
from media_catalog.content_review import (
    acknowledge_content_review_takeover, get_content_review_issue_decisions,
    get_content_review_takeover, list_content_reviews, retain_content_review_issue, retain_content_review_issues,
)
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
    update_plan_settings,
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
from media_catalog.publish import list_publish, resolve_delivery_root
from media_catalog.reviews import reject_review
from media_catalog.review_issues import (
    add_review_issue, list_review_issues, withdraw_review_issue,
)
from media_catalog.rework import (
    approve_rework_cost,
    execute_rework,
    get_rework_request,
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
from media_catalog.scene_cards import list_scene_cards, picture_view_digest, set_picture_lock
from media_catalog.script_segments import project_script_shots, storyboard_digest
from media_catalog.timeline_contract import seconds_to_us
from media_catalog.visual_observations import visual_observation_for_shot
from media_catalog.visual_batch_preview import preview_visual_batch
from media_catalog.visual_batch_execution import queue_visual_batch, visual_batch_status
from media_catalog.scene_window_jobs import (
    preview_scene_windows, queue_scene_window_job, scene_window_job_status,
)
from media_catalog.packaging_instances import (
    add_instance as add_packaging_instance,
    update_instance as update_packaging_instance,
    delete_instance as delete_packaging_instance,
)
from media_catalog.ad_voice_preview import current_ad_voice_source, measured_voice_duration
from media_catalog.digital_render_profile import current_digital_duration
from media_catalog.script_rework import (
    commit_script_rework, pre_candidate_rework_available, preview_script_rework,
)
from media_catalog.shot_candidates import list_shot_candidates
from media_catalog.storyboard_motion import (
    accept_motion_advisory, project_motion_advisories,
)
from media_catalog.storyboard_review import accept_storyboard
from media_catalog.storyboard_edits import (
    apply_storyboard_edit, can_undo_storyboard, undo_storyboard_edit,
)
from media_catalog.storyboard_feedback import (
    apply_feedback_candidate,
    record_feedback,
    rerank_scene_candidates,
)
from media_catalog.job_timing import job_timing
from media_catalog.snapshots import latest_review_candidate
from media_catalog.task_sources import (
    get_task_sources,
    initial_source_check_pending,
    list_eligible_assets,
    source_readiness,
    task_has_source_configuration,
)
from media_catalog.initial_source_check import get_initial_source_check, list_initial_source_check_statuses
from media_catalog.task_script import require_transcription_inputs, transcription_dispatch, ScriptDraftError, confirm_script, read_task_script as _read_task_script, revise_transcribed_script, save_script_draft
from media_catalog.digital_source_revision import preview_source_replacement, replace_digital_source
from media_catalog.digital_source_rework import preview_source_rework, commit_source_rework
from media_catalog.transcript_reconciliation import (
    confirm_transcript_review, project_transcript_state,
)
from media_catalog.tasks import get_task, list_tasks, set_status
from media_catalog.workbench_worker import (
    can_resume_job,
    JobRequestConflict,
    enqueue_job,
    get_job,
    list_jobs,
    matching_job_request,
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


@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str
    details: dict | None = None



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

    @observe_api
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
                    purpose=_query_value(query, "purpose"),
                    media_kind=_query_value(query, "mediaKind"),
                    availability=_query_value(query, "availability"),
                    authorization=_query_value(query, "authorization"),
                    analysis_status=_query_value(query, "analysisStatus"),
                    limit=_query_integer(query, "limit", 100),
                    offset=_query_integer(query, "offset", 0),
                ))
            except ValueError as error:
                raise ApiError(
                    400,
                    "ASSET_INDEX_QUERY_INVALID",
                    "素材索引筛选条件无效，请检查后重试。",
                ) from error
        if method == "POST" and parts == ["api", "catalog-exchange", "apply"]:
            try:
                return ApiResult(200, apply_catalog_import(
                    self.db, _required_string(body, "previewId"),
                    _required_string(body, "requestKey"),
                ))
            except ValueError as error:
                code, _, message = str(error).partition(":")
                status = 409 if code in {"IMPORT_RECHECK_FAILED", "REQUEST_REPLAY_CONFLICT"} else 400
                raise ApiError(status, code, message.strip() or "导入应用失败。") from error
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "assets"] and parts[3] == "detail":
            try:
                return ApiResult(200, get_asset_detail(
                    self.db, parts[2], task_id=_query_value(query, "taskId"),
                ))
            except KeyError as error:
                raise ApiError(404, "ASSET_NOT_IN_SCOPE", "素材不在当前任务范围。") from error
            except ValueError as error:
                raise ApiError(400, "ASSET_INDEX_QUERY_INVALID", "素材详情参数无效。") from error
        if (method == "PUT" and len(parts) == 5 and
                parts[:2] == ["api", "assets"] and parts[3] == "segments"):
            asset_id, segment_id = parts[2], parts[4]
            try:
                # The same frozen task scope that granted detail access grants edits.
                detail = get_asset_detail(self.db, asset_id, task_id=body.get("taskId"))
                if not any(item["segmentId"] == segment_id for item in detail["segments"]):
                    raise ValueError("SEGMENT_INVALID: 当前范围没有这个片段")
                return ApiResult(200, save_segment_mark(
                    self.db, asset_id, segment_id,
                    expected_revision=body.get("expectedRevision"),
                    expected_fingerprint=body.get("expectedFingerprint"),
                    source_start_us=body.get("sourceStartUs"),
                    source_end_us=body.get("sourceEndUs"),
                    title=body.get("title"), description=body.get("description"),
                    camera_motion=body.get("cameraMotion"),
                    motion_kind=body.get("motionKind"),
                    subject_motion=body.get("subjectMotion"),
                    usage_status=body.get("usageStatus"), note=body.get("note", ""),
                ))
            except KeyError as error:
                raise ApiError(404, "ASSET_NOT_IN_SCOPE", "素材不在当前任务范围。") from error
            except ValueError as error:
                code = str(error).split(":", 1)[0]
                if code in {"ASSET_STALE", "REVISION_CONFLICT"}:
                    raise ApiError(409, code, str(error).split(":", 1)[-1].strip()) from error
                raise ApiError(400, code, str(error).split(":", 1)[-1].strip()) from error
        if (method == "GET" and len(parts) == 5 and
                parts[:2] == ["api", "tasks"] and
                parts[3:] == ["storyboard", "candidates"]):
            try:
                return ApiResult(200, list_shot_candidates(
                    self.db, parts[2],
                    limit=_query_integer(query, "limit", 50),
                    offset=_query_integer(query, "offset", 0),
                    query=(query or {}).get("q", [""])[0],
                ))
            except ValueError as error:
                raise ApiError(400, "STORYBOARD_CANDIDATE_QUERY_INVALID",
                               "候选素材分页条件无效。") from error
        if parts == ["api", "libraries"]:
            if method == "GET":
                return ApiResult(200, {"libraries": self._libraries()})
            if method == "POST":
                return ApiResult(200, self._register_library(body))
        if (method == "POST" and len(parts) == 5 and
                parts[:2] == ["api", "libraries"] and
                parts[3] == "scene-windows"):
            library_id = parts[2]
            if library_id not in {item["library_id"] for item in list_libraries(self.db)}:
                raise ApiError(404, "NOT_FOUND", "素材目录不存在。")
            try:
                if parts[4] == "preview":
                    return ApiResult(200, preview_scene_windows(
                        self.db, None, library_id, body.get("assets"),
                        offset=body.get("offset", 0), limit=body.get("limit", 50),
                    ))
                if parts[4] == "start":
                    queued = queue_scene_window_job(
                        self.db, None, library_id, body.get("assets"),
                        _required_string(body, "requestId"),
                    )
                    return ApiResult(202, {"job": _public_job(
                        self.db, get_job(self.db, queued["jobId"])), **queued})
            except ValueError as error:
                code, _, detail = str(error).partition(":")
                raise ApiError(409 if code in {"ASSET_OFFLINE", "ASSET_STALE",
                                               "JOB_IDEMPOTENCY_CONFLICT"} else 400,
                               code, detail.strip() or "场景候选请求无效") from error
        if (method == "POST" and len(parts) == 5 and
                parts[:2] == ["api", "libraries"] and
                parts[3] == "visual-analysis"):
            library_id = parts[2]
            if library_id not in {item["library_id"] for item in list_libraries(self.db)}:
                raise ApiError(404, "NOT_FOUND", "素材目录不存在。")
            try:
                if parts[4] == "preview":
                    return ApiResult(200, preview_visual_batch(
                        self.db, None, _required_string(body, "profileId"),
                        body.get("items"), library_id=library_id,
                    ))
                if parts[4] == "start":
                    queued = queue_visual_batch(
                        self.db, None, _required_string(body, "profileId"),
                        body.get("items"), _required_string(body, "previewDigest"),
                        _required_string(body, "requestId"), body.get("confirmPaidCall"),
                        library_id=library_id,
                    )
                    return ApiResult(202, {"job": _public_job(
                        self.db, get_job(self.db, queued["jobId"])), **queued})
            except ValueError as error:
                code, _, detail = str(error).partition(":")
                status = 409 if code in {
                    "PAID_CONFIRMATION_REQUIRED", "VISUAL_PREVIEW_STALE",
                    "VISUAL_BATCH_BLOCKED", "VISUAL_BATCH_ALREADY_ACTIVE",
                    "JOB_IDEMPOTENCY_CONFLICT",
                    "VISUAL_PROFILE_UNVERIFIED", "ASSET_OFFLINE", "ASSET_STALE",
                } else 400
                raise ApiError(status, code, detail.strip() or "素材分析请求无效") from error
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
        if parts[:2] == ["api", "bgm-library"]:
            from media_catalog import local_bgm
            if len(parts) == 2 and method == "GET":
                return ApiResult(200, local_bgm.catalog(self.db))
            if len(parts) == 2 and method == "POST":
                return ApiResult(202, local_bgm.register_folder(self.db, _required_string(body, "path")))
            if len(parts) == 4 and parts[2] == "tracks" and method == "PUT":
                return ApiResult(200, local_bgm.update_track(self.db, parts[3], body))
            if len(parts) == 4 and parts[3] == "scan" and method == "POST":
                return ApiResult(202, local_bgm.request_scan(self.db, parts[2]))
            if len(parts) == 4 and parts[3] == "mount" and method == "PUT":
                return ApiResult(202, local_bgm.register_folder(self.db, _required_string(body, "path"), parts[2]))
            if len(parts) == 3 and method == "PUT":
                return ApiResult(200, local_bgm.set_enabled(self.db, parts[2], body.get("enabled")))
        if parts == ["api", "packaging-catalog", "resources"] and method in {"POST", "PATCH"}:
            from media_catalog.packaging_library import create_resource, rename_resource
            try:
                result = create_resource(self.db, self.project_root, body) if method == "POST" else rename_resource(self.db, body)
            except (ValueError, OSError) as error:
                raise ApiError(400, "PACKAGING_RESOURCE_INVALID", str(error)) from error
            return ApiResult(201 if method == "POST" else 200, result)
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
            if parts[4] == "test-status" and method == "GET":
                if get_model_profile(self.db, parts[3]) is None:
                    raise ApiError(404, "NOT_FOUND", "模型配置不存在。")
                return ApiResult(200, latest_visual_probe(self.db, parts[3]))
            if parts[4] == "test" and method == "POST":
                return ApiResult(200, self._test_model_profile(parts[3]))
            if parts[4] == "default" and method == "POST":
                return ApiResult(200, set_default_profile(self.db, parts[3]))
            if parts[4] == "voice-preview-status" and method == "POST":
                return ApiResult(200, self._test_profile_voice(parts[3], body, status_only=True))
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
                if "requestId" not in body:
                    return ApiResult(200, creator(self.db, body, self.project_root))
                from media_catalog.task_create_idempotency import recover_task_creations
                try:
                    created = create_once(
                        self.db, body,
                        lambda input_body, creation_db: creator(creation_db, input_body, self.project_root),
                        recover=lambda creation_db: recover_task_creations(creation_db, self.project_root),
                    )
                except CreateRequestConflict as error:
                    raise ApiError(409, error.code, str(error)) from error
                return ApiResult(200, created)
        if parts == ["api", "jobs"] and method == "GET":
            return ApiResult(200, {"jobs": [_public_job(self.db, item) for item in list_jobs(self.db)]})
        if method == "POST" and parts == ["api", "tasks", "preflight"]:
            preflight = _active_route_adapter(
                body.get("route"), preflight_digital_task, preflight_ad_task
            )
            return ApiResult(200, preflight(self.db, body, self.project_root))
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "rough-assets":
            from media_catalog.rough_media import rough_asset_page
            try:
                return ApiResult(200, rough_asset_page(self.db, parts[2],
                    limit=_query_integer(query, "limit", 24), offset=_query_integer(query, "offset", 0),
                    query=(query or {}).get("q", [""])[0]))
            except ValueError as error:
                raise ApiError(400, "ROUGH_ASSET_QUERY_INVALID", "候选素材分页或搜索条件无效。") from error
        if len(parts) >= 3 and parts[:2] == ["api", "tasks"]:
            return self._task_route(method, parts[2], parts[3:], body, query=query)
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
            if len(parts) == 3 and method == "GET":
                return ApiResult(200, _public_job(self.db, get_job(self.db, parts[2])))
            if len(parts) == 4 and method == "GET" and parts[3] == "visual-items":
                return ApiResult(200, visual_batch_status(self.db, parts[2]))
            if len(parts) == 4 and method == "GET" and parts[3] == "scene-items":
                try:
                    return ApiResult(200, scene_window_job_status(self.db, parts[2]))
                except KeyError as error:
                    raise ApiError(404, "NOT_FOUND", "场景检测作业不存在。") from error
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
        if capability == "visual_analysis" and (
            account.get("provider") not in {"openai_compatible", "volcengine_ark", "new_api"}
            or protocol not in {"openai", "openai_compatible", "openai_chat"}
        ):
            raise ApiError(400, "VISUAL_PROVIDER_UNSUPPORTED",
                           "素材画面分析需要可探测的多模态 Chat 服务。")
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
            if capability == "visual_analysis" and "visualProtocolMode" in body:
                from media_catalog.visual_protocol import visual_protocol_mode
                try:
                    settings["visual_protocol_mode"] = visual_protocol_mode({
                        "visual_protocol_mode": body["visualProtocolMode"],
                    })
                except ValueError as error:
                    raise ApiError(400, "VISUAL_PROTOCOL_MODE_INVALID",
                                   "请选择明确支持的视觉协议模式。") from error
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

    def _test_profile_voice(self, profile_id: str, body: dict, *, status_only: bool = False) -> dict:
        profile, resource_id = self._profile_voice_scope(profile_id)
        voice_id = _required_string(body, "voiceId")
        self._selectable_voice(profile["accountId"], resource_id, voice_id)
        try:
            if status_only:
                return voice_preview_status(self.db, profile_id, voice_id, self.project_root)
            return create_tts_voice_preview(
                self.db, profile_id, voice_id, self.project_root,
                retry_request_id=body.get("retryRequestId"),
                provider_checked=body.get("providerChecked", False),
                confirm_paid_retry=body.get("confirmPaidRetry", False),
            )
        except ModelProfileProbeError as error:
            raise ApiError(422, error.code, str(error),
                           {"voicePreview": error.receipt} if error.receipt else None) from error

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
            from media_catalog.model_profiles import _profile_row
            from media_catalog.visual_profile_probe import visual_probe_request_binding
            row = _profile_row(self.db, profile_id)
            binding = (visual_probe_request_binding(row)
                       if row is not None and row["capability"] == "visual_analysis" else None)
            operation_id = begin_visual_probe(self.db, profile_id, request_binding=binding)
        except ValueError as error:
            code = "VISUAL_PROTOCOL_MODE_INVALID" if str(error).startswith("VISUAL_PROTOCOL_MODE_INVALID:") else "MODEL_CONFIGURATION_INVALID"
            raise ApiError(400, code, "视觉协议配置无效，请先更正配置。") from error
        try:
            result = probe_model_profile(self.db, profile_id)
        except ModelProfileProbeError as error:
            operation = (finish_visual_probe(self.db, operation_id, status="failed",
                         receipt=error.receipt, error_code=error.code) if operation_id else None)
            profile = get_model_profile(self.db, profile_id)
            details = {"visualProbeOperation": operation} if operation else {}
            if error.code in {"VISUAL_PROBE_BINDING_INVALID", "VISUAL_PROBE_POLICY_INVALID"}:
                raise ApiError(400, error.code, "本次视觉检测的请求范围与已核对的执行条件不一致。", details or None) from error
            if operation and operation["status"] == "stale":
                raise ApiError(409, "VISUAL_PROFILE_STALE", "检测期间配置已变化，请核对当前配置。", details) from error
            if profile is not None and profile.get("lastErrorCode"):
                details.update({
                    "profileId": profile["profileId"],
                    "availabilityStatus": profile["availabilityStatus"],
                    "lastCheckedAt": profile.get("lastCheckedAt"),
                    "lastErrorMessage": profile.get("lastErrorMessage"),
                })
                raise ApiError(422, profile["lastErrorCode"],
                    profile.get("lastErrorMessage") or str(error), details) from error
            raise ApiError(409, "MODEL_PROFILE_UNAVAILABLE", str(error), details or None) from error
        except Exception:
            if operation_id:
                finish_visual_probe(self.db, operation_id, status="unknown",
                                    error_code="MODEL_PROBE_OUTCOME_UNKNOWN")
            raise
        if operation_id:
            operation = finish_visual_probe(self.db, operation_id, status="succeeded",
                                           receipt=result.get("visualProbeReceipt"))
            if operation["status"] == "stale":
                raise ApiError(409, "VISUAL_PROFILE_STALE", "检测期间配置已变化，请核对当前配置。",
                               {"visualProbeOperation": operation})
            if operation["status"] != "succeeded":
                raise ApiError(409, operation["errorCode"] or "VISUAL_PROBE_RECEIPT_MISMATCH",
                               "未取得与本次协议一致的检测回执。", {"visualProbeOperation": operation})
            result = {**result, "visualProbeReceipt": operation["diagnostic"],
                      "visualProbeOperation": operation}
        return result

    def _delete_model_profile(self, profile_id: str) -> dict:
        try:
            deleted = delete_model_profile(self.db, profile_id)
        except ModelProfileInUse as error:
            raise ApiError(409, "MODEL_PROFILE_IN_USE", str(error)) from error
        if deleted is None:
            raise ApiError(404, "NOT_FOUND", "模型配置不存在。")
        return deleted

    def task_summaries(self) -> list[dict]:
        checks = list_initial_source_check_statuses(self.db)
        check_actions = {
            "queued": ("queued", "wait_source_check"),
            "running": ("running", "wait_source_check"),
            "offline": ("blocked", "reconnect_source"),
            "review_required": ("awaiting_user", "review_source_changes"),
            "failed": ("failed", "retry_source_check"),
        }
        result = []
        for task in list_tasks(self.db):
            workflow = project_workflow(self.db, task["task_id"], verify_delivery_content=False)
            if task["task_type"] == "digital_human" and workflow["current_stage"] in {"material_sync", "ai_storyboard"}:
                from media_catalog.workflow import apply_transcript_attention
                workflow = apply_transcript_attention(
                    workflow, task["task_type"], project_transcript_state(self.project_root, get_task(self.db, task["task_id"]), include_passages=False))
            check = checks.get(task["task_id"])
            attention = (
                check_actions.get(check)
                if workflow["current_stage"] == "material_sync" else None
            )
            run_status, next_action = attention or (
                workflow["run_status"], workflow["next_action"]
            )
            result.append({
                "taskId": task["task_id"],
                "title": task["title"],
                "route": task["task_type"],
                "orchestrationMode": task["orchestration_mode"],
                "updatedAt": task["updated_at"],
                "currentStage": workflow["current_stage"],
                "runStatus": run_status,
                "nextAction": next_action,
                "gates": workflow["gates"],
            })
        return result

    def _task_route(self, method: str, task_id: str, rest: list[str], body: dict,
                    *, query: dict[str, list[str]] | None = None) -> ApiResult:
        task = get_task(self.db, task_id)
        if task is None:
            raise ApiError(404, "NOT_FOUND", "任务不存在。")
        if method == "GET" and not rest:
            return ApiResult(200, self._task_detail(
                task, projection=_query_value(query or {}, "projection")))
        key = tuple(rest)
        if key and key[0]=='voice-editing' and key!=('voice-editing','context'):
            from media_catalog.voice_draft_service import (voice_draft_session,save_voice_draft,
                get_voice_draft,cancel_voice_draft,recover_voice_context,VoiceDraftError)
            from media_catalog.voice_task_context import VoiceTaskContextError
            try:
                if len(key)==1 and method=='GET':
                    return ApiResult(200,voice_draft_session(self.db,self.project_root,task_id))
                if key==('voice-editing','context','recover') and method=='POST':
                    return ApiResult(200,recover_voice_context(self.db,self.project_root,task_id,body))
                if key==('voice-editing','drafts') and method=='POST':
                    return ApiResult(200,save_voice_draft(self.db,self.project_root,task_id,body))
                if len(key)==3 and key[1]=='drafts' and method=='GET':
                    return ApiResult(200,get_voice_draft(self.db,self.project_root,task_id,key[2]))
                if len(key)==4 and key[1]=='drafts' and key[3]=='cancel' and method=='POST':
                    return ApiResult(200,cancel_voice_draft(self.db,self.project_root,task_id,key[2],body))
                from media_catalog.voice_bundle_service import (queue_voice_preview,get_voice_preview,apply_voice_draft,restore_voice_version)
                if len(key)==4 and key[1]=='drafts' and key[3]=='preview':
                    if method=='POST':return ApiResult(202,queue_voice_preview(self.db,self.project_root,task_id,key[2],body))
                    if method=='GET':return ApiResult(200,get_voice_preview(self.db,self.project_root,task_id,key[2]))
                if len(key)==4 and key[1]=='drafts' and key[3]=='recover' and method=='POST':
                    from media_catalog.voice_unknown_recovery import recover_voice_preview
                    return ApiResult(200,recover_voice_preview(self.db,self.project_root,task_id,key[2],body))
                if len(key)==4 and key[1]=='drafts' and key[3]=='apply' and method=='POST':
                    return ApiResult(200,apply_voice_draft(self.db,self.project_root,task_id,key[2],body))
                if key==('voice-editing','versions') and method=='GET':
                    from media_catalog.voice_bundle_service import list_voice_versions
                    return ApiResult(200,list_voice_versions(self.db,self.project_root,task_id))
                if len(key)==4 and key[1]=='versions' and key[2].isdigit() and key[3]=='restore' and method=='POST':
                    return ApiResult(200,restore_voice_version(self.db,self.project_root,task_id,int(key[2]),body))
            except VoiceTaskContextError as error:
                raise ApiError(409,error.code,error.message) from error
            except VoiceDraftError as error:
                raise ApiError(error.status,error.code,error.message) from error
        if key == ('voice-editing', 'context') and method == 'GET':
            from media_catalog.voice_task_context import resolve_voice_task_context, VoiceTaskContextError
            try:
                context = resolve_voice_task_context(self.db, self.project_root, task_id,
                                                     include_voice_catalog=True)
            except VoiceTaskContextError as error:
                raise ApiError(409, error.code, error.message) from error
            return ApiResult(200, context.public_payload())
        if key and key[0] == 'edit-proposals':
            from media_catalog.edit_proposals import (propose_edit, get_edit_proposal,
                list_edit_proposals, apply_edit as apply_proposed_edit, cancel_edit, undo_edit)
            try:
                from media_catalog.edit_intent import (intent_context, queue_edit_intent,
                    get_edit_intent, select_intent_option)
                if key == ('edit-proposals','intent-context') and method == 'GET':
                    return ApiResult(200, intent_context(self.db, task_id))
                if key == ('edit-proposals','intent-jobs'):
                    if method == 'GET':
                        return ApiResult(200, get_edit_intent(self.db, task_id))
                    if method == 'POST':
                        return ApiResult(202, queue_edit_intent(self.db, task_id,
                            expected_revision=_required_string(body,'expectedRevision'),
                            instruction=_required_string(body,'instruction'),
                            target_card_id=_required_string(body,'targetCardId'),
                            plan_id=_optional_string(body,'planId'), add_duration=body.get('addDuration'),
                            confirm_paid_call=body.get('confirmPaidCall')))
                if len(key) == 3 and key[1] == 'intent-jobs' and method == 'GET':
                    return ApiResult(200, get_edit_intent(self.db, task_id, key[2]))
                if len(key) == 4 and key[1] == 'intent-jobs' and key[3] == 'select' and method == 'POST':
                    return ApiResult(200, select_intent_option(self.db, task_id, key[2],
                        _required_string(body,'optionId'), _required_string(body,'requestId'),
                        _required_string(body,'expectedRevision')))
                if method == 'GET' and len(key) == 1:
                    return ApiResult(200, {'proposals': list_edit_proposals(self.db, task_id)})
                if method == 'GET' and len(key) == 2:
                    return ApiResult(200, get_edit_proposal(self.db, task_id, key[1]))
                if method == 'POST' and len(key) == 1:
                    return ApiResult(200, propose_edit(self.db, task_id, _required_string(body, 'requestId'),
                        _required_string(body, 'expectedRevision'), _required_string(body, 'instruction'), plan_id=body.get('planId')))
                if method == 'POST' and len(key) == 3 and key[2] in {'apply','cancel','undo'}:
                    request_id = _required_string(body, 'requestId')
                    if key[2] == 'cancel':
                        return ApiResult(200, cancel_edit(self.db, task_id, key[1], request_id))
                    if body.get('confirm') is not True:
                        raise ApiError(400, 'PROPOSAL_CONFIRM_REQUIRED', '请先查看差异并明确确认此操作。')
                    operation = apply_proposed_edit if key[2] == 'apply' else undo_edit
                    return ApiResult(200, operation(self.db, task_id, key[1], request_id, _required_string(body, 'expectedRevision')))
            except KeyError as error:
                raise ApiError(404, 'PROPOSAL_NOT_FOUND', '当前任务没有这份草稿或方案。') from error
            except (RuntimeError,ValueError) as error:
                code, _, message = str(error).partition(':')
                conflict = code.startswith('PROPOSAL_') and code not in {'PROPOSAL_INPUT_INVALID','PROPOSAL_OPERATION_INVALID'}
                raise ApiError(409 if conflict else 400, code, message.strip() or str(error)) from error
        if key in {('creative-advice',), ('creative-advice','prepare'), ('creative-advice','locate')}:
            from media_catalog.creative_visual_advice import (queue_creative_advice, get_creative_advice,
                prepare_creative_advice, locate_creative_advice)
            try:
                if method == 'GET' and key == ('creative-advice',):
                    return ApiResult(200, get_creative_advice(self.db, task_id, project_root=self.project_root))
                if method == 'GET' and key == ('creative-advice','prepare'):
                    return ApiResult(200, prepare_creative_advice(self.db, task_id, project_root=self.project_root))
                if method == 'POST' and key == ('creative-advice',):
                    return ApiResult(202, queue_creative_advice(self.db, task_id,
                        expected_revision=_required_string(body,'expectedRevision'),
                        profile_id=_required_string(body,'profileId'), confirm_paid_call=body.get('confirmPaidCall'),
                        retry_of_job_id=_optional_string(body,'retryOfJobId'), project_root=self.project_root))
                if method == 'POST' and key == ('creative-advice','locate'):
                    return ApiResult(200, locate_creative_advice(self.db, task_id,
                        (_optional_string(body,'jobId') or _required_string(body,'evidenceRef')), _required_string(body,'cardId'), project_root=self.project_root))
            except KeyError as error:
                raise ApiError(404, 'ADVICE_NOT_FOUND', '当前任务没有这份建议或镜头。') from error
            except (RuntimeError,ValueError) as error:
                code, _, message = str(error).partition(':')
                raise ApiError(409 if code in {'ADVICE_STALE','RHYTHM_REVISION_CHANGED'} else 400,
                    code, message.strip() or str(error)) from error
        if key in {('rhythm-advice',), ('rhythm-advice', 'preview'), ('rhythm-advice', 'locate')}:
            from media_catalog.rhythm_advice import build_rhythm_advice, get_rhythm_advice, preview_rhythm_edit, locate_rhythm_suggestion
            try:
                if method == 'GET' and key == ('rhythm-advice',):
                    return ApiResult(200, get_rhythm_advice(self.db, task_id))
                if method == 'POST' and key == ('rhythm-advice',):
                    return ApiResult(200, build_rhythm_advice(self.db, task_id, _required_string(body, 'expectedRevision')))
                if method == 'POST' and len(key) == 2:
                    operation = preview_rhythm_edit if key[1] == 'preview' else locate_rhythm_suggestion
                    return ApiResult(200, operation(self.db, task_id, _required_string(body, 'reportId'),
                        _required_string(body, 'suggestionId'), _required_string(body, 'expectedRevision')))
            except KeyError as error:
                raise ApiError(404, 'RHYTHM_NOT_FOUND', '当前任务没有这份建议或条目。') from error
            except (RuntimeError, ValueError) as error:
                code, _, message = str(error).partition(':')
                raise ApiError(409 if code in {'RHYTHM_REVISION_CHANGED', 'RHYTHM_INPUTS_CHANGED'} else 400,
                    code, message.strip()) from error
        if method == 'POST' and key == ('reshoot-list', 'revise'):
            from media_catalog.reshoot_revision import revise_reshoot_need
            try:
                return ApiResult(200, revise_reshoot_need(self.db, task_id,
                    expected_revision=_required_string(body, 'expectedRevision'),
                    report_id=_required_string(body, 'reportId'), gap_id=_required_string(body, 'gapId'),
                    query=_required_string(body, 'query'), reason=_required_string(body, 'reason'),
                    project_root=self.project_root))
            except KeyError as error:
                raise ApiError(404, 'RESHOOT_REVISION_NOT_FOUND', '当前任务没有这份清单或需求。') from error
            except (RuntimeError, ValueError) as error:
                code, _, message = str(error).partition(':')
                raise ApiError(409 if isinstance(error, RuntimeError) else 400, code, message.strip()) from error
        if method == 'POST' and key == ('reshoot-list', 'adopt-advice'):
            from media_catalog.creative_advice_adoption import adopt_reshoot_advice
            try:
                return ApiResult(200, adopt_reshoot_advice(self.db,task_id,
                    expected_revision=_required_string(body,'expectedRevision'),card_id=_required_string(body,'cardId'),
                    item_index=body.get('itemIndex'),query=_required_string(body,'query'),
                    job_id=_optional_string(body,'jobId'),evidence_ref=_optional_string(body,'evidenceRef'),project_root=self.project_root))
            except KeyError as error:
                raise ApiError(404,'RESHOOT_ADVICE_NOT_FOUND','当前任务没有该建议。') from error
            except (RuntimeError,ValueError) as error:
                code,_,message=str(error).partition(':')
                raise ApiError(409 if isinstance(error,RuntimeError) else 400,code,message.strip()) from error
        if key in {('reshoot-list',), ('reshoot-list', 'export')}:
            from media_catalog.creative_assistance import build_reshoot_list, get_reshoot_list, export_reshoot_list
            try:
                if method == 'GET' and key == ('reshoot-list',):
                    return ApiResult(200, get_reshoot_list(self.db, task_id))
                if method == 'POST' and key == ('reshoot-list',):
                    return ApiResult(200, build_reshoot_list(self.db, task_id, _required_string(body, 'expectedRevision'), project_root=self.project_root))
                if method == 'POST' and key == ('reshoot-list', 'export'):
                    content = export_reshoot_list(self.db, task_id, _required_string(body, 'reportId'),
                        _required_string(body, 'expectedRevision'), format=body.get('format', 'text'))
                    return ApiResult(200, {'content': content, 'format': body.get('format', 'text')})
            except KeyError as error:
                raise ApiError(404, 'RESHOOT_NOT_FOUND', '当前任务没有这份缺口清单。') from error
            except (RuntimeError, ValueError) as error:
                code, _, message = str(error).partition(':')
                raise ApiError(409 if code in {'RESHOOT_REVISION_CHANGED', 'RESHOOT_INPUTS_CHANGED'} else 400,
                               code, message.strip()) from error
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
        if method == "GET" and len(key) == 3 and key[0] == "packaging" and key[2] == "music-candidates":
            from media_catalog.packaging_music_candidates import candidates
            _owned_plan(self.db, task_id, key[1])
            return ApiResult(200, candidates(self.db, task_id, key[1]))
        if method == "GET" and len(key) == 3 and key[0] == "packaging" and key[2] == "music-review":
            from media_catalog.packaging_music_review import review_history
            _owned_plan(self.db, task_id, key[1])
            return ApiResult(200, review_history(self.db, task_id, key[1]))
        if method == "GET" and key == ("delivery-copy", "suggestion"):
            candidates = list_candidates(self.db, task_id)
            availability = delivery_copy_availability(
                self.project_root, get_task(self.db, task_id).get("orchestration_mode"),
            )
            result = latest_delivery_copy_suggestion(
                self.db, self.project_root, task_id, candidates[0]["candidate_id"],
            ) if candidates else {"status": "not_requested", "jobId": None, "receiptId": None}
            return ApiResult(200, {**result, "availability": availability})
        if method == "GET" and key == ("storyboard", "recovery-availability"):
            from media_catalog.workbench_actions import storyboard_recovery_availability
            return ApiResult(200, storyboard_recovery_availability(self.db, task_id, self.project_root))
        if method == "GET" and key == ("sources", "status"):
            from media_catalog.source_browsing import source_browsing_status
            return ApiResult(200, source_browsing_status(self.db, task_id))
        if method != "POST":
            raise ApiError(404, "NOT_FOUND", "未找到该接口。")

        source_sensitive = (
            key in {("picture", "lock"), ("render",), ("publish",)}
            or (key and key[0] == "packaging")
            or (key and key[0] == "rough-cut" and key != ("rough-cut", "impact"))
            or (key and key[0] == "rough-media")
            or (key and key[0] == "storyboard" and key[1:2] in {
                ("continue",), ("edit",), ("undo",), ("apply-candidate",),
                ("rerank",), ("motion",),
            })
        )
        if source_sensitive and task_has_source_configuration(self.db, task_id):
            if initial_source_check_pending(self.db, task_id):
                raise ApiError(
                    409, "SOURCE_CHECK_PENDING",
                    "创建后的素材检查尚未完成，请稍后重试。",
                )
            readiness = source_readiness(self.db, task_id)
            if not readiness["ready"]:
                code = (
                    "PRIMARY_SOURCE_OFFLINE"
                    if readiness["reason"] == "primary_offline"
                    else "SOURCE_REVIEW_REQUIRED"
                )
                raise ApiError(
                    409, code,
                    "素材范围或文件已变化，请先核对并处理影响。",
                )

        if key in {("scene-windows", "preview"), ("scene-windows", "start")}:
            try:
                if key[1] == "preview":
                    return ApiResult(200, preview_scene_windows(
                        self.db, task_id, None, body.get("assets"),
                        offset=body.get("offset", 0), limit=body.get("limit", 50),
                    ))
                queued = queue_scene_window_job(
                    self.db, task_id, None, body.get("assets"),
                    _required_string(body, "requestId"),
                )
                return ApiResult(202, {"job": _public_job(
                    self.db, get_job(self.db, queued["jobId"])), **queued})
            except ValueError as error:
                code, _, detail = str(error).partition(":")
                raise ApiError(409 if code in {"ASSET_OFFLINE", "ASSET_STALE",
                                               "JOB_IDEMPOTENCY_CONFLICT"} else 400,
                               code, detail.strip() or "场景候选请求无效") from error

        if key == ("visual-analysis", "preview"):
            try:
                return ApiResult(200, preview_visual_batch(
                    self.db, task_id, _required_string(body, "profileId"),
                    body.get("items"),
                ))
            except ValueError as error:
                code, _, detail = str(error).partition(":")
                if code not in {
                    "VISUAL_PROFILE_INVALID", "VISUAL_PROFILE_UNVERIFIED",
                    "VISUAL_BATCH_INVALID", "VISUAL_BATCH_DUPLICATE",
                    "ASSET_SCOPE_INVALID", "ASSET_NOT_FOUND", "ASSET_OFFLINE",
                    "ASSET_STALE", "ASSET_PATH_INVALID", "MEDIA_PROBE_REQUIRED",
                    "SOURCE_WINDOW_INVALID", "MEDIA_KIND_INVALID",
                }:
                    code, detail = "VISUAL_BATCH_INVALID", "批次预览失败"
                raise ApiError(409 if code in {
                    "VISUAL_PROFILE_UNVERIFIED", "ASSET_OFFLINE", "ASSET_STALE",
                } else 400, code, detail.strip() or "批次预览失败") from error

        if key == ("visual-analysis", "start"):
            try:
                queued = queue_visual_batch(
                    self.db, task_id, _required_string(body, "profileId"),
                    body.get("items"), _required_string(body, "previewDigest"),
                    _required_string(body, "requestId"), body.get("confirmPaidCall"),
                )
            except ValueError as error:
                code, _, detail = str(error).partition(":")
                status = 409 if code in {
                    "PAID_CONFIRMATION_REQUIRED", "VISUAL_PREVIEW_STALE",
                    "VISUAL_BATCH_BLOCKED", "VISUAL_BATCH_ALREADY_ACTIVE",
                    "JOB_IDEMPOTENCY_CONFLICT",
                    "VISUAL_PROFILE_UNVERIFIED", "ASSET_OFFLINE", "ASSET_STALE",
                } else 400
                raise ApiError(status, code, detail.strip() or "无法开始素材分析") from error
            return ApiResult(202, {"job": _public_job(
                self.db, get_job(self.db, queued["jobId"])), **queued})

        if key == ("brief", "rework", "preview"):
            try:
                preview = preview_script_rework(
                    self.db, self.project_root, task_id,
                    _optional_string(body, "reworkId"), body.get("text"),
                )
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, {
                "token": preview["token"],
                "baseRevision": preview["base_revision"],
                "dependencyDigest": preview["dependency_digest"],
                "changedInputs": preview["changed_inputs"],
                "actualAffectedArtifacts": preview["actual_affected_artifacts"],
                "runningJobs": preview["running_jobs"],
                "changed": preview["changed"],
                "ready": preview["ready"],
                "costConfirmationRequired": preview["cost_confirmation_required"],
                "expiresAt": preview["expires_at"],
            })
        if key == ("brief", "rework", "commit"):
            try:
                result = commit_script_rework(
                    self.db, self.project_root, task_id,
                    _optional_string(body, "reworkId"), body.get("text"),
                    _required_string(body, "token"),
                    expected_revision=_required_string(body, "expectedRevision"),
                    request_id=_required_string(body, "requestId"),
                    confirm_cost=body.get("confirmCost") is True,
                )
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, result["receipt"])
        if key == ("brief", "voice"):
            from media_catalog.model_profiles import select_task_tts_voice, TaskVoiceSelectionError
            try:
                result = select_task_tts_voice(
                    self.db, task_id,
                    profile_id=_required_string(body, "profileId"),
                    voice_id=_required_string(body, "voiceId"),
                    expected_binding_digest=(None if "expectedBindingDigest" in body and body["expectedBindingDigest"] is None
                                             else _required_string(body, "expectedBindingDigest")),
                )
            except TaskVoiceSelectionError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, result)
        if key == ("brief", "save"):
            try:
                save_script_draft(
                    self.db, self.project_root, task_id, body.get("text"),
                    _required_string(body, "expectedScriptSha256"),
                )
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, self._task_detail(get_task(self.db, task_id)))
        if key == ("brief", "lock"):
            try:
                confirmation = confirm_script(self.db, self.project_root, task_id, _required_string(body, "expectedScriptSha256"))
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            confirmed_task = confirmation["task"]
            if confirmed_task.get("task_type") == "ad_mix":
                return ApiResult(200, self._task_detail(confirmed_task))
            try:
                dispatch = transcription_dispatch(self.db, confirmed_task, self.project_root)
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            if dispatch is None:
                return ApiResult(200, self._task_detail(confirmed_task))
            if dispatch["existing"]:
                public = _public_job(self.db, get_job(self.db, dispatch["job_id"]))
                return ApiResult(202 if public["status"] in {"queued", "running"} else 200, public)
            def check_transcription_inputs(_connection):
                try:
                    require_transcription_inputs(
                        self.db, task_id, dispatch["input_digest"],
                        script_sha256=dispatch["script_sha256"], project_root=self.project_root,
                    )
                except ScriptDraftError as error:
                    raise ApiError(error.status, error.code, error.message) from error
            return self._enqueue(task_id, "transcribe", {
                "script_sha256": dispatch["script_sha256"], "input_digest": dispatch["input_digest"],
            }, job_id=dispatch["job_id"], pre_enqueue_check=check_transcription_inputs)
        if key == ("transcript", "recover"):
            from media_catalog.transcript_recovery import request_recovery
            try:
                recovered = request_recovery(self.db, self.project_root, task_id,
                    _required_string(body, "expectedScriptSha256"))
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            if recovered is None:
                return ApiResult(200, {"status": "current"})
            public = _public_job(self.db, recovered)
            return ApiResult(202 if public["status"] in {"queued", "running"} else 200, public)
        if key == ("transcript", "finding"):
            from media_catalog.transcript_finding_review import save_finding_review
            try:
                save_finding_review(self.db, self.project_root, task_id, body)
            except ValueError as error:
                code, _, detail = str(error).partition(":")
                raise ApiError(409, code, detail.strip()) from error
            return ApiResult(200, self._task_detail(get_task(self.db, task_id)))
        if key == ("transcript", "confirm"):
            try:
                # Serialize receipt writes with per-finding saves and script/source rework.
                with self.db.transaction(immediate=True):
                    confirm_transcript_review(
                        self.project_root, get_task(self.db, task_id),
                        _required_string(body, "expectedDigest"),
                        body.get("confirmListened"),
                    )
            except ValueError as error:
                code, _, detail = str(error).partition(":")
                raise ApiError(409, code, detail.strip() or "当前口播核对不可确认") from error
            return ApiResult(200, self._task_detail(get_task(self.db, task_id)))
        if key == ("transcript", "revise-script"):
            try:
                revise_transcribed_script(
                    self.db, self.project_root, task_id, body.get("text"),
                    _required_string(body, "expectedScriptSha256"),
                    _required_string(body, "expectedDigest"),
                )
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, self._task_detail(get_task(self.db, task_id)))
        if key == ("transcript", "source-rework", "preview"):
            try:
                preview = preview_source_rework(
                    self.db, self.project_root, task_id,
                    _optional_string(body, "reworkId"), _required_string(body, "sourcePath"),
                )
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, {
                "token": preview["token"], "baseRevision": preview["base_revision"],
                "changedInputs": preview["changed_inputs"],
                "actualAffectedArtifacts": preview["actual_affected_artifacts"],
                "runningJobs": preview["running_jobs"], "blocking": preview["blocking"],
                "warnings": preview["warnings"], "source": preview["source"],
                "changed": preview["changed"], "ready": preview["ready"],
                "costConfirmationRequired": preview["cost_confirmation_required"],
                "expiresAt": preview["expires_at"],
            })
        if key == ("transcript", "source-rework", "commit"):
            try:
                result = commit_source_rework(
                    self.db, self.project_root, task_id,
                    _optional_string(body, "reworkId"), _required_string(body, "sourcePath"),
                    _required_string(body, "token"),
                    expected_revision=_required_string(body, "expectedRevision"),
                    request_id=_required_string(body, "requestId"),
                    confirm_cost=body.get("confirmCost") is True,
                )
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, result["receipt"])
        if key == ("transcript", "source-preview"):
            try:
                preview = preview_source_replacement(
                    self.db, self.project_root, task_id,
                    _required_string(body, "sourcePath"),
                )
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, preview)
        if key == ("transcript", "replace-source"):
            try:
                replace_digital_source(
                    self.db, self.project_root, task_id,
                    _required_string(body, "sourcePath"),
                    _required_string(body, "previewDigest"),
                )
            except ScriptDraftError as error:
                raise ApiError(error.status, error.code, error.message) from error
            return ApiResult(200, self._task_detail(get_task(self.db, task_id)))
        if key == ("sources", "configuration"):
            from media_catalog.task_source_editing import source_configuration
            return ApiResult(200, source_configuration(self.db, task_id))
        if key == ("sources", "configure"):
            from media_catalog.task_source_editing import change_source_configuration
            try:
                result = change_source_configuration(
                    self.db, task_id, primary_library_id=body.get("primaryLibraryId"),
                    selected_generic_ids=body.get("selectedGenericIds"),
                    expected_revision=body.get("expectedRevision"),
                )
            except ValueError as error:
                code, _, message = str(error).partition(": ")
                raise ApiError(409, code, message or str(error)) from error
            return ApiResult(200, result)
        if key == ("sources", "preview"):
            preview = source_readiness(self.db, task_id)
            initial = get_initial_source_check(self.db, task_id)
            if initial is not None:
                preview["initialCheck"] = {
                    "jobId": initial["job_id"],
                    "status": initial["status"],
                    "summary": initial["summary"],
                    "checkedAt": initial["checked_at"],
                }
            return ApiResult(200, preview)
        if key == ("sources", "sync"):
            _require_gate(self.db, task_id, "brief_locked", "BRIEF_GATE_MISSING")
            if initial_source_check_pending(self.db, task_id):
                raise ApiError(
                    409, "SOURCE_CHECK_PENDING",
                    "创建后的素材检查尚未完成，请稍后重试。",
                )
            token = _optional_string(body, "impactToken")
            readiness = source_readiness(self.db, task_id)
            if readiness["reason"] == "primary_offline":
                raise ApiError(
                    409, "PRIMARY_SOURCE_OFFLINE",
                    "主素材目录不可访问，请先恢复连接。",
                )
            if readiness["changed"] and token != readiness["impact_token"]:
                raise ApiError(
                    409, "SOURCE_REVIEW_REQUIRED",
                    "素材范围或文件发生变化，请先核对影响并确认。",
                )
            return self._enqueue(task_id, "sync_sources", {"impact_token": token})
        if key == ("storyboard", "recover-generated"):
            from media_catalog.workbench_actions import validate_storyboard_recovery
            request_id = _required_string(body, "requestId")
            validated = validate_storyboard_recovery(self.db, task_id, self.project_root)
            expected_digest = validated["input_digest"]

            def recheck_recovery(_connection):
                current = validate_storyboard_recovery(self.db, task_id, self.project_root)
                if current["input_digest"] != expected_digest:
                    raise RuntimeError("STORYBOARD_STALE: 入队前恢复依据已变化，请刷新后重试")

            job_id = "job-recover-" + uuid.uuid5(
                uuid.NAMESPACE_URL, f"cutroom:{task_id}:{request_id}:{expected_digest}").hex
            return self._enqueue(
                task_id, "recover_storyboard",
                {"request_id": request_id, "local_recovery_only": True,
                 "expected_input_digest": expected_digest},
                cost_status="waived", job_id=job_id,
                pre_enqueue_check=recheck_recovery)
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
            if task.get("task_type") == "digital_human":
                transcript = project_transcript_state(
                    self.project_root, task, verify_source_content=True
                )
                if transcript["status"] == "needs_review":
                    raise ApiError(409, "TRANSCRIPT_REVIEW_REQUIRED", "请先回听并核对口播与锁定文案的关键疑点。")
                if transcript["status"] not in {"aligned", "reviewed"}:
                    raise ApiError(409, "TRANSCRIPTION_REQUIRED", "请先完成当前口播的转写与核验。")
            if initial_source_check_pending(self.db, task_id):
                raise ApiError(
                    409, "SOURCE_CHECK_PENDING",
                    "创建后的素材检查尚未完成，请稍后重试。",
                )
            readiness = source_readiness(self.db, task_id)
            if not readiness["ready"]:
                code = (
                    "PRIMARY_SOURCE_OFFLINE"
                    if readiness["reason"] == "primary_offline"
                    else "SOURCE_REVIEW_REQUIRED"
                )
                raise ApiError(
                    409, code,
                    "素材检查尚未通过，请先处理变化或连接异常。",
                )
            request_id = _required_string(body, "requestId")

            def bind_recommendation_profiles(_connection):
                from media_catalog.model_profiles import ModelProfileUnavailable
                from media_catalog.storyboard_profile_bindings import bind_missing_storyboard_profiles
                try:
                    bind_missing_storyboard_profiles(self.db, task)
                except ModelProfileUnavailable as error:
                    raise ApiError(
                        409, "MODEL_PROFILE_UNAVAILABLE",
                        f"{error}。请在设置的“模型与配音”中选择并检测默认配置，再重新推荐。",
                    ) from error

            return self._enqueue(
                task_id,
                "recommend_storyboard",
                {"request_id": request_id},
                cost_status="approved",
                provider_request_id=f"storyboard:{task_id}:{request_id}",
                pre_enqueue_check=bind_recommendation_profiles,
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
        if key == ("storyboard", "continue-checked"):
            from media_catalog.source_continue import queue_source_continue
            job_id = queue_source_continue(self.db, task_id,
                _required_string(body, "expectedStoryboardDigest"), _required_string(body, "requestId"))
            public = _public_job(self.db, get_job(self.db, job_id))
            return ApiResult(202 if public['status'] in {'queued','running'} else 200, public)
        if key == ("storyboard", "continue"):
            return ApiResult(200, accept_storyboard(
                self.db, task_id, _required_string(body, "expectedStoryboardDigest")
            ))
        if key == ("storyboard", "motion", "accept"):
            return ApiResult(200, accept_motion_advisory(
                self.db, task_id, _required_string(body, "pairKey"),
                _required_string(body, "expectedStoryboardDigest"),
                lambda: storyboard_digest(self.db, task_id),
                lambda: self._task_detail(get_task(self.db, task_id))["storyboard"]["groups"],
            ))
        if key == ("storyboard", "refresh-materials"):
            from media_catalog.storyboard_refresh import refresh_storyboard_materials
            return ApiResult(200,refresh_storyboard_materials(self.db,task_id,
                _required_string(body,"expectedStoryboardDigest")))
        if key == ("storyboard", "edit"):
            return ApiResult(200, apply_storyboard_edit(
                self.db, task_id, _required_string(body, "expectedStoryboardDigest"),
                _required_dict(body, "edit"),
            ))
        if key == ("storyboard", "undo"):
            return ApiResult(200, undo_storyboard_edit(
                self.db, task_id, _required_string(body, "expectedStoryboardDigest"),
            ))
        if key == ("rough-subtitles",):
            from media_catalog.rough_subtitles import build_rough_subtitle_preview
            return ApiResult(200, build_rough_subtitle_preview(
                self.project_root, task, _required_string(body, "expectedTranscriptDigest"),
            ))
        if key == ("rough-cut", "impact"):
            return ApiResult(200, preview_edit_impact(
                self.db, task_id, _required_dict(body, "edit"),
                expected_digest=_required_string(body, "expectedPictureViewDigest"),
            ))
        if key == ("rough-cut", "apply"):
            return ApiResult(200, apply_edit(
                self.db, task_id, _required_dict(body, "edit"),
                impact_token=_optional_string(body, "impactToken"),
                expected_digest=_required_string(body, "expectedPictureViewDigest"),
            ))
        if key == ("rough-cut", "undo"):
            return ApiResult(200, undo_last_edit(
                self.db, task_id, expected_digest=_required_string(body, "expectedPictureViewDigest"),
            ))
        if key == ("rough-cut", "redo"):
            return ApiResult(200, redo_last_edit(
                self.db, task_id, expected_digest=_required_string(body, "expectedPictureViewDigest"),
            ))
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
            view_digest = _required_string(body, "pictureViewDigest")
            # Cheap preflight before FFmpeg; set_picture_lock rechecks atomically.
            from media_catalog.visual_uniqueness import DuplicateVisualError, validate_task_scene_uniqueness
            if picture_view_digest(self.db, task_id) != view_digest:
                raise ApiError(409, "PICTURE_VIEW_CHANGED", "画面已变化，请刷新后核对当前粗剪。")
            try:
                validate_task_scene_uniqueness(self.db.connection, task_id)
            except DuplicateVisualError as error:
                ids = [row[0] for row in self.db.connection.execute(
                    "SELECT card_id FROM scene_cards WHERE task_id=? ORDER BY idx,card_id", (task_id,))]
                positions = [ids.index(card_id) + 1 for card_id in error.card_ids]
                numbers = "、".join(str(position) for position in positions)
                raise ApiError(409, "DUPLICATE_VISUAL",
                    f"第 {numbers} 镜头使用了重复画面。请更换其中一个镜头或调整取用区间，再确认锁定。",
                    {"cardIds": error.card_ids, "positions": positions, "evidence": error.evidence}) from error
            assert_picture_media_available(self.db, task_id, media_versions)
            if task.get("task_type") == "ad_mix":
                require_ad_picture_assets(self.db, task_id)
            return ApiResult(200, set_picture_lock(
                self.db, task_id, confirm_all=True, project_root=self.project_root,
                expected_view_digest=view_digest,
                expected_media_versions=media_versions,
            ))
        if len(key) == 3 and key[0] == "packaging" and key[2] == "music-comparison":
            from media_catalog.packaging_music_candidates import create_comparison
            _owned_plan(self.db, task_id, key[1])
            return ApiResult(200, create_comparison(self.db, task_id, key[1], body.get("assetRef"),
                _required_string(body, "contextDigest"), _required_string(body, "requestId")))
        if key == ("packaging", "analyze-audio"):
            _require_gate(self.db, task_id, "picture_lock", "PICTURE_LOCK_MISSING")
            try:
                result = queue_packaging_audio(self.db, self.project_root, task_id)
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(409, code, message.strip()) from error
            return ApiResult(202, result)
        if len(key) == 4 and key[:2] == ("packaging", "suggestions") and key[3] == "cancel":
            from media_catalog.packaging_agent_requests import cancel_agent_request
            return ApiResult(200, cancel_agent_request(self.db, task_id, key[2]))
        if key == ("packaging", "suggestions"):
            _require_gate(self.db, task_id, "picture_lock", "PICTURE_LOCK_MISSING")
            try:
                result = queue_packaging_suggestion(self.db, self.project_root, task_id,
                    style=body.get("style", "focus"), request_id=_required_string(body, "requestId"),
                    confirm_paid_call=body.get("confirmPaidCall"), base_plan_id=body.get("basePlanId"),
                    expected_visual_profile_id=body.get("expectedVisualProfileId"), expected_visual_digest=body.get("expectedVisualDigest"), execution_mode=body.get("executionMode"),
                    expected_prompt_version=body.get("expectedPromptVersion"), expected_prompt_sha256=body.get("expectedPromptSha256"), requested_roles=body.get("requestedRoles"))
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(409, code, message.strip()) from error
            return ApiResult(202, result)
        if key == ("packaging", "propose"):
            _require_gate(self.db, task_id, "picture_lock", "PICTURE_LOCK_MISSING")
            ids = propose_packaging(self.db, task_id, None,
                                    project_root=self.project_root)
            return ApiResult(200, {"planIds": ids, "plans": list_plans(self.db, task_id)})
        if key == ("packaging", "compose"):
            _require_gate(self.db, task_id, "picture_lock", "PICTURE_LOCK_MISSING")
            return ApiResult(200, self._compose_packaging(task, body))
        if len(key) == 3 and key[0] == "packaging" and key[2] == "settings":
            _require_gate(self.db, task_id, "picture_lock", "PICTURE_LOCK_MISSING")
            _owned_plan(self.db, task_id, key[1])
            revision = body.get("expectedRevision")
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise ApiError(400, "PACKAGING_REVISION_INVALID", "请提供当前包装设置版本。")
            changes = {"expected_revision": revision}
            if "subtitleHighlight" in body:
                changes["subtitle_highlight"] = body["subtitleHighlight"]
            if "subtitleAppearance" in body:
                changes["subtitle_appearance"] = body["subtitleAppearance"]
            if "bgmGain" in body:
                changes["bgm_gain"] = body["bgmGain"]
            try:
                return ApiResult(200, update_plan_settings(self.db, key[1], **changes))
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                status = 409 if code in {
                    "PACKAGING_REVISION_CONFLICT", "PACKAGING_LEGACY_PLAN",
                    "PACKAGING_RECOMMENDATION_IMMUTABLE", "PACKAGING_PLAN_CONFIRMED",
                } else 400
                raise ApiError(status, code, message.strip() or "包装设置保存失败。") from error
        if len(key) >= 3 and key[0] == "packaging" and key[2] == "instances":
            _require_gate(self.db, task_id, "picture_lock", "PICTURE_LOCK_MISSING")
            _owned_plan(self.db, task_id, key[1])
            revision = body.get("expectedRevision")
            try:
                if len(key) == 3:
                    result = add_packaging_instance(
                        self.db, key[1], _required_dict(body, "instance"),
                        expected_revision=revision,
                    )
                elif len(key) == 5 and key[4] == "update":
                    result = update_packaging_instance(
                        self.db, key[1], key[3], _required_dict(body, "changes"),
                        expected_revision=revision,
                    )
                elif len(key) == 5 and key[4] == "delete":
                    result = delete_packaging_instance(
                        self.db, key[1], key[3], expected_revision=revision,
                    )
                else:
                    raise ApiError(404, "NOT_FOUND", "包装元素操作不存在。")
            except (ValueError, RuntimeError, KeyError) as error:
                code, _, message = str(error).strip("'\"").partition(":")
                status = 409 if code in {
                    "PACKAGING_REVISION_CONFLICT", "PACKAGING_LEGACY_PLAN",
                    "PACKAGING_INSTANCE_IN_USE", "PACKAGING_RECOMMENDATION_IMMUTABLE",
                    "PACKAGING_PLAN_CONFIRMED",
                } else 404 if code == "PACKAGING_INSTANCE_NOT_FOUND" else 400
                raise ApiError(status, code, message.strip() or "包装元素操作失败。") from error
            return ApiResult(200, {**result, "plan": get_plan(self.db, key[1])})
        if len(key) == 3 and key[0] == "packaging" and key[2] == "preview":
            _owned_plan(self.db, task_id, key[1])
            result = request_preview(self.db, task_id, key[1])
            artifact = current_preview_for_plan(self.db, task_id, key[1])
            return ApiResult(
                202 if result.get("job") else 200,
                _public_preview_request(self.db, result, task_id, artifact),
            )
        if len(key) == 3 and key[0] == "packaging-previews" and key[2] == "music-feedback":
            from media_catalog.packaging_music_review import record_feedback as record_music_feedback
            try:
                result = record_music_feedback(self.db, task_id, key[1],
                    _required_string(body, "rating"), _required_string(body, "requestId"))
            except (ValueError, RuntimeError, PermissionError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(403 if isinstance(error, PermissionError) else 400 if isinstance(error, ValueError) else 409,
                    code, message.strip()) from error
            return ApiResult(200, result)
        if len(key) == 3 and key[0] == "packaging-previews" and key[2] in {"watch", "watch-tick", "viewed"}:
            from media_catalog.packaging_watch import start_preview_watch, tick_preview_watch
            try:
                if key[2] == "watch":
                    result = start_preview_watch(self.db, task_id, key[1], self.project_root)
                elif key[2] == "watch-tick":
                    result = tick_preview_watch(self.db, task_id, key[1],
                        _required_string(body, "sessionId"), body.get("sequence"),
                        body.get("positionSeconds"), body.get("event"), self.project_root)
                else:
                    result = _public_packaging_preview(mark_preview_viewed(self.db, task_id, key[1],
                        watch_session_id=_required_string(body, "sessionId"), project_root=self.project_root), task_id)
            except (ValueError, RuntimeError, KeyError) as error:
                code, _, message = str(error).strip("'\"").partition(":")
                raise ApiError(400 if isinstance(error, ValueError) else 409, code, message.strip()) from error
            return ApiResult(200, result)
        if len(key) == 3 and key[0] == "packaging" and key[2] == "save-template":
            _owned_plan(self.db, task_id, key[1])
            return ApiResult(200, save_personal_template(
                self.db, key[1], _required_string(body, "templateName")
            ))
        if len(key) == 3 and key[0] == "packaging" and key[2] == "confirm":
            _owned_plan(self.db, task_id, key[1])
            if body.get("confirmationMode") == "final_review":
                from media_catalog.packaging_preview import confirm_plan_for_generation
                return ApiResult(200, confirm_plan_for_generation(self.db, task_id, key[1],
                    expected_digest=_required_string(body, "expectedConfirmationDigest")))
            return ApiResult(200, confirm_plan_after_preview(
                self.db,
                task_id,
                key[1],
                _optional_string(body, "previewId"),
                project_root=self.project_root,
            ))
        if key == ("render",):
            _require_gate(self.db, task_id, "packaging_confirmed", "PACKAGING_PLAN_MISSING")
            payload = {}
            if body.get("intelligentReview") is True:
                from media_catalog.intelligent_review import render_review_options
                payload["intelligent_review"] = render_review_options(self.db)
            result = self._enqueue(task_id, "render_and_qa", payload)
            setup_error = payload.get("intelligent_review", {}).get("setup_error")
            if setup_error:
                return ApiResult(result.status, {**result.payload, "intelligentReview": {
                    "status": "not_started", "reason": setup_error,
                }})
            return result
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
            if body.get("manualReviewAcknowledged") is True:
                from media_catalog.content_review import approve_with_content_acknowledgement
                return ApiResult(200, approve_with_content_acknowledgement(
                    self.db, task_id, _required_string(body, "candidateId"),
                    _required_string(body, "snapshotId"), _required_string(body, "qaReportId"),
                    _required_string(body, "watchSessionId"), _required_string(body, "reviewedBy"),
                    run_id=_required_string(body, "contentReviewRunId"), acknowledged=True,
                    notes=_optional_string(body, "notes")))
            return ApiResult(200, approve_after_full_watch(
                self.db, task_id,
                _required_string(body, "snapshotId"),
                _required_string(body, "qaReportId"),
                _required_string(body, "watchSessionId"),
                _required_string(body, "reviewedBy"),
                _optional_string(body, "notes"),
            ))
        if key in {("review", "audio", "prepare"), ("review", "audio", "start")}:
            from media_catalog.audio_review_jobs import prepare_audio_review, queue_audio_review
            _required_string(body, "candidateId")
            candidate = _require_candidate_binding(self.db, task_id, body,
                snapshot_id=_required_string(body, "snapshotId"),
                qa_report_id=_required_string(body, "qaReportId"))
            try:
                options = {"start_seconds": body.get("startSeconds", 0),
                           "end_seconds": body.get("endSeconds")}
                if key[-1] == "prepare":
                    return ApiResult(200, prepare_audio_review(self.db, self.project_root,
                        task_id, candidate["candidate_id"], **options))
                return ApiResult(202, queue_audio_review(self.db, self.project_root,
                    task_id, candidate["candidate_id"], account_id=_required_string(body, "accountId"),
                    request_id=_required_string(body, "requestId"),
                    inputs_digest=_required_string(body, "inputsDigest"),
                    confirm_paid_call=body.get("confirmPaidCall") is True, **options))
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(400 if isinstance(error, ValueError) else 409,
                               code, message.strip()) from error
        if key == ("review", "intelligent", "start"):
            from media_catalog.intelligent_review import review_options, queue_intelligent_review
            candidate = _require_candidate_binding(self.db, task_id, body,
                snapshot_id=_required_string(body,"snapshotId"),qa_report_id=_required_string(body,"qaReportId"))
            if body.get("confirmPaidCall") is not True or body.get("confirmAudioCall") is not True:
                raise ValueError("PAID_CONFIRMATION_REQUIRED: 请确认成片音画检查")
            return ApiResult(202,queue_intelligent_review(self.db,self.project_root,task_id,
                candidate["candidate_id"],review_options(self.db),request_id=_required_string(body,"requestId")))
        if key == ("review", "content", "start"):
            from media_catalog.content_review_jobs import queue_content_review
            candidate = _require_candidate_binding(self.db, task_id, body,
                snapshot_id=_required_string(body, "snapshotId"),
                qa_report_id=_required_string(body, "qaReportId"))
            return ApiResult(202, queue_content_review(self.db, self.project_root, task_id,
                candidate["candidate_id"], profile_id=_required_string(body, "profileId"),
                request_id=_required_string(body, "requestId"),
                confirm_paid_call=body.get("confirmPaidCall") is True,
                review_mode=body.get("reviewMode", "sparse"),
                audio_account_id=body.get("audioAccountId"),
                confirm_audio_call=body.get("confirmAudioCall") is True))
        if key == ("review", "content", "takeover"):
            candidate = _require_candidate_binding(self.db, task_id, body)
            try:
                takeover = acknowledge_content_review_takeover(
                    self.db, task_id, candidate["candidate_id"],
                    _required_string(body, "watchSessionId"),
                    acknowledged_by=_required_string(body, "reviewedBy"),
                    acknowledged=body.get("acknowledged") is True,
                )
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(400 if isinstance(error, ValueError) else 409,
                               code, message.strip()) from error
            return ApiResult(200, takeover)
        if key == ("review", "content", "issues", "retain"):
            candidate = _require_candidate_binding(self.db, task_id, body)
            try:
                decisions = retain_content_review_issues(
                    self.db, task_id, candidate["candidate_id"], _required_string(body, "runId"),
                    body.get("issueIndexes"), reviewed_by=_required_string(body, "reviewedBy"),
                    reason=_required_string(body, "reason"), expected_decisions=body.get("expectedDecisions"),
                )
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(400 if isinstance(error, ValueError) else 409, code, message.strip()) from error
            return ApiResult(200, {"decisions": decisions})
        if len(key) == 5 and key[:3] == ("review", "content", "issues") and key[4] == "retain":
            candidate = _require_candidate_binding(self.db, task_id, body)
            try:
                if not key[3].isdecimal():
                    raise ValueError("CONTENT_REVIEW_ISSUE_INVALID: 问题序号无效")
                issue_index = int(key[3])
                decision = retain_content_review_issue(
                    self.db, task_id, candidate["candidate_id"],
                    _required_string(body, "runId"), issue_index,
                    reviewed_by=_required_string(body, "reviewedBy"),
                    reason=_required_string(body, "reason"),
                )
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(400 if isinstance(error, ValueError) else 409,
                               code, message.strip()) from error
            return ApiResult(200, decision)
        if key == ("review", "issues"):
            candidate = _require_candidate_binding(self.db, task_id, body)
            try:
                issue = add_review_issue(
                    self.db, task_id, candidate["candidate_id"],
                    _required_string(body, "category"),
                    _required_string(body, "notes"),
                    _optional_number(body, "positionSeconds"),
                    request_id=_required_string(body, "requestId"),
                )
            except (ValueError, RuntimeError, KeyError) as error:
                raise _review_issue_api_error(error) from error
            return ApiResult(200, _public_review_issue(issue))
        if len(key) == 4 and key[:2] == ("review", "issues") and key[3] == "withdraw":
            candidate = _require_candidate_binding(self.db, task_id, body)
            try:
                issue = withdraw_review_issue(
                    self.db, task_id, candidate["candidate_id"], key[2],
                )
            except (ValueError, RuntimeError, KeyError) as error:
                raise _review_issue_api_error(error) from error
            return ApiResult(200, _public_review_issue(issue))
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
            rework = get_rework_request(self.db, task_id, key[2])
            if rework is not None and rework["category"] == "script":
                raise ApiError(409, "SCRIPT_CHANGE_REQUIRED", "请在文案页预览实际影响并提交新文案。")
            if body.get("confirmCost") is True:
                approve_rework_cost(self.db, task_id, key[2])
            execution_key = _required_string(body, "idempotencyKey")
            if (rework is not None and rework["category"] == "technical"
                    and rework["target_stage"] == "auto_qa"):
                from media_catalog.snapshots import latest_qa_only_rework

                def apply_qa_rework(_connection):
                    execute_rework(self.db, task_id, key[2], idempotency_key=execution_key)
                    active = latest_qa_only_rework(self.db, task_id)
                    if active is None or active["rework_id"] != key[2]:
                        raise RuntimeError("QA_REWORK_STALE: 这次仅重做质检的请求已过期，请刷新后重新确认。")

                queued = self._enqueue(
                    task_id, "render_and_qa", {"qa_rework_id": key[2]},
                    cost_status="waived", job_id=f"job-qa-{key[2]}",
                    pre_enqueue_check=apply_qa_rework,
                )
                # A duplicate queue entry still has to match the original execution key.
                result = execute_rework(self.db, task_id, key[2], idempotency_key=execution_key)
                return ApiResult(queued.status, {**_public_rework(result), "job": queued.payload})
            result = execute_rework(
                self.db, task_id, key[2], idempotency_key=execution_key,
            )
            return ApiResult(200, _public_rework(result))
        if key == ("delivery-copy", "suggestions"):
            candidate = _require_candidate_binding(self.db, task_id, body)
            _require_gate(self.db, task_id, "human_reviewed", "REVIEW_NOT_APPROVED")
            try:
                queued = queue_delivery_copy_suggestion(
                    self.db, self.project_root, task_id,
                    candidate_id=candidate["candidate_id"],
                    request_id=_required_string(body, "requestId"),
                    confirm_paid_call=body.get("confirmPaidCall"),
                )
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(409 if code in {
                    "PAID_CONFIRMATION_REQUIRED", "P11_REVIEW_REQUIRED",
                    "P11_PROMPT_CHANGED", "P11_AGENT_CONTEXT_APPROVAL_REQUIRED",
                    "MODEL_BINDING_MISSING",
                    "CANDIDATE_CHANGED", "REVIEW_NOT_APPROVED",
                    "SCRIPT_CHANGED", "JOB_IDEMPOTENCY_CONFLICT",
                    "REQUEST_REPLAY_MISMATCH",
                } else 400, code, message.strip()) from error
            return ApiResult(202, queued)
        if key == ("delivery-copy", "adopt"):
            candidate = _require_candidate_binding(self.db, task_id, body)
            _require_gate(self.db, task_id, "human_reviewed", "REVIEW_NOT_APPROVED")
            try:
                draft = accept_delivery_copy_suggestion(
                    self.db, self.project_root, task_id,
                    candidate_id=candidate["candidate_id"],
                    receipt_id=_required_string(body, "receiptId"),
                    expected_revision=body.get("expectedRevision"),
                    title_index=body.get("titleIndex"),
                    description=body.get("description"),
                    tags=_string_list(body, "tags"),
                )
            except (ValueError, RuntimeError) as error:
                code, _, message = str(error).partition(":")
                raise ApiError(409 if code in {
                    "STALE_INPUT", "DELIVERY_DRAFT_CHANGED",
                    "SUGGESTION_UNAVAILABLE", "CANDIDATE_CHANGED",
                    "REVIEW_NOT_APPROVED",
                } else 400, code, message.strip()) from error
            return ApiResult(200, draft)
        if key == ("delivery-copy",):
            candidate = _require_candidate_binding(self.db, task_id, body)
            _require_gate(self.db, task_id, "human_reviewed", "REVIEW_NOT_APPROVED")
            try:
                draft = save_delivery_copy_draft(
                    self.db, task_id, candidate["candidate_id"],
                    expected_revision=body.get("expectedRevision"),
                    title=body.get("title"), description=body.get("description"),
                    tags=_string_list(body, "tags"),
                )
            except DeliveryCopyConflict as error:
                code, _, message = str(error).partition(":")
                raise ApiError(409, code, message.strip()) from error
            except ValueError as error:
                code, _, message = str(error).partition(":")
                raise ApiError(400, code, message.strip()) from error
            return ApiResult(200, draft)
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
            external_cover_signature = _optional_string(body, "externalCoverSignature")
            if external_cover_signature is not None:
                if cover_choice != "external" or not re.fullmatch(r"[a-f0-9]{64}", external_cover_signature):
                    raise ApiError(400, "INVALID_REQUEST", "外部封面图片指纹无效")
            output_root = _optional_string(body, "outputRoot")
            if output_root is not None:
                try:
                    output_root = str(resolve_delivery_root(output_root))
                except ValueError as error:
                    code, _, message = str(error).partition(":")
                    raise ApiError(409 if code == "DELIVERY_ROOT_UNAVAILABLE" else 400,
                                   code, message.strip()) from error
            legacy_platform = _optional_string(body, "platform")
            publish_payload = {
                "candidate_id": candidate["candidate_id"],
                **({"platform": legacy_platform} if legacy_platform is not None else {}),
                "title": _optional_string(body, "title"),
                "description": _optional_string(body, "description"),
                "tags": _string_list(body, "tags"),
                "cover_choice": cover_choice,
                "external_cover_path": external_cover_path,
                "expected_external_cover_signature": external_cover_signature,
                "delivery_type": delivery_type,
                "output_root": output_root,
            }
            retry_job_id = _optional_string(body, "retryJobId")
            if retry_job_id is not None:
                expected_payload = {
                    "action": "build_publish_package",
                    "task_id": task_id,
                    **publish_payload,
                }
                previous = matching_job_request(
                    self.db, retry_job_id, task_id,
                    "build_publish_package", expected_payload,
                )
                if previous is not None:
                    public = _public_job(self.db, previous)
                    if public["status"] in {"queued", "running"}:
                        return ApiResult(202, public)
                    if public["status"] in {"failed", "interrupted"}:
                        if not public["canResume"]:
                            raise ApiError(409, "DELIVERY_RETRY_UNAVAILABLE",
                                           "上次导出当前不可恢复，请先核对后台任务。")
                        try:
                            resume_job(self.db, retry_job_id)
                        except RuntimeError:
                            latest = _public_job(self.db, get_job(self.db, retry_job_id))
                            if latest["status"] not in {"queued", "running"}:
                                raise
                        return ApiResult(202, _public_job(self.db, get_job(self.db, retry_job_id)))
            return self._enqueue(task_id, "build_publish_package", publish_payload)
        raise ApiError(404, "NOT_FOUND", "未找到该接口。")

    def _task_detail(self, task: dict, *, projection: str | None = None) -> dict:
        from media_catalog.publish import DELIVER_ROOT
        task_id = task["task_id"]
        review_candidate = latest_review_candidate(self.db, task_id)
        candidates = list_candidates(self.db, task_id)
        current = candidates[0] if candidates and not candidates[0].get("invalidation") else None
        current_id = None if current is None else current["candidate_id"]
        candidate_ids = {item["candidate_id"] for item in candidates}
        from media_catalog.intelligent_review import intelligent_review_state
        intelligent_review = intelligent_review_state(self.db, task_id, current_id)
        content_reviews = [item for item in list_content_reviews(self.db, task_id)
                           if item["candidate_id"] in candidate_ids]
        content_review = next(
            (item for item in content_reviews if item["candidate_id"] == current_id), None,
        )
        content_takeovers = [item for item in (
            get_content_review_takeover(self.db, task_id, review["candidate_id"], review["run_id"])
            for review in content_reviews
        ) if item is not None]
        content_issue_decisions = [item for review in content_reviews
                                   for item in get_content_review_issue_decisions(
                                       self.db, task_id, review["run_id"])]
        publish_packages = workflow = public_jobs = rework_requests = None
        delivery = False
        if projection == "delivery":
            # The optional read projection never substitutes persisted task status
            # for current workflow, package bytes, jobs or pending rework checks.
            publish_packages = list_publish(self.db, task_id)
            workflow = project_workflow(self.db, task_id, publish_packages=publish_packages)
            public_jobs = [_public_job(self.db, item) for item in list_jobs(self.db, task_id=task_id)]
            rework_requests = list_rework_requests(self.db, task_id)
            delivery = bool(
                workflow["current_stage"] is None and workflow["run_status"] == "completed"
                and all(workflow["gates"].values())
                and current is not None and review_candidate is not None
                and review_candidate.get("candidate_id") == current_id
                and isinstance(current.get("output_signature"), dict)
                and current["output_signature"] == review_candidate.get("output_signature")
                == review_candidate.get("qa_output_signature")
                and any(item.get("candidate_id") == current_id
                        and item.get("package_status") == "current"
                        and item.get("is_current") is True and item.get("package_complete") is True
                        for item in publish_packages)
                and not any(item.get("status") in {
                    "queued", "running", "paused", "pending", "waiting_agent", "claimed"
                } for item in public_jobs)
                and not any(item.get("status") == "pending_confirmation" for item in rework_requests)
            )
            if delivery:
                from media_catalog.candidate_versions import resolve_candidate_media
                try:
                    # Package verification checks the exported copy; independently
                    # verify the exact original candidate used by the preview.
                    resolve_candidate_media(self.db, task_id, current_id)
                except (KeyError, RuntimeError, OSError, ValueError):
                    delivery = False
        if not delivery:
            raw_scene_cards = list_scene_cards(self.db, task_id)
            view_digest = picture_view_digest(self.db, task_id, raw_scene_cards)
            media_descriptions: dict[str, dict] = {}
            scene_cards = self._with_rough_media(task_id, raw_scene_cards, media_descriptions)
            for scene_card in scene_cards:
                scene_card["visual_analysis"] = visual_observation_for_shot(
                    self.db, scene_card
                )
            from media_catalog.rough_media import selected_rough_assets
            eligible_assets = self._with_rough_media(
                task_id,
                selected_rough_assets(self.db, task_id, raw_scene_cards),
                media_descriptions,
            )
            storyboard = project_script_shots(self.db, task_id, scene_cards)
            storyboard["digest"] = (
                storyboard_digest(self.db, task_id)
                if storyboard["status"] == "current" else None
            )
            storyboard["canUndo"] = can_undo_storyboard(self.db, task_id)
            storyboard["motionAdvisories"] = (
                project_motion_advisories(self.db, task_id, storyboard["groups"])
            )
            transcript_check = project_transcript_state(self.project_root, task)
            if transcript_check is not None and transcript_check["status"] not in {"pending", "stale"}:
                transcript_check["mediaUrl"] = (
                    f"/api/tasks/{task_id}/digital-source/media"
                )
            ad_voice_source = current_ad_voice_source(self.db, self.project_root, task, projection_only=True)
            ad_voice = ad_voice_source.path
            actual_duration = (
                measured_voice_duration(ad_voice) if task["task_type"] == "ad_mix"
                else current_digital_duration(task)
            )
            if publish_packages is None:
                publish_packages = list_publish(self.db, task_id)
                workflow = project_workflow(self.db, task_id, publish_packages=publish_packages)
            from media_catalog.workflow import apply_transcript_attention
            workflow = apply_transcript_attention(workflow, task["task_type"], transcript_check)
            from media_catalog.storyboard_refresh import rough_refresh_blocker
            storyboard["canRefreshRoughMaterials"] = (
                task["task_type"] == "ad_mix" and bool(storyboard.get("digest"))
                and workflow["current_stage"] in {"rough_cut", "picture_order", "picture_lock"}
                and rough_refresh_blocker(self.db, task_id) is None
            )
        return {
            "task": task,
            "finalScriptText": _read_task_script(self.project_root, task),
            **({"detailProjection": "delivery"} if delivery else {
                "adVoiceMediaUrl": (
                    f"/api/tasks/{quote(task_id, safe='')}/script-voice/media"
                    if ad_voice is not None else None
                ),
                "adVoiceSource": ad_voice_source.public_payload(),
                "actualDurationSeconds": actual_duration,
                "transcriptCheck": transcript_check,
                "sceneCards": scene_cards,
                "pictureViewDigest": view_digest,
                "storyboard": storyboard,
                "eligibleAssets": eligible_assets,
                "eligibleAssetsDeferred": True,
                "packagingAudio": packaging_audio_state(self.db, self.project_root, task_id, verify_content=False),
                "packagingSuggestion": {**packaging_suggestion_state(self.db, task_id), **packaging_suggestion_availability(self.db, self.project_root, task_id)},
                "packagingPlans": _packaging_plan_views(self.db, task_id),
            }),
            "workflow": workflow,
            "preCandidateScriptReworkAvailable": (
                current is None and pre_candidate_rework_available(self.db, task_id)
            ),
              "sources": get_task_sources(self.db, task_id),
              "initialSourceCheck": get_initial_source_check(self.db, task_id),
            "modelBindings": list_task_profile_bindings(self.db, task_id),
            "materialAuthorization": get_material_authorization(self.db, task_id),
            "reviewCandidate": _legacy_review_candidate(review_candidate),
            "candidateVersions": [
                _public_candidate(item, task_id, current_id)
                for item in candidates
            ],
            "currentCandidate": (
                None if current is None else _public_candidate(current, task_id, current_id)
            ),
            "deliveryCopyDraft": (
                None if current_id is None else get_delivery_copy_draft(self.db, task_id, current_id)
            ),
            "intelligentReview": intelligent_review,
            "contentReview": content_review,
            "contentReviews": content_reviews,
            "contentReviewTakeover": next(
                (item for item in content_takeovers if item["candidate_id"] == current_id), None,
            ),
            "contentReviewTakeovers": content_takeovers,
            "contentReviewIssueDecisions": content_issue_decisions,
            "reworkRequests": [
                _public_rework(item) for item in (
                    list_rework_requests(self.db, task_id) if rework_requests is None else rework_requests)
            ],
            "reviewIssues": [
                _public_review_issue(item)
                for candidate in candidates
                for item in list_review_issues(self.db, task_id, candidate["candidate_id"])
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
            "deliveryOutputRoot": str(DELIVER_ROOT.resolve()),
            "publishPackages": [
                _public_publish(item) for item in publish_packages
            ],
            "jobs": ([_public_job(self.db, item) for item in list_jobs(self.db, task_id=task_id)]
                     if public_jobs is None else public_jobs),
        }

    def _with_rough_media(
        self, task_id: str, items: list[dict], descriptions: dict[str, dict] | None = None,
    ) -> list[dict]:
        # Share only within this task-detail request; the next request rechecks files.
        if descriptions is None:
            descriptions = {}
        projected = []
        for item in items:
            copy = dict(item)
            asset_id = copy.get("asset_id")
            if asset_id:
                asset_key = str(asset_id)
                if asset_key not in descriptions:
                    descriptions[asset_key] = describe_rough_media(self.db, task_id, asset_key)
                copy.update(descriptions[asset_key])
            if copy.get("card_id"):
                copy.update(_rough_source_projection(copy))
            projected.append(copy)
        return projected

    def _compose_packaging(self, task: dict, body: dict) -> dict:
        task_id = task["task_id"]
        clone_from = _optional_string(body, "cloneFrom")
        plan_id = _optional_string(body, "planId")
        if clone_from:
            cloned = clone_plan(
                self.db, clone_from, task_id=task_id,
                plan_name=_optional_string(body, "planName"),
                include_instance_map=True,
            )
            return {"planId": cloned["plan_id"], "status": "draft",
                    "plan": get_plan(self.db, cloned["plan_id"]),
                    "instanceMap": cloned["instance_map"]}
        if plan_id:
            _owned_plan(self.db, task_id, plan_id)
            revision = body.get("expectedRevision")
            if type(revision) is not int or revision < 0:
                raise ApiError(400, "PACKAGING_REVISION_INVALID", "请提供当前包装方案版本。")
            slot = _required_string(body, "slot")
            if body.get("enabled") is False:
                return disable_optional_slot(self.db, plan_id, slot, expected_revision=revision)
            asset_ref = _required_string(body, "assetRef")
            category = "title_card" if slot in {"brand", "info_card"} else slot
            manifest_item = find_item(category, asset_ref, self.db)
            if manifest_item is None:
                raise ApiError(400, "PACKAGING_MANIFEST_UNKNOWN", "所选包装素材不存在，请重新选择。")
            routes = manifest_item.get("default_for") or []
            if routes and get_plan(self.db, plan_id)["route"] not in routes:
                raise ApiError(409, "PACKAGING_NOT_APPLICABLE", "所选包装素材不适用于本片，请重新选择。")
            return replace_item(
                self.db, plan_id, slot, asset_ref,
                asset_file=_optional_string(body, "assetFile"),
                asset_version=_optional_string(body, "assetVersion"),
                notes=_optional_string(body, "notes"),
                expected_revision=revision,
            )
        plan_name = _optional_string(body, "planName") or "自选组合"
        new_id = create_blank_plan(self.db, task_id, None, plan_name)
        return {"planId": new_id, "status": "draft", "plan": get_plan(self.db, new_id)}

    def _enqueue(
        self,
        task_id: str,
        kind: str,
        payload: dict,
        *,
        cost_status: str = "unknown",
        provider_request_id: str | None = None,
        job_id: str | None = None,
        pre_enqueue_check=None,
    ) -> ApiResult:
        stable_job_id = job_id
        job_id = job_id or f"job-{uuid.uuid4().hex}"
        try:
            actual_id = enqueue_job(
                self.db,
                job_id=job_id,
                job_type=kind,
                task_id=task_id,
                payload={"action": kind, "task_id": task_id, **payload},
                cost_status=cost_status,
                provider_request_id=provider_request_id,
                pre_enqueue_check=pre_enqueue_check,
            )
        except ValueError:
            # A simultaneous confirmation may have queued the same bound request.
            existing = get_job(self.db, job_id) if stable_job_id else None
            if existing is None or existing["task_id"] != task_id or existing["job_type"] != kind:
                raise
            actual_id = job_id
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
    from media_catalog.digital_composition import PIP_POSITION
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
        resolved_start = (seconds_to_us(narrative_start) * 5 // 4) / 1_000_000
        resolved_end = (seconds_to_us(narrative_end) * 5 // 4) / 1_000_000
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
        "preview_position": dict(PIP_POSITION) if card.get("mix_form") in {"画中画", "信息卡"} else None,
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
            if prefix == "PACKAGING_FILE_MISSING":
                return ApiError(409, prefix, "包装素材文件缺失，请恢复素材文件或替换缺失的包装素材后重试。")
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
    current = candidates[0] if candidates and not candidates[0].get("invalidation") else None
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
        "invalidation": candidate.get("invalidation"),
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


def _public_review_issue(value: dict) -> dict:
    return {
        "issueId": value.get("issue_id"),
        "candidateId": value.get("candidate_id"),
        "category": value.get("category"),
        "notes": value.get("notes"),
        "positionSeconds": value.get("position_seconds"),
        "status": value.get("status"),
        "createdAt": value.get("created_at"),
        "withdrawnAt": value.get("withdrawn_at"),
    }


def _review_issue_api_error(error: Exception) -> ApiError:
    code, _, message = str(error).strip("'\"").partition(":")
    status = 404 if code == "REVIEW_ISSUE_NOT_FOUND" else (
        409 if code in {
            "REVIEW_ISSUE_CANDIDATE_STALE", "REVIEW_ISSUE_CANDIDATE_CLOSED",
            "REVIEW_ISSUE_MEDIA_CHANGED", "REVIEW_ISSUE_REQUEST_CONFLICT",
        } else 400
    )
    return ApiError(status, code, message.strip() or "审片问题操作失败。")


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
        "missingFiles": list(value.get("missing_files") or []),
        "changedFiles": list(value.get("changed_files") or []),
        "coverPath": value.get("cover_path"),
        "coverChoice": value.get("cover_choice"),
        "coverSignature": value.get("cover_signature"),
        "deliveryType": value.get("delivery_type"),
        "deliveryVersion": value.get("delivery_version"),
        "createdAt": value.get("created_at"),
    })
    return result


def _owned_plan(db, task_id: str, plan_id: str) -> dict:
    plan = get_plan(db, plan_id)
    if plan is None or plan["task_id"] != task_id:
        raise ApiError(404, "NOT_FOUND", "包装方案不存在。")
    return plan


_RENDER_FAILURE_COPY = {
    "ACTION_OUTPUT_CHANGED": "成片文件与原记录不一致，系统已停止复用。",
    "ACTION_DEPENDENCY_CHANGED": "锁定输入或素材依赖发生变化，系统已停止复用。",
    "ACTION_CANDIDATE_CHANGED": "当前候选已变化，旧作业不能继续覆盖。",
    "RENDER_OUTPUT_MISSING": "已登记的成片文件无法读取。",
    "RENDER_OUTPUT_EXISTS": "候选目录已有未绑定文件，系统已停止覆盖。",
    "RENDER_OUTPUT_INVALID": "生成的成片文件未通过有效性检查。",
    "RENDER_OUTPUT_CHANGED": "生成后的成片文件发生变化。",
    "RENDER_OUTPUT_CHANGED_DURING_QA": "自动检查期间成片文件发生变化。",
    "DEPENDENCY_SNAPSHOT_CHANGED": "成片依赖的素材或运行文件发生变化。恢复原依赖后可继续；若已升级，请在当前阶段记录技术返工，选择重新生成并质检。",
    "CATALOG_SNAPSHOT_CHANGED": "任务素材或锁定选择发生变化。",
    "PROCESS_START_FAILED": "成片生成进程未能启动。",
    "PROCESS_FAILED": "成片生成进程执行失败。",
    "PROCESS_TIMEOUT": "成片生成进程超时。",
    "PICTURE_LOCK_MISSING": "画面锁定已失效，需要重新确认。",
    "PACKAGING_PLAN_MISSING": "包装方案未确认或已失效。",
    "PACKAGING_ASSET_MISSING": "已确认的包装素材无法读取。",
    "AD_SCENE_ASSET_OFFLINE": "已锁定的广告镜头素材当前离线。",
    "AD_RENDER_DEPENDENCY_MISSING": "广告成片依赖的运行文件缺失。",
    "STORYBOARD_REQUIRED": "旧版转写或分镜文件缺失。请先恢复任务数据；重复重试不会补回文件。",
    "TRANSCRIPT_TIMING_MISSING": "字幕时间轴文件缺失，成片生成已停止。请先恢复口播识别结果，再重试。",
    "SUBTITLE_SEMANTICS_MISSING": "字幕重点词记录缺失，成片生成已停止。请先恢复字幕数据，再重试。",
    "QA_REPORT_MISSING": "自动检查报告缺失。",
    "QA_RETRY_INPUT_CHANGED": "原成片生成条件已变化，不能仅重做质检。请在当前阶段记录技术返工，选择重新生成并质检。",
}


def _render_failure(job: dict, status: str) -> tuple[str | None, str | None]:
    if status not in {"failed", "interrupted"}:
        return None, None
    summary = str(job.get("error_summary") or "")
    match = re.match(
        r"^(?:[A-Za-z][A-Za-z0-9_]*Error:\s*)?([A-Z][A-Z0-9_]{2,80}):",
        summary,
    )
    code = match.group(1) if match and match.group(1) in _RENDER_FAILURE_COPY else None
    problem = _RENDER_FAILURE_COPY[code] if code else (
        "后台处理意外中断，详细诊断保留在本机作业记录。"
        if status == "interrupted"
        else "后台处理失败，详细诊断保留在本机作业记录。"
    )
    return code, problem


_DELIVERY_FAILURE_COPY = {
    "PUBLISH_WORKFLOW_BLOCKED": "导出前校验未通过，无法核验成片与当前任务记录一致。原成片已保留，请查看处理详情。",
    "DEPENDENCY_SNAPSHOT_CHANGED": "已审片的输入依赖校验未通过。原成片已保留，请查看处理详情；当前提示不能确定具体哪项依赖变化。",
    "DELIVERY_ROOT_UNAVAILABLE": "所选交付目录当前不可访问，请恢复连接后继续上次导出。",
    "DELIVERY_SPACE_INSUFFICIENT": "交付目录剩余空间不足，请释放空间后继续上次导出。",
    "DELIVERY_STAGE_CHANGED": "导出临时文件已变化，请核对本机目录后重新导出。",
    "PUBLISH_PACKAGE_CHANGED": "交付文件或清单已变化，请核对本机目录后重新导出。",
    "DELIVERY_IO_FAILED": "本地交付文件读写失败，请检查交付目录、源文件、空间和权限后继续上次导出。",
}


def _delivery_failure(job: dict, status: str) -> tuple[str | None, str | None]:
    if status not in {"failed", "interrupted"}:
        return None, None
    summary = str(job.get("error_summary") or "")
    match = re.match(
        r"^(?:[A-Za-z][A-Za-z0-9_]*Error:\s*)?([A-Z][A-Z0-9_]{2,80}):",
        summary,
    )
    code = match.group(1) if match and match.group(1) in _DELIVERY_FAILURE_COPY else None
    if code is None and re.match(
        r"^(?:OSError|FileNotFoundError|PermissionError|IsADirectoryError):", summary
    ):
        code = "DELIVERY_IO_FAILED"
    problem = _DELIVERY_FAILURE_COPY[code] if code else (
        "导出作业意外中断，可检查目录后继续上次导出。"
        if status == "interrupted"
        else "导出未完成，请查看本机作业诊断后继续上次导出。"
    )
    return code, problem


_GENERATION_FAILURE_COPY = {
    'MODEL_OUTPUT_INVALID': '模型已返回内容，但分镜方案未通过格式或原文分组校验，未应用到本片；不会自动重复调用。',
    'MODEL_OUTPUT_EMPTY': '模型未返回可用的分镜方案，未应用到本片；不会自动重复调用。',
    'MODEL_OUTPUT_TRUNCATED': '模型返回的分镜方案不完整，未应用到本片；不会自动重复调用。',
    'AD_HIGHLIGHT_INVALID': '分镜结果的可选字幕重点词未通过原文校验。这不是配音读法错误；原文已保留。',
    'AD_COPY_COVERAGE_INVALID': '分镜没有完整覆盖已确认文案，未采用本次结果；原文已保留。',
    'TTS_AUDIO_DECODE_FAILED': '配音服务已返回音频，但本机解码失败，未应用到本片；不会自动重试。',
    'TTS_PROVIDER_REJECTED': '配音服务拒绝了本次请求，请检查服务配置及额度。',
    'TTS_HTTP_REJECTED': '配音服务请求未成功，请检查服务配置和连接。',
    'VOICE_GENERATION_FAILED': '配音生成失败，未应用到本片；请查看本机诊断记录。',
}


def _generation_failure(db, job, status):
    if status not in {'failed', 'interrupted', 'cancelled'}: return None, None
    summary = str(job.get('error_summary') or '')
    # Recognize only known codes behind the existing process error wrapper.
    match = re.match(r'^(?:[A-Za-z][A-Za-z0-9_]*Error:\s*)?(?:PROCESS_FAILED:\s*)?(?:✘ 规划请求失败或输出无效（未自动重试或付费修复）:\s*)?([A-Z][A-Z0-9_]{2,80})(?::|$)', summary.strip())
    code = match.group(1) if match and match.group(1) in _GENERATION_FAILURE_COPY else None
    if job['job_type'] == 'tts':
        from media_catalog.voice_bundle_service import voice_generation_failure_for_job
        code = voice_generation_failure_for_job(db, job['job_id']) or code
    return code, _GENERATION_FAILURE_COPY.get(code, '本次分镜或配音处理已停止，详细诊断保留在本机；不会自动重试。')


def _public_job(db, job: dict | None) -> dict:
    if job is None:
        raise ApiError(404, "NOT_FOUND", "后台任务不存在。")
    status = job.get("public_status") or job["status"]
    paused = status == "paused"
    can_resume = can_resume_job(db, job["job_id"])
    # Cancellation closes the local workflow; outcomeStatus still preserves the
    # original provider audit uncertainty in job details.
    manual_check = status != "cancelled" and (
        (job.get("outcome_status") == "unknown" and not can_resume)
        or (status in {"failed", "interrupted", "paused"} and not can_resume)
    )
    failure_code, failure_problem = (
        _render_failure(job, status)
        if job["job_type"] == "render_and_qa"
        else _delivery_failure(job, status)
        if job["job_type"] == "build_publish_package"
        else _generation_failure(db, job, status)
        if job["job_type"] in {"recommend_storyboard", "tts"}
        else (None, None)
    )
    requires_input_review = False
    retry_strategy = None
    if job["job_type"] == "render_and_qa" and status in {"failed", "interrupted", "paused"}:
        retry_strategy = (
            "请先人工核对本机作业记录，再决定是否重新发起处理。"
            if manual_check
            else "可从安全检查点继续。系统先核验已生成成片的内容签名和依赖；一致时仅重做自动检查。"
        )
    if job["job_type"] == "build_publish_package" and status in {"failed", "interrupted"}:
        retry_strategy = (
            "请先核对本机作业记录，再决定是否重新发起导出。"
            if manual_check
            else "可继续上次导出；系统核验已完成文件，只补失败部分。修改导出设置会创建新作业。"
        )
    if job['job_type'] == 'source_continue' and status in {'failed', 'interrupted'}:
        reasons = {
            'SOURCE_SYNC_REQUIRED': '素材已变化，本次未确认分镜。请点击“查看素材变化”，核对后明确同步。',
            'SOURCE_CONTINUE_STALE': '分镜或素材范围已变化，请刷新后重新查看并确认。',
            'SOURCE_CHECK_PENDING': '创建时的素材检查尚未完成，请等待原作业。',
            'SOURCE_CHECK_FAILED': '创建时的素材检查失败，请先处理原作业。',
            'STORYBOARD_CHANGED': '分镜已变化，请重新查看当前画面安排，再生成粗剪。',
            'STORYBOARD_GAP': '尚有画面未匹配。请逐段补齐下方待补画面，再生成粗剪。',
            'STORYBOARD_COVERAGE': '有文案段缺少镜头安排，请查看并补齐待补画面。',
            'STORYBOARD_STALE': '当前文案的分镜尚未就绪，请返回分镜核对。',
        }
        match = re.match(r'^(?:(?:RuntimeError|ValueError):\s*)?([A-Z_]+):', job.get('error_summary') or '')
        failure_code = match.group(1) if match and match.group(1) in reasons else None
        failure_problem = reasons.get(failure_code, '素材核验未完成，未确认分镜；请查看作业记录后重试。')
        requires_input_review = (
            failure_code in {'SOURCE_SYNC_REQUIRED', 'SOURCE_CONTINUE_STALE',
                             'STORYBOARD_CHANGED', 'STORYBOARD_GAP',
                             'STORYBOARD_COVERAGE', 'STORYBOARD_STALE'}
            and job.get('is_paid') == 0 and job.get('outcome_status') != 'unknown'
        )
        if requires_input_review:
            # These are deterministic input checks, not uncertain provider work.
            # A reviewed current page starts a new request; never replay old inputs.
            manual_check = False
            can_resume = False
    candidate_id = None
    if job['job_type'] == 'content_review':
        from media_catalog.content_review_jobs import content_review_job_candidate_id
        candidate_id = content_review_job_candidate_id(db, job['job_id'], job['task_id'])
    from .voice_progress import voice_progress_for_job
    voice_progress = voice_progress_for_job(db, job)
    from .material_progress import material_progress_for_job
    material_progress = material_progress_for_job(db, job)
    timing = job_timing(db, job)
    return {
        "jobId": job["job_id"],
        "kind": job["job_type"],
        "candidateId": candidate_id,
        "taskId": job["task_id"],
        "libraryId": job.get("library_id"),
        "status": status,
        "progress": {"current": job["progress_current"], "total": job["progress_total"]},
        "phase": job["phase_label"],
        **(material_progress or {}),
        "queuePosition": job.get("queue_position"),
        "canPause": status in {"queued", "running"} and not job.get("pause_requested"),
        "pauseRequested": bool(job.get("pause_requested")),
        "canCancel": status in {"queued", "running", "paused", "interrupted"},
        "canResume": can_resume,
        "needsManualCheck": manual_check,
        "outcomeStatus": job.get("outcome_status"),
        "problem": failure_problem or ("任务未完成，请按下一步处理。" if status in {"failed", "interrupted"} else None),
        "failureCode": failure_code,
        "requiresInputReview": requires_input_review,
        **({"voiceGeneration": voice_progress} if voice_progress is not None else {}),
        **({"providerDiagnostics": job["providerDiagnostics"]} if job.get("providerDiagnostics") else {}),
        "retryStrategy": retry_strategy,
        "nextAction": "manual_check" if manual_check else ("resume" if can_resume else None),
        "estimatedStartAt": timing["estimated_start_at"],
        "estimatedRemainingSeconds": timing["estimated_remaining_seconds"],
        "estimateStatus": timing["estimate_status"],
        "elapsedSeconds": timing["elapsed_seconds"],
        "estimateBasis": timing["estimate_basis"],
        "heartbeatExpired": timing["heartbeat_expired"],
        "observedAt": timing["observed_at"],
        "createdAt": job["created_at"],
        "queueSequence": job.get("queue_sequence"),
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
            "timelineMap": [],
            "durationSeconds": None,
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
        "audioAdvice": preview.get("audio_advice") if status == "ready" else None,
        "coverage": list(preview.get("coverage") or []),
        "timelineMap": list(preview.get("timeline_map") or []),
        "durationSeconds": preview.get("duration_seconds"),
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


def _packaging_editor_style(item: dict) -> dict:
    style = {k: item[k] for k in ("primary_hex", "highlight_hex", "outline_hex", "background_hex", "accent_hex", "size", "fontname_guess", "fill", "stroke_hex", "bg_hex", "back_hex", "bold") if k in item}
    if item.get("design_version") == "refined-v1":
        style["design_version"] = "refined-v1"
    weight = item.get("font_weight")
    if type(weight) is int and 100 <= weight <= 900:
        style["font_weight"] = weight
    # Expose only the existing manifest's bounded visual values to the editor.
    glow = item.get("glow")
    if isinstance(glow, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", glow):
        style["glow"] = glow
    if isinstance(item.get("shadow"), bool):
        style["shadow"] = item["shadow"]
    return style


_PACKAGING_CATEGORIES = (
    ("subtitle", "字幕", "subtitle"),
    ("voice", "音色", "voice"),
    ("bgm", "BGM", "bgm"),
    ("sfx", "音效", "sfx"),
    ("title_card", "文字卡", "title_card"),
    ("info_card", "信息卡", "title_card"),
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
    "info_card": ("visual-cards", "visual-cards"),
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
        manifest = load_manifest(manifest_key, db)
        assets = []
        for item in manifest.get("items", []):
            if item.get("_category") and key != item["_category"]:
                continue
            if key == "brand" and not _looks_like_brand(item):
                continue
            if key in {"title_card", "info_card"} and _looks_like_brand(item):
                continue
            license_name = item.get("license")
            try:
                path = resolve_manifest_asset_path(manifest_key, str(item.get("id") or ""), db)
            except RuntimeError:
                path = Path(item["_local_path"]) if item.get("_local_path") else None
            suffix = "" if path is None else path.suffix.lower()
            preview_kind = (
                "audio" if suffix in {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus"}
                else "image" if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                else "video" if suffix in {".mp4", ".mov", ".webm"}
                else None
            )
            availability = (
                "external" if path is None
                else "available" if path.is_file() and item.get("local_available", True)
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
                "personal": bool(item.get("_personal")),
                "resourceCategory": item.get("_category", key),
                "style": _packaging_editor_style(item),
                "fontUrl": (f"/api/packaging-assets/{key}/{asset_ref}/preview" if suffix in {".ttf", ".otf", ".woff", ".woff2"} and availability == "available" else None),
                "assetRef": asset_ref,
                "name": str(item.get("name") or item.get("id") or "未命名素材"),
                "authorizationStatus": (
                    "approved"
                    if license_name == "commercial-approved"
                    else "review_required"
                ),
                "license": str(license_name or "未记录"),
                "availabilityStatus": availability,
                **({"defaultGain": item.get("default_gain", 0.18)} if key == "bgm" else {}),
                **({"durationSeconds": float(item["duration_s"])
                    if availability == "available"
                    and isinstance(item.get("duration_s"), (int, float))
                    and not isinstance(item["duration_s"], bool)
                    and math.isfinite(item["duration_s"])
                    and item["duration_s"] > 0 else None} if key == "sfx" else {}),
                "relativePath": relative_path,
                "sourceLayer": "local" if item.get("_local_root") else source_layer,
                "routes": item.get("default_for") or [],
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
