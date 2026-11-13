"""Strict, credential-free façade used by the optional 剪辑工作台 MCP server."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from .ad_workflow import create_ad_task, preflight_ad_task
from .ad_actions import _validate_ad_plan
from .agent_artifacts import (
    VALID_ARTIFACT_TYPES,
    list_agent_artifacts,
    save_agent_artifact,
)
from .agent_context import build_agent_context, current_agent_input_digest
from .digital_workflow import create_digital_task, preflight_digital_task
from .planning_finalize import (
    finalize_digital_storyboard,
    validate_digital_storyboard_content,
)
from .scene_cards import list_scene_cards
from .task_sources import list_eligible_assets
from .tasks import get_task, list_tasks
from .workbench_worker import (
    enqueue_job,
    get_job,
    list_jobs,
    request_cancel,
    resume_job,
)
from .workflow import project_workflow


ALLOWED_ACTIONS = frozenset(
    {
        "sync_sources",
        "recommend_storyboard",
        "render_and_qa",
        "build_publish_package",
    }
)
PAID_ACTIONS = frozenset({"recommend_storyboard"})
_MAX_CONTENT_BYTES = 1024 * 1024
_MAX_STRING_LENGTH = 20_000
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|token|credential|cookie|password|secret)",
    re.IGNORECASE,
)


def _success(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _failure(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    next_action: str | None = None,
    fields: list[str] | None = None,
) -> dict:
    return {
        "ok": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "nextAction": next_action,
            "fields": fields or [],
        },
    }


def _error_code(error: BaseException, default: str) -> str:
    text = str(error)
    prefix = text.partition(":")[0].strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", prefix):
        return prefix
    return default


def _safe_job(job: dict) -> dict:
    can_resume = (
        job["status"] in {"failed", "interrupted"}
        and job.get("retry_policy") != "never"
        and job.get("outcome_status") != "unknown"
    )
    manual_check = (
        job.get("outcome_status") == "unknown"
        or (job["status"] in {"failed", "interrupted"} and not can_resume)
    )
    return {
        "jobId": job["job_id"],
        "kind": job["job_type"],
        "taskId": job["task_id"],
        "status": job["status"],
        "progress": {
            "current": job["progress_current"],
            "total": job["progress_total"],
        },
        "phase": job["phase_label"],
        "queuePosition": job.get("queue_position"),
        "canCancel": job["status"] in {"queued", "running", "interrupted"},
        "canResume": can_resume,
        "needsManualCheck": manual_check,
        "nextAction": (
            "manual_check" if manual_check else "resume" if can_resume else None
        ),
        "updatedAt": job["updated_at"],
    }


def _validate_content_shape(value, *, path: str = "content", depth: int = 0) -> None:
    if depth > 30:
        raise ValueError("AGENT_CONTENT_INVALID: Agent 产物嵌套过深")
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH:
            raise ValueError(
                f"AGENT_STRING_TOO_LONG: {path} 超过 {_MAX_STRING_LENGTH} 个字符"
            )
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_content_shape(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("AGENT_CONTENT_INVALID: Agent 产物字段名必须是文字")
            if _SENSITIVE_KEY.search(key):
                raise ValueError(
                    "AGENT_SENSITIVE_FIELD_REJECTED: Agent 产物不能包含敏感字段"
                )
            _validate_content_shape(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
            )
        return
    raise ValueError("AGENT_CONTENT_INVALID: Agent 产物包含不支持的数据类型")


def _asset_references(value) -> set[str]:
    result: set[str] = set()
    if isinstance(value, list):
        for item in value:
            result.update(_asset_references(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"assetId", "asset_id"} and isinstance(item, str):
                result.add(item)
            result.update(_asset_references(item))
    return result


class AgentGateway:
    def __init__(
        self,
        db,
        project_root: Path | str,
        workbench_origin: str = "http://127.0.0.1:8765",
    ) -> None:
        self.db = db
        self.project_root = Path(project_root).resolve()
        self.workbench_origin = workbench_origin.rstrip("/")

    def _guard(self, operation):
        try:
            return _success(operation())
        except KeyError:
            return _failure("TASK_NOT_FOUND", "请求的任务或记录不存在。")
        except (ValueError, RuntimeError) as error:
            return _failure(
                _error_code(error, "INVALID_REQUEST"),
                str(error),
            )
        except Exception:
            return _failure(
                "INTERNAL_ERROR",
                "剪辑工作台 处理失败，请在工作台查看诊断信息。",
                retryable=True,
            )

    def _require_agent_task(self, task_id: str) -> dict:
        task = get_task(self.db, task_id)
        if task is None:
            raise KeyError(task_id)
        if task["orchestration_mode"] != "agent":
            raise ValueError("AGENT_MODE_REQUIRED: 任务不是 Agent 编排模式")
        return task

    def get_system_status(self) -> dict:
        def operation():
            schema = self.db.connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
            ).fetchone()[0]
            queue = self.db.connection.execute(
                "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
            return {
                "service": "cutroom",
                "database": "available",
                "schemaVersion": schema,
                "queue": {row["status"]: row["count"] for row in queue},
                "routes": ["ad_mix", "digital_human"],
                "agentActions": sorted(ALLOWED_ACTIONS),
            }

        return self._guard(operation)

    def list_tasks(self, limit: int = 50) -> dict:
        def operation():
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
                raise ValueError("LIMIT_INVALID: limit 必须在 1 到 100 之间")
            result = []
            for task in list_tasks(self.db, limit=limit):
                workflow = project_workflow(self.db, task["task_id"])
                result.append(
                    {
                        "taskId": task["task_id"],
                        "title": task["title"],
                        "route": task["task_type"],
                        "orchestrationMode": task["orchestration_mode"],
                        "currentStage": workflow["current_stage"],
                        "runStatus": workflow["run_status"],
                        "nextAction": workflow["next_action"],
                        "updatedAt": task["updated_at"],
                    }
                )
            return result

        return self._guard(operation)

    def get_task(self, task_id: str) -> dict:
        def operation():
            task = get_task(self.db, task_id)
            if task is None:
                raise KeyError(task_id)
            workflow = project_workflow(self.db, task_id)
            return {
                "task": {
                    "taskId": task["task_id"],
                    "title": task["title"],
                    "route": task["task_type"],
                    "orchestrationMode": task["orchestration_mode"],
                    "status": task["status"],
                },
                "workflow": workflow,
                "sceneCardCount": len(list_scene_cards(self.db, task_id)),
                "jobs": [
                    _safe_job(item)
                    for item in list_jobs(self.db, task_id=task_id, limit=20)
                ],
            }

        return self._guard(operation)

    def get_agent_context(self, task_id: str) -> dict:
        return self._guard(
            lambda: build_agent_context(self.db, task_id, self.project_root)
        )

    def get_job(self, job_id: str) -> dict:
        def operation():
            job = get_job(self.db, job_id)
            if job is None:
                raise KeyError(job_id)
            return _safe_job(job)

        return self._guard(operation)

    def get_result_links(self, task_id: str) -> dict:
        def operation():
            self._require_agent_task(task_id)
            safe_id = quote(task_id, safe="")
            task_root = (
                self.project_root
                / "runtime"
                / "workbench"
                / "tasks"
                / task_id
            ).resolve()
            out_root = (self.project_root / "out" / task_id).resolve()
            roots = [task_root, out_root]
            files = []
            for root in roots:
                if not root.is_dir():
                    continue
                for path in sorted(root.rglob("*")):
                    if not path.is_file() or path.suffix.lower() not in {
                        ".mp4",
                        ".json",
                        ".html",
                        ".md",
                    }:
                        continue
                    try:
                        path.resolve().relative_to(root)
                    except ValueError:
                        continue
                    files.append(str(path.resolve()))
                    if len(files) >= 20:
                        break
            return {
                "taskUrl": f"{self.workbench_origin}/#/tasks/{safe_id}",
                "files": files,
            }

        return self._guard(operation)

    def preflight_task(self, request: dict) -> dict:
        def operation():
            if not isinstance(request, dict):
                raise ValueError("INVALID_REQUEST: request 必须是对象")
            payload = dict(request)
            if payload.get("orchestration_mode", "agent") != "agent":
                raise ValueError(
                    "AGENT_MODE_REQUIRED: MCP 只创建或预检 Agent 模式任务"
                )
            payload["orchestration_mode"] = "agent"
            if payload.get("route") == "ad_mix":
                result = preflight_ad_task(self.db, payload, self.project_root)
            elif payload.get("route") == "digital_human":
                result = preflight_digital_task(
                    self.db,
                    payload,
                    self.project_root,
                )
            else:
                raise ValueError("UNSUPPORTED_ROUTE: Agent 任务路线不受支持")
            return {
                "route": result.get("route"),
                "checks": result.get("checks", []),
                "blocking": result.get("blocking", []),
                "warnings": result.get("warnings", []),
                "estimatedSpaceBytes": result.get("estimated_space_bytes"),
                "nextAction": result.get("next_action"),
                "confirmations": result.get("confirmations", []),
            }

        return self._guard(operation)

    def create_task(self, request: dict) -> dict:
        def operation():
            if not isinstance(request, dict):
                raise ValueError("INVALID_REQUEST: request 必须是对象")
            payload = dict(request)
            if payload.get("orchestration_mode", "agent") != "agent":
                raise ValueError(
                    "AGENT_MODE_REQUIRED: MCP 只允许创建 Agent 模式任务"
                )
            payload["orchestration_mode"] = "agent"
            if payload.get("route") == "ad_mix":
                created = create_ad_task(self.db, payload, self.project_root)
            elif payload.get("route") == "digital_human":
                created = create_digital_task(self.db, payload, self.project_root)
            else:
                raise ValueError("UNSUPPORTED_ROUTE: Agent 任务路线不受支持")
            task = created["task"]
            return {
                "taskId": created["task_id"],
                "route": task["task_type"],
                "orchestrationMode": task["orchestration_mode"],
                "workflow": project_workflow(self.db, created["task_id"]),
            }

        return self._guard(operation)

    def submit_planning_artifact(
        self,
        *,
        task_id: str,
        artifact_type: str,
        schema_version: int,
        input_digest: str,
        content: dict,
        source_agent: str = "codex",
        source_model_label: str | None = None,
    ) -> dict:
        def operation():
            task = self._require_agent_task(task_id)
            if artifact_type not in VALID_ARTIFACT_TYPES:
                raise ValueError(
                    "AGENT_ARTIFACT_TYPE_INVALID: Agent 产物类型不受支持"
                )
            expected_type = (
                "ad_storyboard"
                if task["task_type"] == "ad_mix"
                else "digital_storyboard"
                if task["task_type"] == "digital_human"
                else None
            )
            if artifact_type != expected_type:
                raise ValueError(
                    "AGENT_ARTIFACT_ROUTE_MISMATCH: 产物类型与任务路线不匹配"
                )
            current_digest = current_agent_input_digest(
                self.db,
                task_id,
                self.project_root,
            )
            if input_digest != current_digest:
                raise ValueError(
                    "AGENT_INPUT_STALE: 文案或素材范围已变化，请重新读取上下文"
                )
            if not isinstance(content, dict):
                raise ValueError("AGENT_CONTENT_INVALID: Agent 产物必须是对象")
            serialized = json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if len(serialized) > _MAX_CONTENT_BYTES:
                raise ValueError(
                    "AGENT_CONTENT_TOO_LARGE: Agent 产物不能超过 1 MiB"
                )
            _validate_content_shape(content)
            eligible_ids = {
                item["asset_id"]
                for item in list_eligible_assets(self.db, task_id)
            }
            invalid_assets = sorted(_asset_references(content) - eligible_ids)
            if invalid_assets:
                raise ValueError(
                    "AGENT_ASSET_OUT_OF_SCOPE: 产物引用了任务范围外素材"
                )

            context = build_agent_context(self.db, task_id, self.project_root)
            if artifact_type == "digital_storyboard":
                validate_digital_storyboard_content(
                    content,
                    context["script"]["text"],
                )
            else:
                plan = content.get("plan")
                if not isinstance(plan, dict):
                    raise ValueError(
                        "AGENT_CONTENT_INVALID: 广告产物必须包含 plan 对象"
                    )
                _validate_ad_plan(
                    context["script"]["text"],
                    str(task["brief"]["aspect_ratio"]),
                    plan,
                )

            artifact, created = save_agent_artifact(
                self.db,
                task_id=task_id,
                artifact_type=artifact_type,
                schema_version=schema_version,
                content=content,
                input_digest=input_digest,
                source_agent=source_agent,
                source_model_label=source_model_label,
            )
            result = None
            next_action = "start_recommend_storyboard_for_tts"
            if artifact_type == "digital_storyboard":
                result = finalize_digital_storyboard(
                    self.db,
                    task_id=task_id,
                    project_root=self.project_root,
                    content=content,
                    input_binding={
                        "task_id": task_id,
                        "script_sha256": context["script"]["sha256"],
                    },
                    input_digest=input_digest,
                    source="agent_artifact",
                )
                next_action = "review_storyboard_in_cutroom"
            return {
                "artifact": artifact,
                "created": created,
                "finalization": result,
                "nextAction": next_action,
            }

        return self._guard(operation)

    def start_task_action(
        self,
        *,
        task_id: str,
        action: str,
        confirm_cost: bool = False,
        request_id: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        def operation():
            task = self._require_agent_task(task_id)
            if action not in ALLOWED_ACTIONS:
                raise ValueError(
                    "AGENT_ACTION_NOT_ALLOWED: Agent 动作不在允许列表中"
                )
            if action in PAID_ACTIONS and confirm_cost is not True:
                raise ValueError(
                    "PAID_CONFIRMATION_REQUIRED: 付费动作需要明确确认"
                )
            supplied = payload if isinstance(payload, dict) else {}
            job_payload = {"action": action, "task_id": task_id}
            provider_request_id = None
            cost_status = "unknown"
            if action == "sync_sources":
                impact_token = supplied.get("impact_token")
                if not isinstance(impact_token, str) or not impact_token:
                    raise ValueError(
                        "IMPACT_TOKEN_REQUIRED: 素材同步必须使用预览确认令牌"
                    )
                job_payload["impact_token"] = impact_token
            elif action == "recommend_storyboard":
                if task["task_type"] == "digital_human":
                    if not list_agent_artifacts(
                        self.db,
                        task_id,
                        "digital_storyboard",
                    ):
                        raise ValueError(
                            "AGENT_ARTIFACT_REQUIRED: 请先提交数字人规划产物"
                        )
                    return {
                        "status": "already_finalized",
                        "nextAction": "review_storyboard_in_cutroom",
                    }
                artifacts = [
                    item
                    for item in list_agent_artifacts(
                        self.db,
                        task_id,
                        "ad_storyboard",
                    )
                    if item["status"] == "accepted"
                ]
                if not artifacts:
                    raise ValueError(
                        "AGENT_ARTIFACT_REQUIRED: 请先提交广告规划产物"
                    )
                artifact = artifacts[-1]
                if artifact["input_digest"] != current_agent_input_digest(
                    self.db,
                    task_id,
                    self.project_root,
                ):
                    raise ValueError(
                        "AGENT_INPUT_STALE: 广告规划已不匹配当前输入"
                    )
                job_payload["artifact_id"] = artifact["artifact_id"]
                cost_status = "approved"
                stable_request = request_id or artifact["artifact_id"]
                provider_request_id = (
                    f"agent-storyboard:{task_id}:{stable_request}"
                )
            elif action == "build_publish_package":
                platform = supplied.get("platform")
                if not isinstance(platform, str) or not platform:
                    raise ValueError(
                        "PLATFORM_REQUIRED: 发布包必须指定平台"
                    )
                job_payload.update(
                    {
                        "platform": platform,
                        "title": supplied.get("title"),
                        "description": supplied.get("description"),
                        "tags": supplied.get("tags", []),
                    }
                )
            job_id = f"job-{uuid.uuid4().hex}"
            actual_id = enqueue_job(
                self.db,
                job_id=job_id,
                job_type=action,
                payload=job_payload,
                task_id=task_id,
                cost_status=cost_status,
                provider_request_id=provider_request_id,
            )
            return _safe_job(get_job(self.db, actual_id))

        return self._guard(operation)

    def cancel_job(self, job_id: str, reason: str = "Agent 请求取消") -> dict:
        def operation():
            job = get_job(self.db, job_id)
            if job is None:
                raise KeyError(job_id)
            self._require_agent_task(job["task_id"])
            request_cancel(self.db, job_id, reason=reason[:200])
            return _safe_job(get_job(self.db, job_id))

        return self._guard(operation)

    def resume_job(self, job_id: str) -> dict:
        def operation():
            job = get_job(self.db, job_id)
            if job is None:
                raise KeyError(job_id)
            self._require_agent_task(job["task_id"])
            resume_job(self.db, job_id)
            return _safe_job(get_job(self.db, job_id))

        return self._guard(operation)
