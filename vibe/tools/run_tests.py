"""溯剪终端（园区 Vibe 工作台）—— 一键回归测试入口。

本目录没有独立 venv，默认借用主应用 suojian 的虚拟环境：
    run-tests.ps1 / run-tests.bat 会自动定位解释器。

两个刻意的细节：
1) runner 的 stream 显式指向 stdout。unittest 默认把进度写 stderr，
   在 PowerShell 的 $ErrorActionPreference = "Stop" 下会被当成
   NativeCommandError 中断脚本。
2) discover 不传 top_level_dir。tests/ 没有 __init__.py，一旦传了
   top_level_dir 就会按包导入，直接 ImportError: not importable。
   同时避免打印含中文的绝对路径，防止控制台码页不一致产生乱码。

用法：
    .venv\\Scripts\\python.exe tools\\run_tests.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    os.chdir(root)
    sys.path.insert(0, str(root))

    print(f"[INFO] python: {sys.executable}")

    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    print(
        "SUMMARY run={} fail={} err={} skip={}".format(
            result.testsRun, len(result.failures), len(result.errors), len(result.skipped)
        )
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
