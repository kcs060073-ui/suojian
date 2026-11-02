"""P2-M4 包装方案（packaging_plan）落库 + 方案推荐引擎。

设计要点：
- 包装资产（音色 / BGM / 音效 / 字幕模板 / 文字卡 / 贴纸 / 花字 / 品牌）的描述在
  ``assets/manifests/*.json`` 里，是仓库核心资产；本模块只读取它们来推方案。
- 每条任务推 2–3 套方案，每套逐槽位锁定 asset_ref + 备用项；无论命中默认模板
  都必须人工 ``confirm`` 后才能进 qa / 渲染（对应设计契约的"包装确认门禁"）。
- 所有 id 不写死在代码里，从 manifest 的 ``default_for`` 字段路由匹配，避免漂移。
"""

from collections.abc import Callable
from pathlib import Path
import json
import os
import shutil
import sqlite3
import subprocess
import uuid

from .provenance import canonical_digest, record_gate, selection_digest
from .workflow import invalidate_from_connection

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "assets" / "manifests"

# 每个分类对应的 manifest 文件名（严格对应设计契约 assets/manifests/*.json）
MANIFEST_FILE = {
    "voice": "voice-profiles.json",
    "bgm": "bgm.json",
    "sfx": "sfx.json",
    "subtitle": "subtitle-presets.json",
    "title_card": "visual-cards.json",
    "sticker": "stickers.json",
    "fancy_text": "fancy-text-presets.json",
}

ROUTES = ("数字人混剪", "园区宣传", "信息流广告")
SLOTS = ("voice", "bgm", "sfx", "subtitle", "title_card", "sticker", "fancy_text", "brand")
REQUIRED_SLOTS = frozenset({"subtitle"})

_TASK_TYPE_ROUTES = {
    "数字人混剪": "数字人混剪",
    "digital_human": "数字人混剪",
    "digital-human": "数字人混剪",
    "园区宣传": "园区宣传",
    "park": "园区宣传",
    "park_promo": "园区宣传",
    "信息流广告": "信息流广告",
    "ad_mix": "信息流广告",
    "information_flow_ad": "信息流广告",
    "ad": "信息流广告",
}

# 每个槽位对应的 manifest 文件名（仓库核心资产）
SLOT_MANIFEST = {
    "voice": "voice-profiles.json",
    "bgm": "bgm.json",
    "sfx": "sfx.json",
    "subtitle": "subtitle-presets.json",
    "title_card": "visual-cards.json",
    "sticker": "stickers.json",
    "fancy_text": "fancy-text-presets.json",
    "brand": "visual-cards.json",
}


# --------------------------------------------------------------------------- #
# manifest 读取
# --------------------------------------------------------------------------- #
def _resolve_env_ref(value):
    """把 ``env:XXX`` 形式的引用解析成环境变量值；非 env: 原样返回。"""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


