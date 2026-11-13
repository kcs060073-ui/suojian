# P0 素材数据底座实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在普通办公电脑上实现可扫描真实 10—50GB 本地/NAS素材目录、可搜索、可断点恢复、可迁移挂载位置且不误合并素材的 SQLite 数据底座。

**Architecture:** 新建独立 Python `media_catalog` 包，使用标准库 `sqlite3`、`hashlib`、`subprocess` 和 `unittest`，避免为 P0 增加原生 Node SQLite 依赖。CLI 负责库注册、扫描、状态、搜索、备份和恢复；现有 TypeScript 渲染线暂不直接读数据库，P2 再通过 JSON 适配器接入。

**Tech Stack:** Python 3.11、SQLite、`unittest`、FFmpeg/ffprobe、现有 Windows/PowerShell 环境。

## Global Constraints

- 原片只读，不复制、不改名、不转码、不删除。
- 同一时间只允许一个扫描或技术探测任务写入目录数据库。
- 快速指纹只标记疑似重复，不自动合并；确认同一素材前必须计算完整 SHA-256。
- 素材身份、位置、分析状态和人工决定以 `runtime/media-catalog/catalog.sqlite` 为主账本。
- 所有长耗时步骤记录 `pending / running / completed / failed / interrupted / stale` 和可恢复游标。
- ffprobe 查找顺序：`FFPROBE_PATH` 显式配置 → 当前 PATH → 已知安装根目录探测 → 返回可读诊断；不得写死本机 WinGet 版本路径。
- P0 成功标准是扫描、建目录、搜索和恢复，不包含自动分镜、视频渲染或视频脚本写作 Skill。

---

### Task 1: 建立 Python 包、测试入口和核心类型

**Files:**
- Create: `media_catalog/__init__.py`
- Create: `media_catalog/models.py`
- Create: `tests/__init__.py`
- Create: `tests/test_models.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `JobStatus`, `MediaKind`, `ScanSummary`，供后续数据库、扫描器和 CLI 共用。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_models.py
import unittest
from media_catalog.models import JobStatus, MediaKind, ScanSummary

class ModelTests(unittest.TestCase):
    def test_enums_and_summary_are_stable(self):
        self.assertEqual(JobStatus.INTERRUPTED.value, "interrupted")
        self.assertEqual(MediaKind.VIDEO.value, "video")
        self.assertEqual(ScanSummary(discovered=3, completed=2, failed=1).remaining, 0)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_models -v`

Expected: FAIL，提示 `No module named 'media_catalog'`。

- [ ] **Step 3: 实现最小类型**

```python
# media_catalog/models.py
from dataclasses import dataclass
from enum import Enum

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    STALE = "stale"

class MediaKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    OTHER = "other"

@dataclass(frozen=True)
class ScanSummary:
    discovered: int
    completed: int
    failed: int

    @property
    def remaining(self) -> int:
        return max(0, self.discovered - self.completed - self.failed)
```

在 `.gitignore` 追加：

```gitignore
runtime/media-catalog/
config/media-libraries.json
__pycache__/
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m unittest tests.test_models -v`

Expected: 1 test，`OK`。

- [ ] **Step 5: 提交**

```powershell
git add media_catalog tests/test_models.py tests/__init__.py .gitignore
git commit -m "feat: add media catalog core types"
```

### Task 2: 实现版本化 SQLite Schema 和迁移

**Files:**
- Create: `media_catalog/migrations/001_initial.sql`
- Create: `media_catalog/database.py`
- Create: `tests/test_database.py`

**Interfaces:**
- Produces: `CatalogDatabase.open(path)`, `migrate()`, `transaction()`。
- Schema produces: `libraries`, `assets`, `asset_locations`, `duplicate_candidates`, `analysis_runs`, `jobs`, `schema_migrations`。

- [ ] **Step 1: 写迁移幂等性失败测试**

```python
# tests/test_database.py
import tempfile, unittest
from pathlib import Path
from media_catalog.database import CatalogDatabase

class DatabaseTests(unittest.TestCase):
    def test_migration_is_idempotent_and_enables_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp) / "catalog.sqlite")
            db.migrate(); db.migrate()
            tables = {r[0] for r in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertTrue({"libraries", "assets", "asset_locations", "analysis_runs", "jobs"} <= tables)
            self.assertEqual(db.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            db.close()
```

- [ ] **Step 2: 运行并确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_database -v`

Expected: FAIL，提示缺少 `media_catalog.database`。

- [ ] **Step 3: 创建首版 Schema**

```sql
-- media_catalog/migrations/001_initial.sql
CREATE TABLE IF NOT EXISTS libraries (
  library_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('local','nas')),
  mount_path TEXT NOT NULL,
  online INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  media_kind TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  quick_fingerprint TEXT NOT NULL,
  full_sha256 TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_quick_fingerprint
  ON assets(size_bytes, quick_fingerprint);
