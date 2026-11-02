"""Credential-safe bridge for task-scoped ad planning and TTS."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--ts-node", type=Path, required=True)
    parser.add_argument("--general-model")
    parser.add_argument("--voice")
    parser.add_argument("--catalog-db", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--text-capability", default="general_text")
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--ratio", choices=("9:16", "16:9"), required=True)
    parser.add_argument("--plan-output", type=Path, required=True)
    parser.add_argument("--timing-output", type=Path, required=True)
    parser.add_argument("--voice-output", type=Path, required=True)
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--speed", default="1.0")
    return parser.parse_args(argv)


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
        print(f"AD_PIPELINE_START_FAILED: {type(error).__name__}", file=sys.stderr)
        return 2
    return int(completed.returncode)


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
    task_mode = args.catalog_db is not None or args.task_id is not None
    if task_mode:
        if args.catalog_db is None or not args.task_id:
            print("MODEL_BINDING_ARGUMENTS_INVALID: 必须同时提供数据库和任务编号", file=sys.stderr)
            return 2
        try:
            with CatalogDatabase.open(args.catalog_db) as db:
                environment, _general = task_model_environment(
                    db,
                    args.task_id,
                    args.text_capability,
                    base_environment=environment,
                )
                environment, _tts = task_model_environment(
                    db,
                    args.task_id,
                    "tts",
                    base_environment=environment,
                )
        except (OSError, ValueError, TaskModelConfigurationError) as error:
            print(str(error), file=sys.stderr)
            return 2
    else:
        general_secret = read_secret("general_text", env=environment)
        tts_secret = read_secret("tts", env=environment)
        if not general_secret or not args.general_model:
            print("GENERAL_TEXT_NOT_CONFIGURED: 请先配置文案与分镜模型", file=sys.stderr)
            return 2
        if not tts_secret or not args.voice:
            print("TTS_NOT_CONFIGURED: 请先配置 TTS 访问凭据", file=sys.stderr)
            return 2
        if not environment.get("TTS_APPID"):
            print("TTS_APPID_NOT_CONFIGURED: 请在本机环境配置 TTS_APPID", file=sys.stderr)
            return 2
        environment["ARK_MODEL"] = args.general_model
        environment["ARK_API_KEY"] = general_secret
        environment["TTS_ACCESS_TOKEN"] = tts_secret
        environment["TTS_VOICE_TYPE"] = args.voice

    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment.pop(key, None)

    node = str(args.node.resolve())
    ts_node = str(args.ts_node.resolve())
    plan_command = [
        node, ts_node, str((root / "scripts" / "plan-ad.ts").resolve()),
        "--script", str(args.script.resolve()),
        "--ratio", args.ratio,
        "--output", str(args.plan_output.resolve()),
    ]
    code = _run(plan_command, root, environment)
    if code:
        return code

    tts_command = [
        node, ts_node, str((root / "scripts" / "tts.ts").resolve()),
        "--plan", str(args.plan_output.resolve()),
        "--speed", args.speed,
        "--timing-output", str(args.timing_output.resolve()),
        "--voice-output", str(args.voice_output.resolve()),
        "--segments-dir", str(args.segments_dir.resolve()),
    ]
    return _run(tts_command, root, environment)


if __name__ == "__main__":
    raise SystemExit(main())

