"""Black-box smoke checks for the local CUTROOM workbench."""

from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener


EXPECTED_STAGES = (
    "create_preflight",
    "brief_lock",
    "material_sync",
    "ai_storyboard",
    "rough_cut",
    "picture_order",
    "picture_lock",
    "packaging_confirm",
    "fine_render",
    "auto_qa",
    "full_review",
    "publish_package",
)
EXPECTED_PACKAGE_FILES = (
    "video-final.mp4",
    "cover-01.jpg",
    "cover-02.jpg",
    "cover-03.jpg",
    "publish-copy.md",
    "publish-check.json",
    "qa-report.md",
)
REQUIRED_GATES = (
    "brief_locked",
    "picture_lock",
    "packaging_confirmed",
    "qa_passed",
    "human_reviewed",
)
FORBIDDEN_CREDENTIAL_KEYS = {
    "apikey",
    "credential",
    "credentialref",
    "password",
    "secret",
    "token",
}


class SmokeFailure(RuntimeError):
    pass


def _credential_paths(value: Any, path: str = "$") -> list[str]:
    matches = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            if any(forbidden in normalized for forbidden in FORBIDDEN_CREDENTIAL_KEYS):
                matches.append(f"{path}.{key}")
            matches.extend(_credential_paths(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(_credential_paths(item, f"{path}[{index}]"))
    return matches


def validate_workbench_snapshot(
    bootstrap: dict,
    tasks_payload: dict,
    detail: dict,
    *,
    task_id: str,
) -> dict[str, bool]:
    errors: list[str] = []
    checks: dict[str, bool] = {}

    task_ids = [item.get("taskId") for item in bootstrap.get("tasks", [])]
    checks["task_present"] = task_id in task_ids and detail.get("task", {}).get(
        "task_id"
    ) == task_id
    if not checks["task_present"]:
        errors.append("真实目标任务未同时出现在 bootstrap 和详情中")

    checks["board_list_same_source"] = (
        tasks_payload.get("tasks") == bootstrap.get("tasks")
    )
    if not checks["board_list_same_source"]:
        errors.append("任务看板/列表没有共享同一批 SQLite 任务事实")

    bootstrap_stages = [
        item.get("key") for item in bootstrap.get("workflowStages", [])
    ]
    detail_stages = [
        item.get("stage_key")
        for item in detail.get("workflow", {}).get("stages", [])
    ]
    checks["workflow_twelve_steps"] = (
        bootstrap_stages == list(EXPECTED_STAGES)
        and detail_stages == list(EXPECTED_STAGES)
    )
    if not checks["workflow_twelve_steps"]:
        errors.append("十二步工作流顺序或数量不准确")

    gates = detail.get("workflow", {}).get("gates", {})
    checks["four_gates_and_qa"] = all(gates.get(key) is True for key in REQUIRED_GATES)
    if not checks["four_gates_and_qa"]:
        errors.append("四道人工门禁或自动 QA 尚未全部通过，尤其需完成当前候选的完整审片")

    candidate = detail.get("reviewCandidate") or {}
    checks["trusted_review_candidate"] = (
        candidate.get("producer") == "assemble-v1"
        and all(
            candidate.get(key)
            for key in ("mix_snapshot_id", "qa_snapshot_id", "qa_report_id")
        )
    )
    if not checks["trusted_review_candidate"]:
        errors.append("缺少 assemble-v1 生成并通过 QA 的可信候选成片")

    watch = detail.get("watchSession") or {}
    checks["full_watch"] = (
        watch.get("status") == "completed" and watch.get("can_approve") is True
    )
    if not checks["full_watch"]:
        errors.append("当前候选没有可批准的完整观看记录")

    packages = detail.get("publishPackages") or []
    package_ok = False
    for package in packages:
        names = [Path(item).name for item in package.get("files", [])]
        if (
            package.get("package_complete") is True
            and names == list(EXPECTED_PACKAGE_FILES)
        ):
            package_ok = True
            break
    checks["seven_file_publish_package"] = package_ok
    if not package_ok:
        errors.append("没有找到完整且顺序固定的七文件发布包")

    routes = {item.get("key"): item for item in bootstrap.get("routes", [])}
    checks["paid_video_generation_disabled"] = (
        routes.get("digital_human", {}).get("availability") == "available"
        and all(
            routes.get(key, {}).get("availability") != "available"
            for key in ("park_video", "ad_mix")
        )
    )
    if not checks["paid_video_generation_disabled"]:
        errors.append("Phase 1 路线开放边界不符合约定")

    credential_paths = _credential_paths([bootstrap, tasks_payload, detail])
    checks["responses_contain_no_credentials"] = not credential_paths
    if credential_paths:
        errors.append("API 响应包含凭据字段：" + ", ".join(credential_paths))

    if errors:
        raise SmokeFailure("\n".join(f"- {item}" for item in errors))
    return checks


class SmokeClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/") + "/"
        self.origin = self.base_url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
            request_headers["Origin"] = self.origin
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=20)
        except HTTPError as error:
            return error.code, dict(error.headers.items()), error.read()
        except URLError as error:
            raise SmokeFailure(f"无法连接 CUTROOM：{error.reason}") from error
        with response:
            return response.status, dict(response.headers.items()), response.read()

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
    ) -> tuple[int, dict]:
        status, _headers, body = self.request(method, path, payload=payload)
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError as error:
            raise SmokeFailure(f"{path} 没有返回 JSON") from error
        return status, parsed