CREATE TABLE IF NOT EXISTS asset_locations (
  location_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  library_id TEXT NOT NULL REFERENCES libraries(library_id),
  relative_path TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL,
  file_identity TEXT,
  state TEXT NOT NULL DEFAULT 'available',
  UNIQUE(library_id, relative_path)
);
CREATE TABLE IF NOT EXISTS analysis_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  tool_version TEXT,
  result_json TEXT,
  error_summary TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(asset_id, stage)
);
CREATE TABLE IF NOT EXISTS duplicate_candidates (
  left_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  right_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'suspected',
  checked_sha256_at TEXT,
  PRIMARY KEY(left_asset_id, right_asset_id)
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,
  library_id TEXT REFERENCES libraries(library_id),
  status TEXT NOT NULL,
  cursor TEXT,
  heartbeat_at TEXT,
  error_summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
```

- [ ] **Step 4: 实现数据库封装**

```python
# media_catalog/database.py
from contextlib import contextmanager
from pathlib import Path
import sqlite3

class CatalogDatabase:
    def __init__(self, path: Path, connection: sqlite3.Connection):
        self.path, self.connection = path, connection

    @classmethod
    def open(cls, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return cls(path, conn)

    def migrate(self):
        sql = (Path(__file__).parent / "migrations" / "001_initial.sql").read_text("utf-8")
        with self.connection:
            self.connection.executescript(sql)
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, datetime('now'))"
            )

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection

    def close(self):
        self.connection.close()
```

- [ ] **Step 5: 运行全部测试并提交**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部 `OK`。

```powershell
git add media_catalog/migrations media_catalog/database.py tests/test_database.py
git commit -m "feat: add media catalog sqlite schema"
```

### Task 3: 实现素材库注册与可迁移挂载位置

**Files:**
- Create: `media_catalog/libraries.py`
- Create: `tests/test_libraries.py`
- Create: `config/media-libraries.example.json`

**Interfaces:**
- Produces: `register_library(db, library_id, name, kind, mount_path)`、`update_mount(db, library_id, mount_path)`、`resolve_location(db, asset_id)`。

- [ ] **Step 1: 写换盘符恢复引用的失败测试**

```python
# tests/test_libraries.py
import tempfile, unittest
from pathlib import Path
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library, update_mount

class LibraryTests(unittest.TestCase):
    def test_mount_can_change_without_changing_library_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp)/"c.sqlite"); db.migrate()
            register_library(db, "park-01", "项目库", "local", r"D:\素材")
            update_mount(db, "park-01", r"E:\素材")
            row = db.connection.execute("SELECT * FROM libraries WHERE library_id='park-01'").fetchone()
            self.assertEqual(row["mount_path"], r"E:\素材")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_libraries -v`

Expected: FAIL，缺少 `media_catalog.libraries`。

- [ ] **Step 3: 实现注册和更新**

```python
# media_catalog/libraries.py
from pathlib import Path

def register_library(db, library_id: str, name: str, kind: str, mount_path: str):
    if kind not in {"local", "nas"}: raise ValueError("kind must be local or nas")
    now = db.connection.execute("SELECT datetime('now')").fetchone()[0]
    with db.transaction() as conn:
        conn.execute("""INSERT INTO libraries VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(library_id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
        mount_path=excluded.mount_path, updated_at=excluded.updated_at""",
        (library_id, name, kind, str(Path(mount_path)), 1, now, now))

def update_mount(db, library_id: str, mount_path: str):
    with db.transaction() as conn:
        changed = conn.execute(
            "UPDATE libraries SET mount_path=?, updated_at=datetime('now') WHERE library_id=?",
            (str(Path(mount_path)), library_id)).rowcount
    if changed != 1: raise KeyError(library_id)
```

- [ ] **Step 4: 添加示例配置**

```json
{
  "libraries": [
    {"library_id": "park-local-01", "name": "项目本地素材库", "kind": "local", "mount_path": "D:\\园区素材"}
  ]
}
```

- [ ] **Step 5: 全测并提交**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -v`

```powershell
git add media_catalog/libraries.py tests/test_libraries.py config/media-libraries.example.json
git commit -m "feat: register movable media libraries"
```

### Task 4: 实现流式发现、媒体分类和两级指纹

**Files:**
- Create: `media_catalog/fingerprint.py`
- Create: `media_catalog/scanner.py`
- Create: `tests/test_scanner.py`

