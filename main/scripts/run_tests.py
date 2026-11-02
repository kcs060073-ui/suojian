"""溯剪主应用 —— 一键回归测试入口。

背景：
    tests/ 目录没有 __init__.py，直接 `python -m unittest discover -s tests`
    会 ImportError；本项目历史上一直靠逐文件加载才跑得动。
    本脚本把这条已验证可用的加载方式固化下来。

另外一个刻意设计：TextTestRunner 的 stream 显式指向 stdout。
unittest 默认把进度点写 stderr，在 PowerShell 的
$ErrorActionPreference = "Stop" 下会被当成 NativeCommandError 中断脚本。

用法：
    .venv\\Scripts\\python.exe scripts\\run_tests.py
    或 run-tests.ps1 / run-tests.bat
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest


def build_suite(root: pathlib.Path) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for path in sorted((root / "tests").glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            print(f"LOAD_FAIL {path.name} (无法构建模块 spec)")
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # 加载期异常要显式暴露，不能静默跳过
            print(f"LOAD_FAIL {path.name} {type(exc).__name__}: {str(exc)[:200]}")
            continue
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    return suite


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    os.chdir(root)
    sys.path.insert(0, str(root))

    print(f"[INFO] repo root: {root}")
    print(f"[INFO] python   : {sys.executable}")

    suite = build_suite(root)
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    print(
        "SUMMARY run={} fail={} err={} skip={}".format(
            result.testsRun, len(result.failures), len(result.errors), len(result.skipped)
        )
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