def _expect_error(
    client: SmokeClient,
    path: str,
    payload: dict,
    *,
    status: int,
    code: str,
) -> dict:
    actual_status, body = client.json("POST", path, payload=payload)
    actual_code = (body.get("error") or {}).get("code")
    if actual_status != status or actual_code != code:
        raise SmokeFailure(
            f"{path} 预期 {status}/{code}，实际 {actual_status}/{actual_code}"
        )
    return body


def run_smoke(base_url: str, task_id: str, startup_key: str | None = None) -> dict:
    client = SmokeClient(base_url)
    observed_payloads: list[dict] = []

    status, _headers, page = client.request("GET", "/")
    if status != 200 or b"<title>CUTROOM" not in page:
        raise SmokeFailure("固定入口没有返回 CUTROOM 正式页面")

    if startup_key:
        login_path = "/auth/bootstrap?" + urlencode({"key": startup_key})
        login_status, _login_headers, _login_body = client.request("GET", login_path)
        if login_status != 200:
            raise SmokeFailure(f"本机启动凭据交换失败：HTTP {login_status}")

    status, bootstrap = client.json("GET", "/api/bootstrap")
    if status == 403 and not startup_key:
        raise SmokeFailure("API 尚未授权；请传入本次服务输出的 --startup-key")
    if status != 200:
        raise SmokeFailure(f"bootstrap 失败：HTTP {status}")
    observed_payloads.append(bootstrap)

    status, tasks = client.json("GET", "/api/tasks")
    if status != 200:
        raise SmokeFailure(f"任务列表失败：HTTP {status}")
    observed_payloads.append(tasks)

    encoded_task = quote(task_id, safe="")
    status, detail = client.json("GET", f"/api/tasks/{encoded_task}")
    if status != 200:
        raise SmokeFailure(f"目标任务详情失败：HTTP {status}")
    observed_payloads.append(detail)
    snapshot_checks = validate_workbench_snapshot(
        bootstrap,
        tasks,
        detail,
        task_id=task_id,
    )

    preview_status, preview_headers, preview_body = client.request(
        "GET",
        f"/api/tasks/{encoded_task}/review-media",
        headers={"Range": "bytes=0-1"},
    )
    if (
        preview_status != 206
        or preview_headers.get("Accept-Ranges") != "bytes"
        or len(preview_body) != 2
    ):
        raise SmokeFailure("当前可信候选不能通过受控 Range 接口预览")

    traversal_status, _headers, traversal_body = client.request(
        "GET", "/..%2FREADME.md"
    )
    traversal_payload = json.loads(traversal_body)
    observed_payloads.append(traversal_payload)
    if (
        traversal_status != 403
        or traversal_payload.get("error", {}).get("code") != "PATH_REJECTED"
    ):
        raise SmokeFailure("静态路径逃逸没有被拒绝")

    incomplete = next(
        (
            item
            for item in bootstrap.get("tasks", [])
            if not any((item.get("gates") or {}).values())
        ),
        None,
    )
    if incomplete is None:
        raise SmokeFailure("验收库缺少用于验证门禁不可绕过的未开始任务")
    blocked_task = quote(incomplete["taskId"], safe="")
    gate_cases = (
        (f"/api/tasks/{blocked_task}/sources/sync", {"impactToken": "smoke"}, "BRIEF_GATE_MISSING"),
        (f"/api/tasks/{blocked_task}/packaging/compose", {}, "PICTURE_LOCK_MISSING"),
        (f"/api/tasks/{blocked_task}/render", {}, "PACKAGING_PLAN_MISSING"),
        (f"/api/tasks/{blocked_task}/publish", {"platform": "smoke"}, "REVIEW_NOT_APPROVED"),
    )
    for path, payload, code in gate_cases:
        observed_payloads.append(
            _expect_error(client, path, payload, status=409, code=code)
        )

    observed_payloads.append(
        _expect_error(
            client,
            f"/api/tasks/{encoded_task}/video/generate",
            {"prompt": "must-not-run"},
            status=404,
            code="NOT_FOUND",
        )
    )

    leaks = _credential_paths(observed_payloads)
    serialized = json.dumps(observed_payloads, ensure_ascii=False)
    if leaks or (startup_key and startup_key in serialized):
        raise SmokeFailure("smoke 观察到 API 响应泄露凭据")

    return {
        "baseUrl": client.origin,
        "taskId": task_id,
        "checks": {
            "formal_page": True,
            **snapshot_checks,
            "controlled_preview": True,
            "four_gate_bypass_rejected": True,
            "path_traversal_rejected": True,
            "paid_generation_route_absent": True,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 CUTROOM 真实纵切")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument(
        "--startup-key",
        help="仅用于本次本机服务的首次会话交换；不会写入报告或输出",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_smoke(args.base_url, args.task, args.startup_key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as error:
        print(f"SMOKE FAILED\n{error}")
        raise SystemExit(1) from error