**Interfaces:**
- Produces: `quick_fingerprint(path, block_size=1048576)`、`full_sha256(path)`、`discover_files(root)`、`scan_library(db, library_id)`。

- [ ] **Step 1: 写指纹与改名重连失败测试**

```python
# tests/test_scanner.py
import tempfile, unittest
from pathlib import Path
from media_catalog.fingerprint import quick_fingerprint, full_sha256
from media_catalog.scanner import discover_files, scan_library
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library

class ScannerTests(unittest.TestCase):
    def test_discovery_ignores_non_media_and_fingerprint_survives_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); a = root/"a.mp4"; a.write_bytes(b"x"*4096+b"middle"+b"y"*4096)
            (root/"note.txt").write_text("ignore", "utf-8")
            first = quick_fingerprint(a); renamed = root/"改名.mp4"; a.rename(renamed)
            self.assertEqual(first, quick_fingerprint(renamed))
            self.assertEqual([renamed], list(discover_files(root)))
            self.assertEqual(len(full_sha256(renamed)), 64)

    def test_quick_collision_creates_candidate_without_merging_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/"a.mp4").write_bytes(b"a"); (root/"b.mp4").write_bytes(b"b")
            db = CatalogDatabase.open(root/"catalog.sqlite"); db.migrate()
            register_library(db, "lib", "测试库", "local", str(root))
            summary = scan_library(db, "lib", fingerprint_fn=lambda _: "same")
            self.assertEqual(summary.completed, 2)
            self.assertEqual(db.connection.execute("SELECT count(*) FROM assets").fetchone()[0], 2)
            self.assertEqual(db.connection.execute("SELECT count(*) FROM duplicate_candidates").fetchone()[0], 1)

    def test_rename_reuses_asset_id_when_file_identity_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); original = root/"a.mp4"; original.write_bytes(b"video")
            db = CatalogDatabase.open(root/"catalog.sqlite"); db.migrate()
            register_library(db, "lib", "测试库", "local", str(root))
            scan_library(db, "lib")
            before = db.connection.execute("SELECT asset_id FROM asset_locations").fetchone()[0]
            original.rename(root/"renamed.mp4"); scan_library(db, "lib")
            rows = db.connection.execute("SELECT asset_id,relative_path FROM asset_locations").fetchall()
            self.assertEqual([(before, "renamed.mp4")], [(r[0], r[1]) for r in rows])

    def test_repeat_scan_without_file_identity_does_not_duplicate_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/"a.mp4").write_bytes(b"video")
            db = CatalogDatabase.open(root/"catalog.sqlite"); db.migrate()
            register_library(db, "lib", "测试库", "local", str(root))
            scan_library(db, "lib", identity_fn=lambda _: "")
            scan_library(db, "lib", identity_fn=lambda _: "")
            self.assertEqual(db.connection.execute("SELECT count(*) FROM assets").fetchone()[0], 1)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_scanner -v`

- [ ] **Step 3: 实现流式指纹**

```python
# media_catalog/fingerprint.py
import hashlib
from pathlib import Path

def quick_fingerprint(path: Path, block_size: int = 1024 * 1024) -> str:
    size = path.stat().st_size
    h = hashlib.sha256(); h.update(str(size).encode("ascii"))
    with path.open("rb") as f:
        h.update(f.read(block_size))
        if size > block_size: f.seek(max(0, size - block_size)); h.update(f.read(block_size))
    return h.hexdigest()

def full_sha256(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""): h.update(chunk)
    return h.hexdigest()
```

