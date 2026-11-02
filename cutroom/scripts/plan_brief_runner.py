"""Credential-safe bridge from ATR's fixed environment to plan-brief.ts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--ts-node", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--catalog-db", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--capability", default="general_text")
    parser.add_argument("--packaging-capability")
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--brief-output", type=Path, required=True)
    parser.add_argument("--plan-output", type=Path, required=True)
    parser.add_argument("--speed", default="1.25")
    parser.add_argument("--profile", default="zhaoshang")
    return parser.parse_args(argv)


def merge_split_outputs(
    storyboard_brief,
    storyboard_plan,
    packaging_brief,
    packaging_plan,
):
    if not all(
        isinstance(value, dict)
        for value in (
            storyboard_brief,
            storyboard_plan,
            packaging_brief,
            packaging_plan,
        )
    ):
        raise ValueError("PLAN_BRIEF_OUTPUT_INVALID: 模型规划输出必须是对象")
    merged_brief = dict(packaging_brief)
    merged_brief["broll"] = list(storyboard_brief.get("broll") or [])
    merged_plan = dict(packaging_plan)
    return merged_brief, merged_plan


def _command(args, root: Path, brief_output: Path, plan_output: Path) -> list[str]:
    return [
        str(args.node.resolve()),
        str(args.ts_node.resolve()),
        str((root / "scripts" / "plan-brief.ts").resolve()),
        "--script", str(args.script.resolve()),
        "--brief-output", str(brief_output.resolve()),
        "--plan-output", str(plan_output.resolve()),
        "--speed", args.speed,
        "--profile", args.profile,
    ]


def _run(command: list[str], root: Path, environment: dict[str, str]) -> int:
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            env=environment,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    except (OSError, ValueError) as error:
        print(f"PLAN_BRIEF_START_FAILED: {type(error).__name__}", file=sys.stderr)
        return 2
    return int(completed.returncode)


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("PLAN_BRIEF_OUTPUT_INVALID: 模型规划输出无法读取") from error
    if not isinstance(value, dict):
        raise ValueError("PLAN_BRIEF_OUTPUT_INVALID: 模型规划输出必须是对象")
    return value


def _write_object(path: Path, value: dict) -> None:
    part = path.with_name(path.name + ".merge.part")
    part.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    part.replace(path)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = args.project_root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from media_catalog.credentials import legacy_environment, read_secret
    from media_catalog.database import CatalogDatabase
    from media_catalog.model_runtime import (
        TaskModelConfigurationError,
        task_model_environment,
    )

    environment = legacy_environment(root)
    packaging_environment = None
    task_mode = args.catalog_db is not None or args.task_id is not None
    if task_mode:
        if args.catalog_db is None or not args.task_id:
            print("MODEL_BINDING_ARGUMENTS_INVALID: 必须同时提供数据库和任务编号", file=sys.stderr)
            return 2
        try:
            with CatalogDatabase.open(args.catalog_db) as db:
                environment, _snapshot = task_model_environment(
                    db,
                    args.task_id,
                    args.capability,
                    base_environment=environment,
                )
                if args.packaging_capability:
                    packaging_environment, _packaging_snapshot = (
                        task_model_environment(
                            db,
                            args.task_id,
                            args.packaging_capability,
                            base_environment=legacy_environment(root),
                        )
                    )
        except (OSError, ValueError, TaskModelConfigurationError) as error:
            print(str(error), file=sys.stderr)
            return 2
    else:
        secret = read_secret("general_text", env=environment)
        if not secret or not args.model:
            print("GENERAL_TEXT_NOT_CONFIGURED: 请先在设置中配置文案与分镜模型", file=sys.stderr)
            return 2
        environment["ARK_MODEL"] = args.model
        environment["ARK_API_KEY"] = secret
    if packaging_environment is None:
        return _run(
            _command(args, root, args.brief_output, args.plan_output),
            root,
            environment,
        )

    storyboard_brief = args.brief_output.with_name(
        "." + args.brief_output.name + ".storyboard-stage"
    )
    storyboard_plan = args.plan_output.with_name(
        "." + args.plan_output.name + ".storyboard-stage"
    )
    packaging_brief = args.brief_output.with_name(
        "." + args.brief_output.name + ".packaging-stage"
    )
    packaging_plan = args.plan_output.with_name(
        "." + args.plan_output.name + ".packaging-stage"
    )
    stage_paths = (
        storyboard_brief,
        storyboard_plan,
        packaging_brief,
        packaging_plan,
    )
    for path in stage_paths:
        path.unlink(missing_ok=True)
    try:
        code = _run(
            _command(args, root, storyboard_brief, storyboard_plan),
            root,
            environment,
        )
        if code:
            return code
        code = _run(
            _command(args, root, packaging_brief, packaging_plan),
            root,
            packaging_environment,
        )
        if code:
            return code
        merged_brief, merged_plan = merge_split_outputs(
            _read_object(storyboard_brief),
            _read_object(storyboard_plan),
            _read_object(packaging_brief),
            _read_object(packaging_plan),
        )
        _write_object(args.brief_output, merged_brief)
        _write_object(args.plan_output, merged_plan)
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        for path in stage_paths:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
