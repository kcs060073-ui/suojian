"""workbench 外部依赖路径的单一来源。

为什么要有这个文件：
    2026-10-23 评估发现，主应用源码原本住在 Codex 的日期戳临时目录里，
    而 workbench 有 7 处硬编码绝对路径指回去（server.py / library.py /
    production.py，另有 4 处散落在 tests 里）。一旦目录被清理或换机，
    渲染链路会同时断掉，报错点却分散在多个模块里，极难排查。

解析顺序（每个路径都一样）：
    环境变量 > PATH 探测 > 默认位置 > 历史遗留位置

    默认位置不存在时自动回退到历史遗留位置，保证「没做迁移的机器也能跑」。

支持的环境变量：
    SUOJIAN_ROOT            主应用根目录
    SUOJIAN_PYTHON          主应用 venv 的 python
    SUOJIAN_IMPORT_DIR      park-vibe-skill-fusion 上游导入目录
    SUOJIAN_LEGACY_MODULES  Remotion 的 node_modules（历史遗留）
    SUOJIAN_RUNTIME_PYTHON  codex 运行时 python（备用）
    SUOJIAN_NODE            node 可执行文件
    SUOJIAN_FFPROBE         ffprobe 可执行文件
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
HOME = Path.home()

# 迁移目标位置（稳定、非日期戳目录）
DEFAULT_SUOJIAN_ROOT = Path("C:/Users/Tanikawa/suojian")
# 历史遗留位置（2026-10-23 之前的真实源码目录）
LEGACY_SUOJIAN_ROOT = Path(
    "C:/Users/Tanikawa/Documents/Codex/2026-09-20/"
    "continuing-from-chatgpt-conversation-6aaf4eec-667c/suojian"
)
LEGACY_IMPORT_DIR = Path(
    "C:/Users/Tanikawa/Documents/Codex/2026-10-20/"
    "suojian-vibe/work/imports/park-vibe-skill-fusion-2026-10-20"
)
LEGACY_REMOTION_MODULES = Path("C:/Users/Tanikawa/Documents/Codex/2026-10-08/ban/motion-park/node_modules")
LEGACY_RUNTIME_PYTHON = HOME / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
FALLBACK_NODE = Path("C:/Users/Tanikawa/tools/node/node.exe")
FALLBACK_FFPROBE = Path("C:/Users/Tanikawa/tools/ffmpeg/bin/ffprobe.exe")


def _first_existing(*candidates):
    """返回第一个真实存在的候选路径，都不存在则返回 None。"""
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    return None


def _resolve_path(env_name: str, *candidates) -> Path:
    raw = os.environ.get(env_name)
    if raw:
        return Path(raw).expanduser()
    found = _first_existing(*candidates)
    if found is not None:
        return found
    # 一个都不存在时返回第一个候选：让调用方看到「期望路径」而不是静默 None
    return Path(candidates[0]).expanduser()


def _resolve_exe(env_name: str, which_name: str, *candidates) -> str:
    raw = os.environ.get(env_name)
    if raw:
        return str(Path(raw).expanduser())
    hit = shutil.which(which_name)
    if hit:
        return hit
    found = _first_existing(*candidates)
    if found is not None:
        return str(found)
    return str(Path(candidates[0]).expanduser())


# —— 主应用（三入口 Flask 工作台）——
SUOJIAN_ROOT: Path = _resolve_path("SUOJIAN_ROOT", DEFAULT_SUOJIAN_ROOT, LEGACY_SUOJIAN_ROOT)
PYTHON: Path = _resolve_path(
    "SUOJIAN_PYTHON",
    SUOJIAN_ROOT / ".venv/Scripts/python.exe",
    LEGACY_SUOJIAN_ROOT / ".venv/Scripts/python.exe",
)

# —— 上游导入目录与素材库 ——
IMPORT_DIR: Path = _resolve_path("SUOJIAN_IMPORT_DIR", LEGACY_IMPORT_DIR)
# 与 server.py 原逻辑一致：仓库自带 vendor 优先，否则用上游快照
VENDOR: Path = (
    REPO_ROOT / "work/vendor"
    if (REPO_ROOT / "work/vendor").exists()
    else IMPORT_DIR / "experiments/vendor"
)

# —— 原生渲染依赖 ——
LEGACY_MODULES: Path = _resolve_path("SUOJIAN_LEGACY_MODULES", LEGACY_REMOTION_MODULES)
RUNTIME_PYTHON: Path = _resolve_path("SUOJIAN_RUNTIME_PYTHON", LEGACY_RUNTIME_PYTHON)
NODE: str = _resolve_exe("SUOJIAN_NODE", "node", FALLBACK_NODE)
FFPROBE: str = _resolve_exe("SUOJIAN_FFPROBE", "ffprobe", FALLBACK_FFPROBE)


def describe() -> dict:
    """返回当前解析结果，便于 /api/health 或排障脚本打印。"""
    return {
        "SUOJIAN_ROOT": str(SUOJIAN_ROOT),
        "PYTHON": str(PYTHON),
        "IMPORT_DIR": str(IMPORT_DIR),
        "VENDOR": str(VENDOR),
        "LEGACY_MODULES": str(LEGACY_MODULES),
        "RUNTIME_PYTHON": str(RUNTIME_PYTHON),
        "NODE": NODE,
        "FFPROBE": FFPROBE,
    }


if __name__ == "__main__":
    for key, value in describe().items():
        print(f"{key:16} {value}")