```python
# media_catalog/scanner.py
from pathlib import Path
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".jpg", ".jpeg", ".png", ".webp", ".wav", ".mp3", ".m4a"}

def discover_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            yield path

def file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else ""

def scan_library(db, library_id: str, fingerprint_fn=quick_fingerprint,
                 identity_fn=file_identity) -> ScanSummary:
    library = db.connection.execute(
        "SELECT mount_path FROM libraries WHERE library_id=?", (library_id,)
    ).fetchone()
    if library is None: raise KeyError(library_id)
    root = Path(library["mount_path"])
    paths = list(discover_files(root))
    completed = failed = 0
    for path in paths:
        try:
            stat = path.stat(); identity = identity_fn(path); fingerprint = fingerprint_fn(path)
            relative_path = str(path.relative_to(root))
            same_path = db.connection.execute("""
                SELECT al.location_id,al.asset_id,al.mtime_ns,a.size_bytes,a.quick_fingerprint
                FROM asset_locations al JOIN assets a USING(asset_id)
                WHERE al.library_id=? AND al.relative_path=?
            """, (library_id, relative_path)).fetchone()
            if same_path:
                with db.transaction() as conn:
                    conn.execute("""UPDATE asset_locations SET mtime_ns=?,file_identity=?,state='available'
                                    WHERE location_id=?""",
                                 (stat.st_mtime_ns, identity, same_path["location_id"]))
                    if (same_path["mtime_ns"], same_path["size_bytes"], same_path["quick_fingerprint"]) != (
                            stat.st_mtime_ns, stat.st_size, fingerprint):
                        conn.execute("""UPDATE assets SET size_bytes=?,quick_fingerprint=?,full_sha256=NULL
                                        WHERE asset_id=?""",
                                     (stat.st_size, fingerprint, same_path["asset_id"]))
                        conn.execute("""UPDATE analysis_runs SET status='stale',updated_at=datetime('now')
                                        WHERE asset_id=? AND status='completed'""", (same_path["asset_id"],))
                completed += 1
                continue
            existing = db.connection.execute(
                "SELECT location_id,asset_id FROM asset_locations WHERE library_id=? AND file_identity<>'' AND file_identity=?",
                (library_id, identity)).fetchone()
            if existing:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE asset_locations SET relative_path=?,mtime_ns=?,state='available' WHERE location_id=?",
                        (relative_path, stat.st_mtime_ns, existing["location_id"]))
                completed += 1
                continue
            asset_id = "ast_" + uuid.uuid4().hex
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) VALUES(?,?,?,?,datetime('now'))",
                    (asset_id, classify_media(path).value, stat.st_size, fingerprint))
                conn.execute(
                    "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,file_identity) VALUES(?,?,?,?,?)",
                    (asset_id, library_id, relative_path, stat.st_mtime_ns, identity))
                matches = conn.execute(
                    "SELECT asset_id FROM assets WHERE size_bytes=? AND quick_fingerprint=? AND asset_id<>?",
                    (stat.st_size, fingerprint, asset_id)).fetchall()
                for row in matches:
                    left, right = sorted((row["asset_id"], asset_id))
                    conn.execute(
                        "INSERT OR IGNORE INTO duplicate_candidates(left_asset_id,right_asset_id,reason) VALUES(?,?,'quick_fingerprint')",
                        (left, right))
            completed += 1
        except (OSError, sqlite3.Error):
            failed += 1
    return ScanSummary(discovered=len(paths), completed=completed, failed=failed)
```

在 `scanner.py` 顶部补齐 `uuid`、`sqlite3`、`quick_fingerprint`、`MediaKind` 和 `ScanSummary` 导入，并实现 `classify_media(path)`，按扩展名返回 `MediaKind.VIDEO / IMAGE / AUDIO / OTHER`。每轮扫描结束后，只把数据库中本轮未见到的位置标记为 `missing`，不删除资产。`file_identity` 不可用或跨卷变化时，只写入 `duplicate_candidates` 等待人工/完整哈希确认，不自动重连。

- [ ] **Step 4: 测试内存不依赖总文件大小**

在 `tests/test_scanner.py` 增加以下测试，确认完整哈希按块读取：

```python
from unittest.mock import call, mock_open, patch

def test_full_hash_never_requests_unbounded_read(self):
    handle = mock_open(read_data=b"abc").return_value
    handle.read.side_effect = [b"abc", b""]
    with patch.object(Path, "open", return_value=handle):
        full_sha256(Path("large.mp4"), chunk_size=1024)
    handle.read.assert_any_call(1024)
    self.assertNotIn(call(), handle.read.call_args_list)
```

然后运行：

Run: `.venv\Scripts\python.exe -m unittest tests.test_scanner -v`

- [ ] **Step 5: 全测并提交**

```powershell
git add media_catalog/fingerprint.py media_catalog/scanner.py tests/test_scanner.py
git commit -m "feat: scan media with safe fingerprints"
```

### Task 5: 实现 ffprobe 发现、技术探测和可读诊断

**Files:**
- Create: `media_catalog/tools.py`
- Create: `media_catalog/probe.py`
- Create: `tests/test_tools.py`
- Create: `tests/test_probe.py`

**Interfaces:**
- Produces: `find_ffprobe(env, which, search_roots) -> Path`、`probe_media(path, ffprobe_path, runner) -> dict`、`probe_pending_assets(db, library_id, ffprobe_path, runner) -> ScanSummary`。

- [ ] **Step 1: 写显式路径优先和缺失诊断测试**