def load_manifest(category: str) -> dict:
    fname = MANIFEST_FILE.get(category, f"{category}.json")
    path = MANIFEST_DIR / fname
    if not path.is_file():
        return {"category": category, "items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"category": category, "items": []}
    data.setdefault("category", category)
    data.setdefault("items", [])
    return data


def load_all_manifests() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cat in ("voice", "bgm", "sfx", "subtitle", "title_card", "sticker", "fancy_text"):
        out[cat] = load_manifest(cat)
    return out


def find_item(category: str, asset_ref: str) -> dict | None:
    for it in load_manifest(category).get("items", []):
        if it.get("id") == asset_ref:
            return it
    return None


# --------------------------------------------------------------------------- #
# 表操作
# --------------------------------------------------------------------------- #
def _canonical_route(connection, task_id: str) -> str:
    task = connection.execute(
        "SELECT task_type,brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    route = _TASK_TYPE_ROUTES.get(task["task_type"])
    if route is None:
        raise ValueError(
            f"PACKAGING_ROUTE_UNSUPPORTED: 无法从任务类型推导包装路线：{task['task_type']}"
        )
    return route


def _require_canonical_route(connection, task_id: str, supplied: str | None) -> str:
    canonical = _canonical_route(connection, task_id)
    if supplied is not None and supplied != canonical:
        raise ValueError(
            f"PACKAGING_ROUTE_MISMATCH: 任务路线应为 {canonical}，不能使用 {supplied}"
        )
    return canonical


def create_plan(db, task_id: str, route: str | None, plan_name: str) -> str:
    plan_id = f"pp-{uuid.uuid4().hex[:12]}"
    with db.transaction() as conn:
        canonical_route = _require_canonical_route(conn, task_id, route)
        conn.execute(
            """
            INSERT INTO packaging_plans(plan_id, task_id, route, plan_name, status)
            VALUES(?, ?, ?, ?, 'draft')
            """,
            (plan_id, task_id, canonical_route, plan_name),
        )
    return plan_id


def create_blank_plan(
    db, task_id: str, route: str | None = None, plan_name: str = "空白方案"
) -> str:
    """Create an empty task-local composition; it is never implicitly confirmed."""
    return create_plan(db, task_id, route, plan_name)


def _plan_row(connection, plan_id: str):
    row = connection.execute(
        "SELECT * FROM packaging_plans WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if row is None:
        if connection.execute(
            "SELECT 1 FROM packaging_templates WHERE template_id=?", (plan_id,)
        ).fetchone() is not None:
            raise RuntimeError(
                "PACKAGING_TEMPLATE_IMMUTABLE: 个人模板只能克隆到任务后使用"
            )
        raise KeyError(f"找不到包装方案：{plan_id}")
    return row


def _plan_data(connection, plan_id: str) -> dict:
    data = dict(_plan_row(connection, plan_id))
    data["items"] = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM packaging_plan_items WHERE plan_id=? "
            "ORDER BY slot,is_backup,rank,item_id",
            (plan_id,),
        ).fetchall()
    ]
    data["resource_kind"] = "task_packaging_plan"
    return data


def _template_data(connection, template_id: str) -> dict | None:
    row = connection.execute(
        "SELECT * FROM packaging_templates WHERE template_id=?", (template_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        snapshot = json.loads(row["snapshot_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "PACKAGING_TEMPLATE_INVALID: 个人模板快照无法解析"
        ) from error
    return {
        "template_id": row["template_id"],
        "template_name": row["template_name"],
        "route": row["route"],
        "snapshot_digest": row["snapshot_digest"],
        "created_at": row["created_at"],
        "status": "template",
        "resource_kind": "personal_packaging_template",
        "items": snapshot.get("items", []),
        "reference_policy": snapshot.get("reference_policy"),
    }


def _require_editable(connection, plan_id: str):
    row = _plan_row(connection, plan_id)
    if row["origin_type"] == "system_recommendation":
        raise RuntimeError(
            "PACKAGING_RECOMMENDATION_IMMUTABLE: 系统推荐方案必须克隆后修改"
        )
    if row["status"] == "confirmed":
        raise RuntimeError("PACKAGING_PLAN_CONFIRMED: 已确认方案必须克隆为新 draft 后修改")
    return row


def reference_policy_for_plan(db, plan_id):
    exists = db.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='packaging_reference_rules'").fetchone()
    if not exists:
        return None
    row = db.connection.execute("SELECT policy_json FROM packaging_reference_rules WHERE plan_id=?", (plan_id,)).fetchone()
    return json.loads(row[0]) if row else None


def clone_plan(
    db,
    plan_id: str,
    *,
    task_id: str | None = None,
    plan_name: str | None = None,
) -> str:
    """Clone a recommendation, task plan, or personal template into a new draft."""
    new_plan_id = f"pp-{uuid.uuid4().hex[:12]}"
    with db.transaction() as connection:
        source_row = connection.execute(
            "SELECT * FROM packaging_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        template = None if source_row is not None else _template_data(connection, plan_id)
        if source_row is None and template is None:
            raise KeyError(f"找不到包装方案或模板：{plan_id}")
        if template is not None:
            if task_id is None:
                raise ValueError("PACKAGING_TEMPLATE_TARGET_REQUIRED: 使用模板必须指定目标任务")
            target_task_id = task_id
            source_items = template["items"]
            target_name = plan_name or template["template_name"]
        else:
            source = dict(source_row)
            target_task_id = task_id or source["task_id"]
            source_items = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM packaging_plan_items WHERE plan_id=? "
                    "ORDER BY slot,is_backup,rank,item_id",
                    (plan_id,),
                ).fetchall()
            ]
            target_name = plan_name or f"{source['plan_name']} · 副本"
        target_route = _require_canonical_route(connection, target_task_id, None)
        _validate_items(source_items, target_route, require_required=False)
        connection.execute(
            "INSERT INTO packaging_plans(plan_id,task_id,route,plan_name,status,origin_type) "
            "VALUES(?,?,?,?, 'draft','custom')",
            (new_plan_id, target_task_id, target_route, target_name),
        )
        for item in source_items:
            connection.execute(
                """
                INSERT INTO packaging_plan_items(
                  plan_id,slot,asset_ref,asset_file,asset_version,is_backup,rank,notes
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    new_plan_id, item["slot"], item["asset_ref"],
                    item.get("asset_file"), item.get("asset_version"),
                    int(bool(item.get("is_backup"))), item.get("rank", 0),
                    item.get("notes"),
                ),
            )
        policy = template.get("reference_policy") if template else reference_policy_for_plan(db, plan_id)
        if policy:
            connection.execute("CREATE TABLE IF NOT EXISTS packaging_reference_rules (plan_id TEXT PRIMARY KEY REFERENCES packaging_plans(plan_id), policy_json TEXT NOT NULL)")
            connection.execute("INSERT INTO packaging_reference_rules VALUES (?,?)", (new_plan_id, json.dumps(policy, ensure_ascii=False)))
    return new_plan_id


def add_item(
    db,
    plan_id: str,
    slot: str,
    asset_ref: str | None,
    asset_file: str | None = None,
    asset_version: str | None = None,
    is_backup: bool = False,
    rank: int = 0,
    notes: str | None = None,
) -> None:
    if slot not in SLOTS:
        raise ValueError(f"slot 必须是 {SLOTS} 之一")
    if not isinstance(asset_ref, str) or not asset_ref.strip():
        raise ValueError("asset_ref 不能为空")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < 0:
        raise ValueError("rank 必须是非负整数")
    with db.transaction() as conn:
        _require_editable(conn, plan_id)
        duplicate = conn.execute(
            "SELECT 1 FROM packaging_plan_items "
            "WHERE plan_id=? AND slot=? AND is_backup=? AND rank=?",
            (plan_id, slot, 1 if is_backup else 0, rank),
        ).fetchone()
        if duplicate is not None:
            raise RuntimeError(
                f"PACKAGING_ITEM_DUPLICATE: 重复槽位身份：{slot}/{int(bool(is_backup))}/{rank}"
            )
        conn.execute(
            """
            INSERT INTO packaging_plan_items
              (plan_id, slot, asset_ref, asset_file, asset_version, is_backup, rank, notes)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                slot,
                asset_ref,
                asset_file,
                asset_version,
                1 if is_backup else 0,
                rank,
                notes,
            ),
        )


def replace_item(
    db,
    plan_id: str,
    slot: str,
    asset_ref: str,
    asset_file: str | None = None,
    asset_version: str | None = None,
    notes: str | None = None,
) -> dict:
    """Replace one complete packaging slot on a draft without touching other slots."""
    if slot not in SLOTS:
        raise ValueError(f"slot 必须是 {SLOTS} 之一")
    if not asset_ref:
        raise ValueError("asset_ref 不能为空")
    category = "title_card" if slot == "brand" else slot
    manifest_item = find_item(category, asset_ref)
    if asset_file is None and manifest_item is not None:
        asset_file = manifest_item.get("file")
    with db.transaction() as connection:
        _require_editable(connection, plan_id)
        connection.execute(
            "DELETE FROM packaging_plan_items WHERE plan_id=? AND slot=?",
            (plan_id, slot),
        )
        connection.execute(
            """
            INSERT INTO packaging_plan_items(
              plan_id,slot,asset_ref,asset_file,asset_version,is_backup,rank,notes
            ) VALUES(?,?,?,?,?,0,0,?)
            """,
            (plan_id, slot, asset_ref, asset_file, asset_version, notes),
        )
    result = get_plan(db, plan_id)
    assert result is not None
    return result


def disable_optional_slot(db, plan_id: str, slot: str) -> dict:
    """Disable an optional slot by removing its complete draft selection."""
    if slot not in SLOTS:
        raise ValueError(f"slot 必须是 {SLOTS} 之一")
    if slot in REQUIRED_SLOTS:
        raise ValueError(f"required slot 不可关闭：{slot}")
    with db.transaction() as connection:
        _require_editable(connection, plan_id)
        connection.execute(
            "DELETE FROM packaging_plan_items WHERE plan_id=? AND slot=?",
            (plan_id, slot),
        )
    result = get_plan(db, plan_id)
    assert result is not None
    return result


def save_personal_template(db, plan_id: str, template_name: str, *, reference_policy=None) -> dict:
    """Persist a task-independent immutable composition snapshot."""
    name = template_name.strip()
    if not name:
        raise ValueError("template_name 不能为空")
    template_id = f"pt-{uuid.uuid4().hex[:12]}"
    with db.transaction(immediate=True) as connection:
        source = _plan_data(connection, plan_id)
        route = _require_canonical_route(
            connection, source["task_id"], source["route"]
        )
        _validate_items(source["items"], route, require_required=True)
        snapshot = {
            "schema": 1,
            "route": route,
            "items": _canonical_items(source["items"]),
        }
        policy = reference_policy or reference_policy_for_plan(db, plan_id)
        if policy:
            from .reference_editing import freeze_reference_policy
            snapshot["reference_policy"] = freeze_reference_policy(policy)
            snapshot["schema"] = 2
        snapshot_json = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            """
            INSERT INTO packaging_templates(
              template_id,template_name,route,snapshot_json,snapshot_digest
            ) VALUES(?,?,?,?,?)
            """,
            (
                template_id, name, route, snapshot_json,
                canonical_digest(snapshot),
            ),
        )
    result = _template_data(db.connection, template_id)
    assert result is not None
    return result


def list_personal_templates(db, route: str) -> list[dict]:
    canonical_route = _TASK_TYPE_ROUTES.get(route)
    if canonical_route not in ROUTES:
        raise ValueError(f"PACKAGING_ROUTE_UNSUPPORTED: 不支持的包装路线：{route}")
    rows = db.connection.execute(
        "SELECT template_id FROM packaging_templates WHERE route=? "
        "ORDER BY created_at,template_id",
        (canonical_route,),
    ).fetchall()
    result = []
    for row in rows:
        template = _template_data(db.connection, row["template_id"])
        if template is not None:
            result.append(template)
    return result


def get_plan(db, plan_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT 1 FROM packaging_plans WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if row is None:
        return None
    return _plan_data(db.connection, plan_id)


def _canonical_items(items: list[dict]) -> list[dict]:
    payload_items = [
        {
            key: item.get(key)
            for key in (
                "slot", "asset_ref", "asset_file", "asset_version", "is_backup", "rank", "notes",
            )
        }
        for item in items
    ]
    payload_items.sort(key=lambda item: (
        item["slot"] or "",
        item["asset_ref"] or "",
        item["asset_file"] or "",
        item["asset_version"] or "",
        int(bool(item["is_backup"])),
        -1 if item["rank"] is None else item["rank"],
        item["notes"] or "",
    ))
    return payload_items


def canonical_packaging_gate_payload(plan: dict, items: list[dict] | None = None) -> dict:
    """Build the sole digest input for a confirmed packaging plan."""
    source_items = plan["items"] if items is None else items
    return {
        "plan_id": plan["plan_id"],
        "route": plan["route"],
        "plan_name": plan["plan_name"],
        "items": _canonical_items(source_items),
    }


def _packaging_path(manifest_item: dict, locked_file: str | None) -> Path | None:
    project_root = MANIFEST_DIR.resolve().parents[1]
    manifest_file = manifest_item.get("file")
    if locked_file and manifest_file:
        if Path(locked_file).as_posix() != Path(manifest_file).as_posix():
            raise RuntimeError("PACKAGING_REFERENCE_MISMATCH: 包装文件与 manifest 不一致")
    file_ref = locked_file or manifest_file
    if file_ref:
        path = Path(file_ref)
        resolved = path.resolve() if path.is_absolute() else (project_root / path).resolve()
        allowed = (
            (project_root / "public").resolve(),
            (project_root / "assets").resolve(),
            (project_root / "asset-packs").resolve(),
        )
        if not any(resolved == root or root in resolved.parents for root in allowed):
            raise RuntimeError("PACKAGING_PATH_INVALID: 包装文件路径越界")
        return resolved
    font_ref = manifest_item.get("font")
    if font_ref:
        return (project_root / "assets" / "fonts" / font_ref).resolve()
    return None


def resolve_manifest_asset_path(category: str, asset_ref: str) -> Path | None:
    """Resolve one registered core asset without allowing paths outside the project."""
    item = find_item(category, asset_ref)
    if item is None:
        return None
    return _packaging_path(item, None)


def _audio_file_readable(path: Path) -> bool:
    """Return whether ffprobe can decode a positive-duration local audio asset."""
    try:
        if path.stat().st_size <= 32:
            return False
    except OSError:
        return False
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return path.stat().st_size > 1024
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        duration = float((result.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return False
    return result.returncode == 0 and duration > 0


def _validate_items(
    items: list[dict], route: str, *, require_required: bool
) -> None:
    primary_slots: dict[str, int] = {}
    identities: set[tuple[str, bool, int]] = set()
    for item in items:
        slot = item.get("slot")
        asset_ref = item.get("asset_ref")
        rank = item.get("rank", 0)
        is_backup = bool(item.get("is_backup"))
        if slot not in SLOTS:
            raise RuntimeError(f"PACKAGING_SLOT_INVALID: 非法或空槽位：{slot!r}")
        if not isinstance(asset_ref, str) or not asset_ref.strip():
            raise RuntimeError(f"PACKAGING_ASSET_REF_INVALID: 包装项缺少素材引用：{slot}")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 0:
            raise RuntimeError(f"PACKAGING_RANK_INVALID: 包装项 rank 非法：{slot}/{rank}")
        identity = (slot, is_backup, rank)
        if identity in identities:
            raise RuntimeError(
                f"PACKAGING_ITEM_DUPLICATE: 重复槽位身份：{slot}/{int(is_backup)}/{rank}"
            )
        identities.add(identity)
        if not item["is_backup"]:
            primary_slots[slot] = primary_slots.get(slot, 0) + 1
    if require_required:
        missing = sorted(
            slot for slot in REQUIRED_SLOTS if primary_slots.get(slot, 0) != 1
        )
        if missing:
            raise RuntimeError(
                "PACKAGING_REQUIRED_SLOT_MISSING: 必需槽位缺失或主项不唯一："
                + ", ".join(missing)
            )
    duplicates = sorted(slot for slot, count in primary_slots.items() if count > 1)
    if duplicates:
        raise RuntimeError("PACKAGING_SLOT_AMBIGUOUS: 槽位主项不唯一：" + ", ".join(duplicates))
    for item in items:
        slot = item["slot"]
        category = "title_card" if slot == "brand" else slot
        manifest_item = find_item(category, item["asset_ref"])
        if manifest_item is None:
            raise RuntimeError(
                f"PACKAGING_MANIFEST_UNKNOWN: manifest 未登记包装项：{slot}/{item['asset_ref']}"
            )
        license_name = manifest_item.get("license", "unknown")
        if license_name != "commercial-approved":
            raise RuntimeError(
                f"PACKAGING_LICENSE_BLOCKED: 包装项不可用于商业发布：{slot}/{item['asset_ref']}"
            )
        routes = manifest_item.get("default_for") or []
        if routes and route not in routes:
            raise RuntimeError(
                f"PACKAGING_NOT_APPLICABLE: 包装项不适用于当前路线：{slot}/{item['asset_ref']}"
            )
        file_path = _packaging_path(manifest_item, item["asset_file"])
        if file_path is not None and not file_path.is_file():
            raise RuntimeError(f"PACKAGING_FILE_MISSING: 包装文件不存在：{file_path}")
        if slot in ("bgm", "sfx") and file_path is not None and not _audio_file_readable(file_path):
            raise RuntimeError(f"PACKAGING_FILE_UNREADABLE: 包装音频不可读取：{file_path}")


def _validate_confirmation(plan: dict) -> None:
    _validate_items(plan["items"], plan["route"], require_required=True)


def _require_picture_lock(db, connection, task_id: str) -> dict:
    lock = connection.execute(
        "SELECT changed_since_lock,selection_digest "
        "FROM picture_lock WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if lock is None:
        raise RuntimeError("PICTURE_LOCK_MISSING: 必须先锁定画面")
    if lock["changed_since_lock"]:
        raise RuntimeError("PICTURE_LOCK_CHANGED: 画面锁定后选择已变化")
    current_digest, _ = selection_digest(db, task_id)
    if current_digest != lock["selection_digest"]:
        raise RuntimeError(
            "PICTURE_LOCK_DIGEST_MISMATCH: 选择身份与画面锁不一致"
        )
    return dict(lock)


def validate_plan_for_render(
    db, plan_id: str, *, require_picture_lock: bool = True
) -> dict:
    """Validate a task plan without confirming or mutating it."""
    def validate(connection) -> dict:
        plan = _plan_data(connection, plan_id)
        try:
            route = _require_canonical_route(
                connection, plan["task_id"], plan["route"]
            )
        except ValueError as error:
            if str(error).startswith("PACKAGING_ROUTE_MISMATCH:"):
                raise RuntimeError(str(error)) from error
            raise
        _validate_items(plan["items"], route, require_required=True)
        if require_picture_lock:
            _require_picture_lock(db, connection, plan["task_id"])
        plan["route"] = route
        return plan

    if db.connection.in_transaction:
        return validate(db.connection)
    with db.transaction() as connection:
        return validate(connection)

def list_plans(db, task_id: str) -> list[dict]:
    return [
        dict(r)
        for r in db.connection.execute(
            """
            SELECT p.plan_id, p.task_id, p.route, p.plan_name, p.status, p.created_at,
                   COUNT(i.item_id) AS item_count
            FROM packaging_plans p
            LEFT JOIN packaging_plan_items i USING(plan_id)
            WHERE p.task_id=?
            GROUP BY p.plan_id
            ORDER BY p.created_at, p.rowid
            """,
            (task_id,),
        ).fetchall()
    ]


def confirm_plan(db, plan_id: str, *, evidence: dict | None = None) -> dict:
    def confirm(conn) -> None:
        plan = _plan_data(conn, plan_id)
        task_id = plan["task_id"]
        try:
            canonical_route = _require_canonical_route(
                conn, task_id, plan["route"]
            )
        except ValueError as error:
            if str(error).startswith("PACKAGING_ROUTE_MISMATCH:"):
                raise RuntimeError(str(error)) from error
            raise
        _require_picture_lock(db, conn, task_id)
        plan["route"] = canonical_route
        _validate_confirmation(plan)
        gate_payload = canonical_packaging_gate_payload(plan)
        gate_digest = canonical_digest(gate_payload)
        previous_gate = conn.execute(
            "SELECT gate_digest,details_json FROM task_gate_events "
            "WHERE task_id=? AND gate_type='packaging' "
            "ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if plan["status"] == "confirmed":
            if previous_gate is not None:
                try:
                    previous_details = json.loads(
                        previous_gate["details_json"] or "{}"
                    )
                except (TypeError, json.JSONDecodeError):
                    previous_details = {}
                if (
                    previous_gate["gate_digest"] == gate_digest
                    and previous_details.get("plan_id") == plan_id
                ):
                    return
            raise RuntimeError(
                "PACKAGING_CONFIRM_CONFLICT: 已确认方案与当前包装门禁不一致"
            )
        if plan["status"] != "draft":
            raise RuntimeError(
                f"PACKAGING_CONFIRM_CONFLICT: 方案状态不可确认：{plan['status']}"
            )
        changed = conn.execute(
            "UPDATE packaging_plans "
            "SET status='confirmed',confirmed_at=datetime('now') "
            "WHERE plan_id=? AND status='draft'",
            (plan_id,),
        ).rowcount
        if changed != 1:
            raise RuntimeError(
                "PACKAGING_CONFIRM_CONFLICT: 方案已被其他操作修改"
            )
        if previous_gate is not None and previous_gate["gate_digest"] != gate_digest:
            invalidate_from_connection(
                conn,
                task_id,
                "packaging_confirm",
                reason="confirmed packaging gate replaced",
            )
        conn.execute(
            "UPDATE tasks SET status='packaging_confirmed',updated_at=datetime('now') "
            "WHERE task_id=? AND status IN ('draft','brief_locked','selection_locked')",
            (task_id,),
        )
        gate_details = dict(gate_payload)
        if evidence is not None:
            gate_details["confirmation_evidence"] = dict(evidence)
        record_gate(conn, task_id, "packaging", gate_digest, gate_details)

    if db.connection.in_transaction:
        confirm(db.connection)
    else:
        with db.transaction(immediate=True) as connection:
            confirm(connection)
    result = get_plan(db, plan_id)
    if result is None:
        raise KeyError(f"找不到包装方案：{plan_id}")
    return result


# --------------------------------------------------------------------------- #
# 方案推荐引擎（manifest 驱动）
# --------------------------------------------------------------------------- #
def _item_is_recommendable(item: dict, route: str, category: str) -> bool:
    if (
        not isinstance(item.get("id"), str)
        or not item["id"].strip()
        or item.get("license", "unknown") != "commercial-approved"
    ):
        return False
    routes = item.get("default_for") or []
    if routes and route not in routes:
        return False
    try:
        path = _packaging_path(item, None)
    except RuntimeError:
        return False
    if path is not None and not path.is_file():
        return False
    if category in ("bgm", "sfx") and path is not None:
        return _audio_file_readable(path)
    return True


def _eligible_items(manifests: dict, slot: str, route: str) -> list[dict]:
    category = "title_card" if slot in ("title_card", "brand") else slot
    return [
        item
        for item in manifests.get(category, {}).get("items", [])
        if _item_is_recommendable(item, route, category)
    ]


def _pick_bgm(manifests: dict, route: str, exclude: set[str]) -> dict | None:
    items = _eligible_items(manifests, "bgm", route)
    # 1) 本 route 的默认 BGM
    for it in items:
        if route in (it.get("default_for") or []) and it["id"] not in exclude:
            return it
    # 2) 任意未被排除的 BGM
    for it in items:
        if it["id"] not in exclude:
            return it
    return None


def _first_available(manifests: dict, slot: str, route: str) -> dict | None:
    items = _eligible_items(manifests, slot, route)
    return items[0] if items else None


def propose(db, task_id: str, route: str) -> list[str]:
    """为任务推 2–3 套包装方案（全部 draft 落库），返回 plan_id 列表。

    每套方案结构：
      - 主 BGM（route 默认）→ 备选 BGM1 → 备选 BGM2（若素材够）
      - 默认音色（voice）
      - 品牌框（brand，若有）
      - 字幕模板 / 文字卡 / 贴纸 / 花字（manifest 有才加）
    """
    route = _require_canonical_route(db.connection, task_id, route)
    manifests = load_all_manifests()
    voice = _first_available(manifests, "voice", route)
    brand = _first_available(manifests, "brand", route)
    subtitle = _first_available(manifests, "subtitle", route)
    title_card = _first_available(manifests, "title_card", route)
    sticker = _first_available(manifests, "sticker", route)
    fancy = _first_available(manifests, "fancy_text", route)
    sfx = _first_available(manifests, "sfx", route)
    if subtitle is None:
        return []

    used: set[str] = set()
    plan_ids: list[str] = []
    plan_defs = [
        ("方案A · 推荐主线", False),
        ("方案B · 备选情绪+贴纸", True),
        ("方案C · 备选动态+花字", True),
    ]
    for idx, (name, allow_sticker) in enumerate(plan_defs):
        bgm = _pick_bgm(manifests, route, used)
        if bgm is not None:
            used.add(bgm["id"])

        plan_id = create_plan(db, task_id, route, name)
        # 音色
        if voice is not None:
            add_item(
                db, plan_id, "voice", voice["id"],
                asset_version=voice.get("engine"),
                notes="默认 TTS 音色（env 解析）",
            )
        # BGM
        if bgm is not None:
            add_item(
                db, plan_id, "bgm", bgm["id"], bgm.get("file"),
                asset_version=bgm.get("mood"),
                notes=bgm.get("note"),
            )
        # 品牌框
        if brand is not None:
            add_item(db, plan_id, "brand", brand["id"], brand.get("file"),
                     notes="品牌框/水印")
        # 字幕模板
        if subtitle is not None:
            add_item(db, plan_id, "subtitle", subtitle["id"], subtitle.get("file"))
        # 文字卡（与 brand 同目录，区别 slot）
        if title_card is not None and title_card.get("id") != (brand or {}).get("id"):
            add_item(db, plan_id, "title_card", title_card["id"], title_card.get("file"))
        # 音效库（ffmpeg 合成，每方案默认带）
        if sfx is not None:
            add_item(db, plan_id, "sfx", sfx["id"], sfx.get("file"),
                     notes="音效库（ffmpeg 合成，CC0）")
        # 贴纸 / 花字（方案 B/C 纳入，素材存在才加）
        if allow_sticker:
            if sticker is not None:
                add_item(db, plan_id, "sticker", sticker["id"], sticker.get("file"),
                         notes="贴纸库（PIL 生成透明 PNG）")
            if fancy is not None:
                add_item(db, plan_id, "fancy_text", fancy["id"], fancy.get("file"),
                         notes="花字预设")
        with db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE packaging_plans SET origin_type='system_recommendation' "
                "WHERE plan_id=?",
                (plan_id,),
            )
        plan_ids.append(plan_id)
    return plan_ids


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
VALID_LICENSE = ("commercial-approved", "internal-only", "unknown", "restricted", "prohibited")


def validate_manifests(manifest_dir: Path | None = None) -> dict:
    """校验所有 manifest：文件存在、license 枚举合法、音频文件可读。"""
    base = manifest_dir or MANIFEST_DIR
    root = base.resolve().parents[1]  # 项目根
    report: dict = {"ok": True, "categories": [], "errors": []}
    for cat in ("voice", "bgm", "sfx", "subtitle", "title_card", "sticker", "fancy_text"):
        path = base / MANIFEST_FILE.get(cat, f"{cat}.json")
        if not path.is_file():
            report["errors"].append(f"缺少 manifest 文件：{cat}.json")
            report["ok"] = False
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            report["errors"].append(f"{cat}.json 解析失败：{e}")
            report["ok"] = False
            continue
        cat_report = {"category": cat, "count": 0, "missing_files": [], "bad_license": []}
        for it in data.get("items", []):
            cat_report["count"] += 1
            lic = it.get("license", "unknown")
            if lic not in VALID_LICENSE:
                cat_report["bad_license"].append(it.get("id", "?"))
            f = it.get("file")
            if f:
                fp = root / f
                if not fp.is_file():
                    cat_report["missing_files"].append(f)
        if cat_report["missing_files"] or cat_report["bad_license"]:
            report["ok"] = False
        report["categories"].append(cat_report)
    return report
