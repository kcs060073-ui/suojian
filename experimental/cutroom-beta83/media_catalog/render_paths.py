"""Reserve new physical candidate directories without adopting historical files."""
from __future__ import annotations
import os
from pathlib import Path


def reserve_candidate_paths(project_root, task_root, task_id: str, start_version: int):
    """Physical directory numbers are separate from DB candidate version numbers."""
    root = Path(project_root).resolve()
    if not isinstance(task_id, str) or not task_id or task_id in {".", ".."} or any(
            char in task_id for char in '/\\:*?<>|'):
        raise RuntimeError("RENDER_OUTPUT_INVALID: 候选成片目录越界")
    output_root = (root / "out" / task_id).resolve()
    work_root = (Path(task_root).resolve() / "render").resolve()
    if not output_root.is_relative_to(root) or not work_root.is_relative_to(root):
        raise RuntimeError("RENDER_OUTPUT_INVALID: 候选成片目录越界")
    output_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    for version in range(max(1, int(start_version)), max(1, int(start_version)) + 10000):
        output_dir = output_root / f"candidate-v{version}"
        work_dir = work_root / output_dir.name
        if os.path.lexists(output_dir) or os.path.lexists(work_dir):
            continue
        try:
            output_dir.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        try:
            work_dir.mkdir(exist_ok=False)
        except FileExistsError:
            # Keep our empty reservation; a competing process owns the work dir.
            continue
        return output_dir / "video-final.mp4", work_dir
    raise RuntimeError("RENDER_OUTPUT_RESERVATION_FAILED: 无法预留新的成片目录")