```python
# tests/test_tools.py
import unittest
from pathlib import Path
from media_catalog.tools import find_ffprobe, ToolNotFound

class ToolTests(unittest.TestCase):
    def test_explicit_ffprobe_path_wins(self):
        result = find_ffprobe({"FFPROBE_PATH": r"C:\tools\ffprobe.exe"}, lambda _: None, [], exists=lambda p: True)
        self.assertEqual(result, Path(r"C:\tools\ffprobe.exe"))

    def test_missing_tool_has_actionable_message(self):
        with self.assertRaisesRegex(ToolNotFound, "FFPROBE_PATH"):
            find_ffprobe({}, lambda _: None, [], exists=lambda p: False)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_tools tests.test_probe -v`

- [ ] **Step 3: 实现工具发现与探测器**

```python
# media_catalog/tools.py
from pathlib import Path
import shutil

class ToolNotFound(RuntimeError): pass

def find_ffprobe(env, which=shutil.which, search_roots=(), exists=lambda p: p.exists()):
    if env.get("FFPROBE_PATH"):
        p = Path(env["FFPROBE_PATH"])
        if exists(p): return p
    found = which("ffprobe")
    if found: return Path(found)
    for root in map(Path, search_roots):
        for p in root.glob("**/ffprobe.exe"):
            if exists(p): return p
    raise ToolNotFound("找不到 ffprobe；请设置 FFPROBE_PATH 或把 FFmpeg bin 加入 PATH")
```

```python
# media_catalog/probe.py
import json, subprocess

def probe_media(path, ffprobe_path, runner=subprocess.run):
    cp = runner([str(ffprobe_path), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    if cp.returncode != 0: raise RuntimeError(cp.stderr[-2000:] or "ffprobe failed")
    return json.loads(cp.stdout)
```

- [ ] **Step 4: 测试损坏文件、超时和中文路径**

在 `tests/test_probe.py` 写入：

```python
import json, subprocess, unittest
from pathlib import Path
from types import SimpleNamespace
from media_catalog.probe import probe_media

class ProbeTests(unittest.TestCase):
    def test_chinese_path_is_passed_as_one_argument(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": []}), stderr="")
        self.assertEqual(probe_media(Path("项目/厂房.mp4"), Path("ffprobe.exe"), runner), {"streams": []})
        self.assertEqual(calls[0][-1], "项目\\厂房.mp4")

    def test_failure_summary_is_bounded(self):
        def runner(args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="x" * 5000)
        with self.assertRaisesRegex(RuntimeError, "x{100}") as caught:
            probe_media(Path("broken.mp4"), Path("ffprobe.exe"), runner)
        self.assertLessEqual(len(str(caught.exception)), 2000)

    def test_timeout_is_not_swallowed(self):
        def runner(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 60)
        with self.assertRaises(subprocess.TimeoutExpired):
            probe_media(Path("slow.mp4"), Path("ffprobe.exe"), runner)

    def test_probe_results_are_persisted_and_completed_assets_are_not_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/"a.mp4").write_bytes(b"video")
            db = CatalogDatabase.open(root/"catalog.sqlite"); db.migrate()
            register_library(db, "lib", "测试库", "local", str(root)); scan_library(db, "lib")
            calls = []
            def runner(args, **kwargs):
                calls.append(args); return SimpleNamespace(returncode=0, stdout='{"streams":[]}', stderr="")
            first = probe_pending_assets(db, "lib", Path("ffprobe.exe"), runner)
            second = probe_pending_assets(db, "lib", Path("ffprobe.exe"), runner)
            row = db.connection.execute("SELECT status,result_json FROM analysis_runs WHERE stage='technical_probe'").fetchone()
            self.assertEqual((first.completed, second.completed, len(calls)), (1, 0, 1))
            self.assertEqual(row["status"], "completed")
            self.assertEqual(json.loads(row["result_json"]), {"streams": []})
```

为该测试补齐 `tempfile`、`CatalogDatabase`、`register_library`、`scan_library` 和 `probe_pending_assets` 导入。

在 `media_catalog/probe.py` 增加：

```python
def probe_pending_assets(db, library_id, ffprobe_path, runner=subprocess.run):
    rows = db.connection.execute("""
      SELECT a.asset_id,l.mount_path,al.relative_path
      FROM assets a JOIN asset_locations al USING(asset_id)
      JOIN libraries l USING(library_id)
      LEFT JOIN analysis_runs ar ON ar.asset_id=a.asset_id AND ar.stage='technical_probe'
      WHERE al.library_id=? AND al.state='available'
        AND (ar.status IS NULL OR ar.status IN ('failed','interrupted','stale'))
    """, (library_id,)).fetchall()
    completed = failed = 0
    for row in rows:
        try:
            result = probe_media(Path(row["mount_path"])/row["relative_path"], ffprobe_path, runner)
            status, result_json, error = "completed", json.dumps(result, ensure_ascii=False), None
            completed += 1
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            status, result_json, error = "failed", None, f"{type(exc).__name__}: {exc}"[-2000:]
            failed += 1
        with db.transaction() as conn:
            conn.execute("""INSERT INTO analysis_runs(asset_id,stage,status,result_json,error_summary,updated_at)
              VALUES(?,'technical_probe',?,?,?,datetime('now'))
              ON CONFLICT(asset_id,stage) DO UPDATE SET status=excluded.status,
              result_json=excluded.result_json,error_summary=excluded.error_summary,updated_at=excluded.updated_at""",
              (row["asset_id"], status, result_json, error))
    return ScanSummary(discovered=len(rows), completed=completed, failed=failed)
```

