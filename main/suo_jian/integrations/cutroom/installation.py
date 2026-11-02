"""Read-only installation checks. Never initialize or migrate a task database."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from contextlib import closing


def installation_info(root: Path) -> dict:
    root = Path(root).resolve()
    manifest = root / "release-manifest.json"
    package = root / "package.json"
    data = json.loads((manifest if manifest.is_file() else package).read_text(encoding="utf-8-sig")) if (manifest.is_file() or package.is_file()) else {}
    database = root / "runtime" / "workbench" / "catalog.sqlite"
    migrations = [int(p.name.split("_", 1)[0]) for p in (root / "media_catalog" / "migrations").glob("[0-9][0-9][0-9]_*.sql")]
    value = {"version": data.get("version"), "distribution": data.get("distribution", "source"),
             "database_path": str(database), "database_exists": database.is_file(),
             "schema_version": None, "expected_schema_version": max(migrations, default=None),
             "reference_extension_available": (root / "media_catalog" / "reference_editing.py").is_file(),
             "mcp_verified": False}
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                value["schema_version"] = db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        except sqlite3.Error:
            value["database_error"] = "无法读取 Cutroom 数据库版本；未初始化或修改任务库。"
    value["database_ready"] = bool(value["schema_version"] is not None and value["expected_schema_version"] is not None
                                   and value["schema_version"] == value["expected_schema_version"])
    return value


def require_existing_database(root: Path) -> dict:
    info = installation_info(root)
    if not info["database_exists"]:
        raise ValueError("Cutroom 尚未绑定已有任务库；请完成安装与数据恢复，不能用空库代替旧任务。")
    if not info["database_ready"]:
        raise ValueError(f"Cutroom 数据库版本 {info['schema_version']} 与程序版本 {info['expected_schema_version']} 不匹配；请先备份并完成官方迁移。状态查询不会自动迁移数据库。")
    return info


def mcp_arguments(root: Path, origin: str | None = None) -> list[str]:
    """Compiled releases support explicit binding; beta.8 binds to its script root."""
    from urllib.parse import urlsplit
    root = Path(root).resolve()
    origin = origin or "http://127.0.0.1:8765"
    parsed = urlsplit(origin)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Cutroom 工作台地址必须是本机 HTTP 来源。")
    arguments = [str(root / "scripts" / "cutroom_mcp.py"), "--client", "codex"]
    if not (root / "release-manifest.json").is_file():
        if origin.rstrip("/") != "http://127.0.0.1:8765":
            raise ValueError("旧版 Cutroom 不支持自定义工作台地址绑定，请使用对应版本的默认地址。")
        return arguments
    return arguments + [
            "--db", str(root / "runtime" / "workbench" / "catalog.sqlite"),
            "--workbench-origin", origin.rstrip("/")]


def execution_root(default: Path, result_dir: Path, state: dict) -> Path:
    """Keep existing paid tasks on their original backend after an upgrade."""
    if not state.get("task_id"):
        return Path(default).resolve()
    if state.get("cutroom_root"):
        return Path(state["cutroom_root"]).resolve()
    binding_path = Path(result_dir) / "cutroom_binding.json"
    if binding_path.is_file():
        binding = json.loads(binding_path.read_text(encoding="utf-8-sig"))
        if binding.get("task_id") == state["task_id"] and binding.get("cutroom_root"):
            return Path(binding["cutroom_root"]).resolve()
    if state.get("preview_path"):
        preview = Path(state["preview_path"]).resolve()
        for parent in preview.parents:
            if parent.name == state["task_id"] and parent.parent.name == "out":
                return parent.parent.parent
    return Path(default).resolve()
