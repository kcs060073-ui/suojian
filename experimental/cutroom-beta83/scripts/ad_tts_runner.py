"""Run only task-scoped ad TTS without loading any text-model capability."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
import subprocess
import sys


_TEXT_ENVIRONMENT_KEYS = (
    "ARK_API_KEY",
    "ARK_MODEL",
    "ARK_BASE_URL",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GENERAL_TEXT_API_KEY",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--ts-node", type=Path, required=True)
    parser.add_argument("--catalog-db", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--plan-input", type=Path, required=True)
    parser.add_argument("--timing-output", type=Path, required=True)
    parser.add_argument("--voice-output", type=Path, required=True)
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--speed", default="1.0")
    parser.add_argument("--execution-output", type=Path)
    parser.add_argument("--compiled-input-fingerprint")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = args.project_root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from media_catalog.credentials import legacy_environment
    from media_catalog.database import CatalogDatabase
    from media_catalog.model_runtime import (
        TaskModelConfigurationError,
        task_model_environment,
    )

    try:
        with CatalogDatabase.open(args.catalog_db) as db:
            environment, _tts = task_model_environment(
                db,
                args.task_id,
                "tts",
                base_environment=legacy_environment(root),
            )
    except (OSError, ValueError, TaskModelConfigurationError) as error:
        print(str(error), file=sys.stderr)
        return 2

    if args.execution_output is not None:
        digest = _tts.get("configDigest")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            print("TTS_FROZEN_DIGEST_REQUIRED", file=sys.stderr)
            return 2
        environment["CUTROOM_TTS_PROFILE_DIGEST"] = digest
        if args.compiled_input_fingerprint is not None:
            if re.fullmatch(r"[0-9a-f]{64}", args.compiled_input_fingerprint) is None:
                print("TTS_COMPILED_DIGEST_INVALID", file=sys.stderr)
                return 2
            environment["CUTROOM_TTS_CONFIG_FINGERPRINT"] = args.compiled_input_fingerprint
        else:
            environment.pop("CUTROOM_TTS_CONFIG_FINGERPRINT", None)
    elif args.compiled_input_fingerprint is not None:
        print("TTS_EXECUTION_OUTPUT_REQUIRED", file=sys.stderr)
        return 2

    for key in _TEXT_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment.pop(key, None)

    command = [
        str(args.node.resolve()),
        str(args.ts_node.resolve()),
        str((root / "scripts" / "tts.ts").resolve()),
        "--plan",
        str(args.plan_input.resolve()),
        "--speed",
        args.speed,
        "--timing-output",
        str(args.timing_output.resolve()),
        "--voice-output",
        str(args.voice_output.resolve()),
        "--segments-dir",
        str(args.segments_dir.resolve()),
    ]
    if args.execution_output is not None:
        command.extend(["--execution-output", str(args.execution_output.resolve())])
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
        print(
            f"AD_TTS_START_FAILED: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