Run: `.venv\Scripts\python.exe -m unittest tests.test_tools tests.test_probe -v`

- [ ] **Step 5: 全测并提交**

```powershell
git add media_catalog/tools.py media_catalog/probe.py tests/test_tools.py tests/test_probe.py
git commit -m "feat: probe media with configurable ffprobe"
```

### Task 6: 实现任务状态、心跳、断点恢复和单重任务锁

**Files:**
- Create: `media_catalog/jobs.py`
- Create: `tests/test_jobs.py`

**Interfaces:**
- Produces: `JobManager.start()`, `heartbeat(cursor)`, `complete()`, `fail(summary)`, `recover_interrupted(max_age_seconds)`。

- [ ] **Step 1: 写崩溃恢复和并发拒绝失败测试**

```python
# tests/test_jobs.py
import tempfile, unittest
from pathlib import Path
from media_catalog.database import CatalogDatabase
from media_catalog.jobs import JobManager, HeavyJobBusy
from media_catalog.libraries import register_library

class JobTests(unittest.TestCase):
    def test_only_one_heavy_job_and_stale_running_becomes_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase.open(Path(tmp)/"c.sqlite"); db.migrate()
            register_library(db, "park-01", "项目库", "local", tmp)
            first = JobManager(db, "scan-1", "scan", "park-01"); first.start()
            with self.assertRaises(HeavyJobBusy):
                JobManager(db, "scan-2", "scan", "park-01").start()
            db.connection.execute("UPDATE jobs SET heartbeat_at='2000-01-01 00:00:00'"); db.connection.commit()
            self.assertEqual(first.recover_interrupted(300), 1)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_jobs -v`

- [ ] **Step 3: 实现事务锁和恢复**

`start()` 必须使用 `BEGIN IMMEDIATE`，先查询 `status='running'` 的重任务；存在时抛 `HeavyJobBusy("已有重任务运行，请勿同时剪辑两条视频")`。每完成一个文件更新 `cursor` 和 `heartbeat_at`；只有心跳过期才转 `interrupted`。

```python
class HeavyJobBusy(RuntimeError): pass

def bounded_error(exc: BaseException, limit: int = 2000) -> str:
    return f"{type(exc).__name__}: {exc}"[-limit:]

class JobManager:
    HEAVY_TYPES = {"scan", "probe", "proxy", "render"}

    def __init__(self, db, job_id: str, job_type: str, library_id: str | None = None):
        self.db, self.job_id, self.job_type, self.library_id = db, job_id, job_type, library_id

    def start(self) -> None:
        conn = self.db.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            if self.job_type in self.HEAVY_TYPES:
                running = conn.execute(
                    "SELECT job_id FROM jobs WHERE status='running' AND job_type IN ('scan','probe','proxy','render')"
                ).fetchone()
                if running and running["job_id"] != self.job_id:
                    raise HeavyJobBusy("已有重任务运行，请勿同时剪辑两条视频")
            conn.execute("""INSERT INTO jobs(job_id,job_type,library_id,status,heartbeat_at,created_at,updated_at)
                            VALUES(?,?,?,'running',datetime('now'),datetime('now'),datetime('now'))
                            ON CONFLICT(job_id) DO UPDATE SET status='running',
                            heartbeat_at=datetime('now'),updated_at=datetime('now'),error_summary=NULL""",
                         (self.job_id, self.job_type, self.library_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def heartbeat(self, cursor: str | None = None) -> None:
        with self.db.transaction() as conn:
            changed = conn.execute("""UPDATE jobs SET cursor=?,heartbeat_at=datetime('now'),updated_at=datetime('now')
                                      WHERE job_id=? AND status='running'""",
                                   (cursor, self.job_id)).rowcount
            if changed != 1:
                raise RuntimeError(f"任务不可更新心跳: {self.job_id}")

    def resume_cursor(self) -> str | None:
        row = self.db.connection.execute("SELECT cursor FROM jobs WHERE job_id=?", (self.job_id,)).fetchone()
        return None if row is None else row["cursor"]

    def complete(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("""UPDATE jobs SET status='completed',heartbeat_at=datetime('now'),
                            updated_at=datetime('now') WHERE job_id=?""", (self.job_id,))

    def fail(self, error: BaseException | str) -> None:
        summary = error[-2000:] if isinstance(error, str) else bounded_error(error)
        with self.db.transaction() as conn:
            conn.execute("""UPDATE jobs SET status='failed',error_summary=?,heartbeat_at=datetime('now'),
                            updated_at=datetime('now') WHERE job_id=?""", (summary, self.job_id))

    def recover_interrupted(self, max_age_seconds: int) -> int:
        modifier = f"-{int(max_age_seconds)} seconds"
        with self.db.transaction() as conn:
            return conn.execute("""UPDATE jobs SET status='interrupted',updated_at=datetime('now')
                WHERE status='running' AND heartbeat_at < datetime('now', ?)""", (modifier,)).rowcount
```

- [ ] **Step 4: 增加重启后从 cursor 继续的测试并运行**

在 `tests/test_jobs.py` 增加：

```python
def test_resume_returns_last_completed_cursor(self):
    with tempfile.TemporaryDirectory() as tmp:
        db = CatalogDatabase.open(Path(tmp)/"c.sqlite"); db.migrate()
        register_library(db, "park-01", "项目库", "local", tmp)
        job = JobManager(db, "scan-1", "scan", "park-01")
        job.start(); job.heartbeat("厂房/a.mp4")
        resumed = JobManager(db, "scan-1", "scan", "park-01")
        self.assertEqual(resumed.resume_cursor(), "厂房/a.mp4")
```

Run: `.venv\Scripts\python.exe -m unittest tests.test_jobs -v`

Expected: 并发被拒绝；过期任务变为 `interrupted`；新任务读取最后 cursor 后继续。

- [ ] **Step 5: 全测并提交**

```powershell
git add media_catalog/jobs.py tests/test_jobs.py
git commit -m "feat: resume catalog jobs safely"
```

### Task 7: 实现备份恢复、磁盘预估和 CLI

**Files:**
- Create: `media_catalog/backup.py`
- Create: `media_catalog/estimate.py`
- Create: `media_catalog/cli.py`
- Create: `scripts/media_catalog.py`
- Create: `tests/test_backup.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- CLI commands: `init`, `library add`, `library mount`, `scan`, `status`, `search`, `backup`, `restore`, `doctor`。
- CLI output: 默认人类可读摘要；`--json` 输出稳定 JSON，供未来 Agent 和 TypeScript 适配器读取。

- [ ] **Step 1: 写备份恢复和 JSON CLI 失败测试**

```python
# tests/test_backup.py
import tempfile, unittest
from pathlib import Path
from media_catalog.backup import backup_database, restore_database
from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library

class BackupTests(unittest.TestCase):
    def test_backup_can_restore_openable_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/"catalog.sqlite"; target = root/"restored.sqlite"
            db = CatalogDatabase.open(source); db.migrate()
            register_library(db, "lib", "测试库", "local", str(root)); db.close()
            backup = backup_database(source, root/"backups")
            restore_database(backup, target)
            restored = CatalogDatabase.open(target)
            row = restored.connection.execute("SELECT library_id FROM libraries").fetchone()
            self.assertEqual(row["library_id"], "lib")
            self.assertEqual(restored.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
```

```python
# tests/test_cli.py
import io, json, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from media_catalog.cli import main

class CliTests(unittest.TestCase):
    def test_status_json_has_stable_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO(); db_path = Path(tmp)/"catalog.sqlite"
            self.assertEqual(main(["--db", str(db_path), "init"]), 0)
            with redirect_stdout(out):
                self.assertEqual(main(["--db", str(db_path), "status", "--json"]), 0)
            result = json.loads(out.getvalue())
            self.assertEqual(set(result), {"libraries", "assets", "jobs", "warnings"})
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_backup tests.test_cli -v`

- [ ] **Step 3: 实现 SQLite 在线备份和安全恢复**

实现以下精确接口：

```python
# media_catalog/backup.py
from datetime import datetime
from pathlib import Path
import shutil, sqlite3

def backup_database(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"catalog-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
    src = sqlite3.connect(source); dst = sqlite3.connect(target)
    try: src.backup(dst)
    finally: dst.close(); src.close()
    return target

def restore_database(backup: Path, target: Path) -> None:
    check = sqlite3.connect(backup)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份数据库完整性检查失败")
    finally: check.close()
    if target.exists(): shutil.copy2(target, target.with_suffix(".pre-restore.sqlite"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)
```

- [ ] **Step 4: 实现 CLI 和用户摘要**

```python
# scripts/media_catalog.py
from media_catalog.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
```

`status` 人类可读输出固定为：

```text
快速盘点：完成
已登记素材：180
技术探测：176 / 180
当前阻塞：2个文件损坏，2个等待ffprobe
预计新增磁盘：仅数据库与日志，不生成代理
```

`doctor` 检查 Python、数据库目录、ffprobe、素材库在线状态、剩余空间和单重任务锁；不得打印密钥或完整环境变量。

`media_catalog/estimate.py` 暴露 `estimate_p0_bytes(file_count: int) -> int`，按每个素材记录 8 KiB 加 20% 余量估算 P0 数据库与日志；P0 不计算代理空间。`cli.py` 使用 `argparse`，`main(argv: list[str] | None = None) -> int`，所有命令都通过该函数返回退出码，不在库函数中调用 `sys.exit()`。

- [ ] **Step 5: 全测并提交**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -v`

```powershell
git add media_catalog/backup.py media_catalog/estimate.py media_catalog/cli.py scripts/media_catalog.py tests/test_backup.py tests/test_cli.py
git commit -m "feat: add media catalog cli and recovery"
```

### Task 8: 文档、真实10GB验收和P0发布门禁

**Files:**
- Modify: `README.md`
- Modify: `docs/user-guide/video-production-user-guide.md`
- Create: `docs/validation/p0-media-catalog-validation.md`
- Modify: `docs/superpowers/specs/2026-08-13-large-media-progressive-asset-workflow-design.md`

**Interfaces:**
- Produces: 可由普通同事执行的 P0 命令清单和真实验收报告。

- [ ] **Step 1: 运行全部自动测试**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部 `OK`，0 failure，0 error。

- [ ] **Step 2: 运行静态语法检查**

Run: `.venv\Scripts\python.exe -m compileall media_catalog scripts/media_catalog.py tests`

Expected: exit 0。

- [ ] **Step 3: 用真实10—50GB目录执行验收**

```powershell
.venv\Scripts\python.exe scripts/media_catalog.py init
.venv\Scripts\python.exe scripts/media_catalog.py library add --id park-real-01 --name "真实项目素材" --kind local --mount "D:\实际素材目录"
.venv\Scripts\python.exe scripts/media_catalog.py doctor
.venv\Scripts\python.exe scripts/media_catalog.py scan --library park-real-01
.venv\Scripts\python.exe scripts/media_catalog.py status --json
```

在扫描中途主动终止一次，再运行同一 `scan`，报告必须记录：总字节、文件数、首扫时间、恢复时间、重复处理数、峰值内存、损坏文件、中文/长路径结果和数据库大小。

- [ ] **Step 4: 验证移动、离线、备份和恢复**

- 在文件身份稳定的同一卷内改名一个素材，确认 `asset_id` 不变；另造快速指纹冲突，确认只生成疑似重复记录、不自动合并。
- 更新挂载路径并确认 `library_id` 和 `asset_id` 不变。
- 临时让 NAS/目录离线，确认状态为离线而不是素材已删除。
- 创建备份、恢复到新数据库并执行 `PRAGMA integrity_check`。

- [ ] **Step 5: 更新文档和状态**

README 与用户手册只描述已实际通过的命令；规格 P0 状态只有在真实验收全部通过后改为“P0已验证”。验证报告写明失败项和复现步骤，不得用“基本通过”替代未完成门禁。

- [ ] **Step 6: 最终验证并提交**

Run: `git diff --check`

```powershell
git add README.md docs/user-guide/video-production-user-guide.md docs/validation/p0-media-catalog-validation.md docs/superpowers/specs/2026-08-13-large-media-progressive-asset-workflow-design.md
git commit -m "docs: validate p0 media catalog workflow"
```

## P0 完成定义

- 真实 10—50GB 混合目录扫描成功，内存不随总字节数线性增长。
- 中断后从最后成功文件继续，不重复完成项。
- 文件改名、目录移动和盘符变化后能恢复引用。
- NAS离线状态明确，不把离线素材标记为已删除。
- 快速指纹只产生疑似重复；完整 SHA-256 决定是否为同一内容。
- ffprobe 缺失时给出可执行修复提示；路径可配置且不依赖开发机固定目录。
- 数据库备份经过实际恢复和完整性检查。
- 同一时间第二个重任务被明确拒绝，提示不要同时剪辑两条视频。
- P0 不改变现有 `plan / compile / make-ass / render-mix` 行为。
