"""P2-M4 包装方案（packaging_plan）落库 + 方案推荐引擎。

设计要点：
- 包装资产（BGM / 音效 / 字幕模板 / 文字卡 / 贴纸 / 花字 / 品牌）的描述在
  ``assets/manifests/*.json`` 里，是仓库核心资产；本模块只读取它们来推方案。
- 每条任务推 2–3 套方案，每套逐槽位锁定 asset_ref + 备用项；无论命中默认模板
  都必须人工 ``confirm`` 后才能进 qa / 渲染（对应设计契约的"包装确认门禁"）。
- 所有 id 不写死在代码里，从 manifest 的 ``default_for`` 字段路由匹配，避免漂移。
"""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import uuid

from .provenance import canonical_digest, record_gate, selection_digest, selection_matches_picture_lock
from .workflow import invalidate_from_connection

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "assets" / "manifests"

# 每个分类对应的 manifest 文件名（严格对应设计契约 assets/manifests/*.json）
MANIFEST_FILE = {
    "voice": "voice-profiles.json",
    "bgm": "bgm.json",
    "sfx": "sfx.json",
    "subtitle": "subtitle-presets.json",
    "title_card": "visual-cards.json",
    "info_card": "visual-cards.json",
    "sticker": "stickers.json",
    "fancy_text": "fancy-text-presets.json",
}

ROUTES = ("数字人混剪", "园区宣传", "信息流广告")
SLOTS = ("voice", "bgm", "sfx", "subtitle", "title_card", "info_card", "sticker", "fancy_text", "brand")
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
    "info_card": "visual-cards.json",
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


# Only a read-only detail projection opts in. No cache survives the request;
# confirmation/render/export continue to read their own current evidence.
_manifest_snapshot = ContextVar("packaging_manifest_snapshot", default=None)


@contextmanager
def manifest_read_snapshot():
    token = _manifest_snapshot.set({})
    try:
        yield
    finally:
        _manifest_snapshot.reset(token)


def load_manifest(category: str, db=None) -> dict:
    snapshot = _manifest_snapshot.get()
    if snapshot is None:
        return _load_manifest_uncached(category, db)
    key = (id(db), str(MANIFEST_DIR), category)
    if key not in snapshot:
        snapshot[key] = _load_manifest_uncached(category, db)
    return deepcopy(snapshot[key])


def _load_manifest_uncached(category: str, db=None) -> dict:
    fname = MANIFEST_FILE.get(category, f"{category}.json")
    path = MANIFEST_DIR / fname
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except json.JSONDecodeError:
        data = {}
    data.setdefault("category", category)
    data.setdefault("items", [])
    if fname == "bgm.json" and db is not None:
        from .local_bgm import manifest_items
        data["items"] = [*data["items"], *manifest_items(db)]
    if db is not None:
        from .packaging_library import manifest_items as personal_items
        data["items"] = [*data["items"], *personal_items(db, category)]
    return data


def load_all_manifests() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cat in ("voice", "bgm", "sfx", "subtitle", "title_card", "sticker", "fancy_text"):
        out[cat] = load_manifest(cat)
    return out


def find_item(category: str, asset_ref: str, db=None) -> dict | None:
    for it in load_manifest(category, db).get("items", []):
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
            INSERT INTO packaging_plans(plan_id, task_id, route, plan_name, status,
                                        schema_version, settings_schema_version, bgm_gain)
            VALUES(?, ?, ?, ?, 'draft', 2, 1, 0.18)
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
    from .packaging_instances import list_instances

    data = dict(_plan_row(connection, plan_id))
    data["items"] = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM packaging_plan_items WHERE plan_id=? "
            "ORDER BY slot,is_backup,rank,item_id",
            (plan_id,),
        ).fetchall()
    ]
    data["instances"] = list_instances(connection, plan_id)
    from .packaging_appearance import appearance_defaults
    task_brief = connection.execute("SELECT brief_json FROM tasks WHERE task_id=?", (data["task_id"],)).fetchone()
    data["subtitle_appearance_defaults"] = appearance_defaults(data, json.loads(task_brief[0] or "{}").get("aspect_ratio", "9:16"), connection)
    data["resource_kind"] = "task_packaging_plan"
    from .packaging_suggestion_jobs import recommendation_metadata
    data["recommendation"] = recommendation_metadata(connection, plan_id)
    return data


def active_items(plan: dict) -> list[dict]:
    return [item for item in plan["items"] if item.get("enabled", 1)]


def active_instances(plan: dict) -> list[dict]:
    enabled = {item["slot"] for item in active_items(plan) if not item["is_backup"]}
    configured = {item["slot"] for item in plan["items"] if not item["is_backup"]}
    return [instance for instance in plan.get("instances", [])
            if instance["role"] in enabled or instance["role"] not in configured]


def _plan_settings(plan: dict) -> dict:
    return {
        "subtitle_highlight": bool(plan.get("subtitle_highlight", 1)),
        "bgm_gain": plan.get("bgm_gain"),
        **({"subtitle_appearance": json.loads(plan["subtitle_appearance_json"])} if plan.get("subtitle_appearance_json", "{}") != "{}" else {}),
    }


_UNSET = object()


def update_plan_settings(
    db, plan_id: str, *, expected_revision: int | None = None,
    subtitle_highlight: bool | object = _UNSET,
    bgm_gain: float | None | object = _UNSET,
    subtitle_appearance: dict | object = _UNSET,
) -> dict:
    """Save the Step 05 style controls as one optimistic draft edit."""
    from .packaging_appearance import validate_appearance
    appearance = json.dumps(validate_appearance(subtitle_appearance, db), sort_keys=True) if subtitle_appearance is not _UNSET else None
    if subtitle_highlight is not _UNSET and not isinstance(subtitle_highlight, bool):
        raise ValueError("PACKAGING_HIGHLIGHT_INVALID: 重点词开关必须是布尔值")
    if bgm_gain is not _UNSET and bgm_gain is not None and (
        not isinstance(bgm_gain, (int, float)) or isinstance(bgm_gain, bool)
        or not math.isfinite(bgm_gain) or not 0 <= bgm_gain <= 0.5
    ):
        raise ValueError("PACKAGING_BGM_GAIN_INVALID: BGM 音量必须在 0 到 50% 之间")
    if expected_revision is not None and (
        not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
        or expected_revision < 0
    ):
        raise ValueError("PACKAGING_REVISION_INVALID: 设置版本无效")
    with db.transaction(immediate=True) as connection:
        row = _require_editable(connection, plan_id)
        if int(row["schema_version"]) < 2:
            raise RuntimeError("PACKAGING_LEGACY_PLAN: 旧版方案不可编辑冻结版设置")
        if expected_revision is not None and row["settings_revision"] != expected_revision:
            raise RuntimeError("PACKAGING_REVISION_CONFLICT: 包装设置已被其他操作修改")
        highlight = int(subtitle_highlight) if subtitle_highlight is not _UNSET else row["subtitle_highlight"]
        gain = (float(bgm_gain) if bgm_gain is not _UNSET and bgm_gain is not None
                else None if bgm_gain is None else row["bgm_gain"])
        appearance = row["subtitle_appearance_json"] if appearance is None else appearance
        if highlight == row["subtitle_highlight"] and gain == row["bgm_gain"] and appearance == row["subtitle_appearance_json"]:
            return _plan_data(connection, plan_id)
        connection.execute(
            "UPDATE packaging_plans SET subtitle_highlight=?,bgm_gain=?,subtitle_appearance_json=?,"
            "settings_revision=settings_revision+1,settings_schema_version=1 WHERE plan_id=?",
            (highlight, gain, appearance, plan_id),
        )
    result = get_plan(db, plan_id)
    assert result is not None
    return result


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
    if not isinstance(snapshot, dict) or canonical_digest(snapshot) != row["snapshot_digest"]:
        raise RuntimeError("PACKAGING_TEMPLATE_INVALID: 模板快照与保存版本不一致，请恢复原模板")
    return {
        "template_id": row["template_id"],
        "template_name": row["template_name"],
        "route": row["route"],
        "snapshot_digest": row["snapshot_digest"],
        "created_at": row["created_at"],
        "status": "template",
        "resource_kind": "personal_packaging_template",
        "items": snapshot.get("items", []),
        "instances": snapshot.get("instances", []),
        "schema_version": snapshot.get("schema", 1),
        "reference_policy": snapshot.get("reference_policy"),
        "settings": snapshot.get("settings", {"subtitle_highlight": True, "bgm_gain": None}),
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
    connection = db.connection
    if connection is None:
        return None
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='packaging_reference_rules'").fetchone():
        return None
    row = connection.execute('SELECT policy_json FROM packaging_reference_rules WHERE plan_id=?', (plan_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _require_composition_revision(plan, expected_revision: int | None) -> None:
    """Protect category edits with the same draft revision as element edits.

    Trusted in-process callers may omit a revision, like update_plan_settings;
    public edits must pass the version seen by the user. The caller holds the
    writer transaction so checking and changing the draft are atomic.
    """
    if expected_revision is None:
        return
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("PACKAGING_REVISION_INVALID: 包装版本无效")
    if plan["instance_revision"] != expected_revision:
        raise RuntimeError("PACKAGING_REVISION_CONFLICT: 包装方案已被其他操作修改，请刷新后再试")


def clone_plan(
    db,
    plan_id: str,
    *,
    task_id: str | None = None,
    plan_name: str | None = None,
    include_instance_map: bool = False,
    connection=None,
) -> str | dict:
    """Clone a recommendation, task plan, or personal template into a new draft."""
    new_plan_id = f"pp-{uuid.uuid4().hex[:12]}"
    from .model_requests import _write_scope
    with _write_scope(db, connection) as connection:
        source_row = connection.execute(
            "SELECT * FROM packaging_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        template = None if source_row is not None else _template_data(connection, plan_id)
        if source_row is None and template is None:
            raise KeyError(f"找不到包装方案或模板：{plan_id}")
        if template is not None:
            if template.get('reference_policy'):
                raise RuntimeError('SUOJIAN_REFERENCE_TEMPLATE_UNSUPPORTED: 参考模板的运镜和花字还未接入新版渲染')
            if task_id is None:
                raise ValueError("PACKAGING_TEMPLATE_TARGET_REQUIRED: 使用模板必须指定目标任务")
            target_task_id = task_id
            source_items = template["items"]
            target_name = plan_name or template["template_name"]
            source_instances = template.get("instances", [])
            schema_version = int(template.get("schema_version", 1))
            settings = template["settings"]
            if schema_version >= 2:
                from .packaging_instances import resolve_template_rules

                source_instances = resolve_template_rules(
                    connection, target_task_id, source_instances,
                )
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
            source_instances = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM packaging_plan_instances WHERE plan_id=? "
                    "ORDER BY created_at,instance_id", (plan_id,),
                )
            ]
            schema_version = source["schema_version"]
            settings = _plan_settings(source)
            if target_task_id != source["task_id"] and source_instances:
                raise ValueError(
                    "PACKAGING_CROSS_TASK_CLONE: 含实例的任务方案请先存为个人模板再跨任务使用"
                )
        target_route = _require_canonical_route(connection, target_task_id, None)
        if schema_version >= 2:
            from .packaging_instances import ROLES

            configured = {item["slot"] for item in source_items
                          if not item.get("is_backup")}
            for instance in source_instances:
                role = instance["role"]
                if role not in ROLES or role in configured:
                    continue
                source_items = [*source_items, {
                    "slot": role, "asset_ref": instance["asset_ref"],
                    "asset_file": None, "asset_version": None, "notes": None,
                    "is_backup": 0, "rank": 0, "enabled": 1,
                }]
                configured.add(role)
        _validate_items(source_items, target_route, require_required=False, db=connection)
        connection.execute(
            "INSERT INTO packaging_plans(plan_id,task_id,route,plan_name,status,origin_type,"
            "schema_version,source_template_id,source_template_digest,"
            "subtitle_highlight,bgm_gain,settings_schema_version) "
            "VALUES(?,?,?,?, 'draft','custom',?,?,?,?,?,1)",
            (new_plan_id, target_task_id, target_route, target_name, schema_version,
             template["template_id"] if template is not None else None,
             template["snapshot_digest"] if template is not None else None,
             int(settings["subtitle_highlight"]), settings["bgm_gain"]),
        )
        connection.execute("UPDATE packaging_plans SET subtitle_appearance_json=? WHERE plan_id=?",
            (json.dumps(settings.get("subtitle_appearance", {}), sort_keys=True), new_plan_id))
        for item in source_items:
            connection.execute(
                """
                INSERT INTO packaging_plan_items(
                  plan_id,slot,asset_ref,asset_file,asset_version,is_backup,rank,notes,enabled
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_plan_id, item["slot"], item["asset_ref"],
                    item.get("asset_file"), item.get("asset_version"),
                    int(bool(item.get("is_backup"))), item.get("rank", 0),
                    item.get("notes"), int(bool(item.get("enabled", 1))),
                ),
            )
        instance_ids = {
            item["instance_id"]: (
                item["instance_id"] if template is not None else f"pi-{uuid.uuid4().hex[:16]}"
            )
            for item in source_instances
        }
        for item in source_instances:
            connection.execute(
                "INSERT INTO packaging_plan_instances("
                "instance_id,plan_id,role,asset_ref,content_text,anchor_kind,anchor_ref,"
                "start_seconds,end_seconds,x,y,scale,gain,source_start_seconds,"
                "fade_in_seconds,fade_out_seconds,timing_rule,origin,revision,"
                "follow_event) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (instance_ids[item["instance_id"]], new_plan_id, item["role"],
                 item["asset_ref"], item.get("content_text"), item.get("anchor_kind", "timeline"),
                 instance_ids.get(item.get("anchor_ref"), item.get("anchor_ref")),
                 item.get("start_seconds"), item.get("end_seconds"), item.get("x"),
                 item.get("y"), item.get("scale"), item.get("gain"),
                 item.get("source_start_seconds"), item.get("fade_in_seconds"),
                 item.get("fade_out_seconds"),
                 item.get("timing_rule", "legacy_v1"),
                 "manual" if template is not None else item.get("origin", "legacy"),
                 item.get("revision", 1), item.get("follow_event", 0)),
            )
            connection.execute("UPDATE packaging_plan_instances SET content_layout_json=? WHERE instance_id=?",
                (item.get("content_layout_json") or "{}", instance_ids[item["instance_id"]]))
        if source_instances:
            connection.execute(
                "UPDATE packaging_plans SET instance_revision=? WHERE plan_id=?",
                (len(source_instances), new_plan_id),
            )
        policy = template.get('reference_policy') if template is not None else reference_policy_for_plan(db, plan_id)
        if policy:
            connection.execute('CREATE TABLE IF NOT EXISTS packaging_reference_rules (plan_id TEXT PRIMARY KEY REFERENCES packaging_plans(plan_id), policy_json TEXT NOT NULL)')
            connection.execute('INSERT INTO packaging_reference_rules(plan_id,policy_json) VALUES(?,?)', (new_plan_id, json.dumps(policy, ensure_ascii=False, sort_keys=True)))
    if include_instance_map:
        return {"plan_id": new_plan_id, "instance_map": instance_ids}
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
    recommendation: dict | None = None,
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
        if not is_backup:
            from .packaging_instances import insert_default_for_slot

            insert_default_for_slot(conn, dict(_plan_row(conn, plan_id)), slot,
                                    asset_ref, recommendation=recommendation)


def replace_item(
    db,
    plan_id: str,
    slot: str,
    asset_ref: str,
    asset_file: str | None = None,
    asset_version: str | None = None,
    notes: str | None = None,
    *, connection=None, expected_revision: int | None = None,
) -> dict:
    """Replace a category at the viewed draft revision, preserving other slots."""
    if slot not in SLOTS:
        raise ValueError(f"slot 必须是 {SLOTS} 之一")
    if not asset_ref:
        raise ValueError("asset_ref 不能为空")
    category = "title_card" if slot in {"brand", "info_card"} else slot
    manifest_item = find_item(category, asset_ref, db)
    if asset_file is None and manifest_item is not None:
        asset_file = manifest_item.get("file")
    from .model_requests import _write_scope
    with _write_scope(db, connection) as connection:
        _require_editable(connection, plan_id)
        plan = dict(_plan_row(connection, plan_id))
        _require_composition_revision(plan, expected_revision)
        routes = (manifest_item or {}).get("default_for") or []
        if routes and plan["route"] not in routes:
            raise RuntimeError("PACKAGING_NOT_APPLICABLE: 所选包装项不适用于当前路线")
        if plan["schema_version"] == 2 and slot in {"title_card", "info_card", "brand", "sfx", "sticker", "fancy_text"}:
            from .packaging_instances import _validate_asset

            _validate_asset(slot, asset_ref, plan["route"], db)
        existing_items = connection.execute(
            "SELECT * FROM packaging_plan_items WHERE plan_id=? AND slot=? ORDER BY item_id",
            (plan_id, slot),
        ).fetchall()
        previous = next((item for item in existing_items if not item["is_backup"]), None)
        unchanged = (len(existing_items) == 1 and previous is not None and previous["enabled"]
                     and previous["rank"] == 0 and all(previous[key] == value for key, value in {
                         "asset_ref": asset_ref, "asset_file": asset_file,
                         "asset_version": asset_version, "notes": notes,
                     }.items()))
        # A category-level choice may deliberately replace distinct instance styles.
        if unchanged and plan["schema_version"] == 2 and slot != "info_card":
            unchanged = connection.execute(
                "SELECT 1 FROM packaging_plan_instances WHERE plan_id=? AND role=? "
                "AND asset_ref!=? LIMIT 1", (plan_id, slot, asset_ref),
            ).fetchone() is None
        if unchanged:
            return _plan_data(connection, plan_id)
        restoring = plan["schema_version"] == 2 and previous is not None and not previous["enabled"]
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
        if restoring:
            if slot == "sfx" and previous["asset_ref"] != asset_ref:
                connection.execute(
                    "UPDATE packaging_plan_instances SET asset_ref=?,revision=revision+1,"
                    "updated_at=datetime('now') WHERE plan_id=? AND role='sfx'",
                    (asset_ref, plan_id),
                )
            connection.execute(
                "UPDATE packaging_plans SET instance_revision=instance_revision+1 "
                "WHERE plan_id=?", (plan_id,),
            )
            if slot == "brand" and connection.execute(
                "SELECT 1 FROM packaging_plan_instances WHERE plan_id=? AND role=? LIMIT 1",
                (plan_id, slot),
            ).fetchone() is None:
                from .packaging_instances import insert_default_for_slot

                insert_default_for_slot(connection, plan, slot, asset_ref)
        elif plan["schema_version"] == 2 and slot != "info_card":
            changed = connection.execute(
                "UPDATE packaging_plan_instances SET asset_ref=?,revision=revision+1,"
                "updated_at=datetime('now') WHERE plan_id=? AND role=?",
                (asset_ref, plan_id, slot),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE packaging_plans SET instance_revision=instance_revision+1 "
                    "WHERE plan_id=?", (plan_id,),
                )
            else:
                from .packaging_instances import insert_default_for_slot

                insert_default_for_slot(connection, plan, slot, asset_ref)
        if plan["schema_version"] == 2 and slot == "sfx":
            from .packaging_instances import _validate_instance

            instances = connection.execute(
                "SELECT * FROM packaging_plan_instances WHERE plan_id=? AND role='sfx'",
                (plan_id,),
            ).fetchall()
            for instance in instances:
                _validate_instance(connection, plan, dict(instance))
        # Category selection is part of the editable composition even for BGM,
        # subtitles and info cards, which may have no generated instances.
        connection.execute(
            "UPDATE packaging_plans SET instance_revision=? WHERE plan_id=?",
            (plan["instance_revision"] + 1, plan_id),
        )
    result = get_plan(db, plan_id)
    assert result is not None
    return result


def disable_optional_slot(db, plan_id: str, slot: str, *, connection=None,
                          expected_revision: int | None = None) -> dict:
    """Close a category while preserving its task-local draft elements."""
    if slot not in SLOTS:
        raise ValueError(f"slot 必须是 {SLOTS} 之一")
    if slot in REQUIRED_SLOTS:
        raise ValueError(f"required slot 不可关闭：{slot}")
    from .model_requests import _write_scope
    with _write_scope(db, connection) as connection:
        plan = _require_editable(connection, plan_id)
        _require_composition_revision(plan, expected_revision)
        dependents = connection.execute(
            "SELECT 1 FROM packaging_plan_instances child JOIN packaging_plan_instances parent "
            "ON child.anchor_ref=parent.instance_id "
            "LEFT JOIN packaging_plan_items child_item ON child_item.plan_id=child.plan_id "
            "AND child_item.slot=child.role AND child_item.is_backup=0 "
            "WHERE parent.plan_id=? AND parent.role=? AND child.anchor_kind='instance' "
            "AND (child_item.item_id IS NULL OR child_item.enabled=1) LIMIT 1",
            (plan_id, slot),
        ).fetchone()
        if dependents is not None:
            raise RuntimeError("PACKAGING_INSTANCE_IN_USE: 先调整跟随此元素的其他包装元素")
        if int(plan["schema_version"]) >= 2:
            changed = connection.execute(
                "UPDATE packaging_plan_items SET enabled=0 "
                "WHERE plan_id=? AND slot=? AND enabled=1", (plan_id, slot),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE packaging_plans SET instance_revision=instance_revision+1 "
                    "WHERE plan_id=?", (plan_id,),
                )
        else:
            changed = connection.execute(
                "DELETE FROM packaging_plan_items WHERE plan_id=? AND slot=?",
                (plan_id, slot),
            ).rowcount
            changed += connection.execute(
                "DELETE FROM packaging_plan_instances WHERE plan_id=? AND role=?",
                (plan_id, slot),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE packaging_plans SET instance_revision=instance_revision+1 WHERE plan_id=?",
                    (plan_id,),
                )
    result = get_plan(db, plan_id)
    assert result is not None
    return result


def save_personal_template(db, plan_id: str, template_name: str, reference_policy=None) -> dict:
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
        _validate_items(source["items"], route, require_required=True, db=connection)
        schema = int(source.get("schema_version", 1))
        snapshot = {
            "schema": schema,
            "route": route,
            "items": _canonical_items(source["items"], include_enabled=schema >= 2),
        }
        if schema >= 2:
            from .packaging_instances import _validate_instance, template_instance_rules

            for instance in active_instances(source):
                _validate_instance(connection, source, instance, require_text=False)
            snapshot["items"] = [
                {**item, "asset_file": None, "notes": None}
                for item in snapshot["items"]
            ]
            snapshot["instances"] = template_instance_rules(connection, source)
            snapshot["settings"] = _plan_settings(source)
        policy = reference_policy or reference_policy_for_plan(db, plan_id)
        if policy:
            from .reference_editing import freeze_reference_policy
            snapshot['reference_policy'] = freeze_reference_policy(policy)
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


def _canonical_items(items: list[dict], *, include_enabled: bool = False) -> list[dict]:
    payload_items = [
        {
            key: item.get(key)
            for key in (
                "slot", "asset_ref", "asset_file", "asset_version", "is_backup", "rank", "notes",
            )
        } | ({"enabled": bool(item.get("enabled", 1))} if include_enabled else {})
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
    instance_mode = int(plan.get("schema_version", 1)) >= 2
    payload = {
        "plan_id": plan["plan_id"],
        "route": plan["route"],
        "plan_name": plan["plan_name"],
        "items": _canonical_items(
            [item for item in source_items if item.get("enabled", 1)]
            if instance_mode else source_items,
            include_enabled=instance_mode,
        ),
    }
    if instance_mode:
        payload["schema_version"] = 2
        payload["disabled_slots"] = sorted({item["slot"] for item in source_items
                                            if not item.get("enabled", 1)})
        if int(plan.get("settings_schema_version", 0)) >= 1:
            payload["settings"] = _plan_settings(plan)
        payload["instances"] = [
            {**{key: instance.get(key) for key in (
                "instance_id", "role", "asset_ref", "content_text", "anchor_kind",
                "anchor_ref", "start_seconds", "end_seconds", "x", "y", "scale",
                "gain", "timing_rule",
            )}, **{key: instance[key] for key in (
                "source_start_seconds", "fade_in_seconds", "fade_out_seconds",
            ) if instance.get(key) is not None},
             **({"content_layout_json": instance["content_layout_json"]} if json.loads(instance.get("content_layout_json") or "{}") else {})}
            for instance in sorted(active_instances(plan),
                                   key=lambda value: value["instance_id"])
        ]
    return payload


def _packaging_path(manifest_item: dict, locked_file: str | None) -> Path | None:
    if manifest_item.get("id", "").startswith(("local-bgm-", "personal-pack-")) and manifest_item.get("_local_root"):
        expected = manifest_item["file"]
        if locked_file and locked_file != expected:
            raise RuntimeError("PACKAGING_REFERENCE_MISMATCH: 本地音乐引用不一致")
        path = Path(manifest_item["_local_path"]).resolve()
        if not path.is_relative_to(Path(manifest_item["_local_root"]).resolve()):
            raise RuntimeError("PACKAGING_PATH_INVALID: 音乐路径越界")
        if not manifest_item.get("local_available"):
            raise RuntimeError("PACKAGING_FILE_MISSING: 本地音乐已停用、改变或离线，请重新扫描")
        return path
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
        font_root = (project_root / "assets" / "fonts").resolve()
        resolved = (font_root / font_ref).resolve()
        if not resolved.is_relative_to(font_root):
            raise RuntimeError("PACKAGING_PATH_INVALID: 字体路径越界")
        return resolved
    return None


def resolve_manifest_asset_path(category: str, asset_ref: str, db=None) -> Path | None:
    """Resolve one registered core asset without allowing paths outside the project."""
    item = find_item(category, asset_ref, db)
    if item is None:
        return None
    return _packaging_path(item, None)


def audio_duration_seconds(path: Path) -> float | None:
    """Reuse probes only for identical bytes; timestamps alone are insufficient."""
    try:
        if path.stat().st_size <= 32:
            return None
        from .packaging_audio_evidence import file_sha256
        content_sha = file_sha256(path)
    except OSError:
        return None
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        return _audio_duration_for_content(str(path.resolve()), content_sha, ffprobe)
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return None


@lru_cache(maxsize=256)
def _audio_duration_for_content(path: str, content_sha: str, ffprobe: str) -> float:
    # Exceptions are not cached: a later explicit request can probe again.
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
    if result.returncode != 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Audio probe did not return a positive duration")
    return duration


def _audio_file_readable(path: Path) -> bool:
    """Return whether ffprobe can decode a positive-duration local audio asset."""
    measured = audio_duration_seconds(path)
    if measured is not None:
        return True
    if shutil.which("ffprobe"):
        return False
    try:
        return path.stat().st_size > 1024
    except OSError:
        return False


def _validate_items(
    items: list[dict], route: str, *, require_required: bool, db=None
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
        if not item["is_backup"] and item.get("enabled", 1):
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
        if not item.get("enabled", 1):
            continue
        slot = item["slot"]
        category = "title_card" if slot in {"brand", "info_card"} else slot
        manifest_item = find_item(category, item["asset_ref"], db)
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
        if file_path is not None and manifest_item.get("_local_root"):
            from .packaging_audio_evidence import file_sha256
            if file_sha256(file_path) != manifest_item.get("source_sha256"):
                raise RuntimeError("PACKAGING_SOURCE_CHANGED: 本地音乐内容已变化，请重新扫描并确认使用权")
        if file_path is not None and not file_path.is_file():
            raise RuntimeError(f"PACKAGING_FILE_MISSING: 包装文件不存在：{file_path}")
        if file_path is not None:
            try:
                if file_path.stat().st_size == 0:
                    raise RuntimeError(f"PACKAGING_FILE_UNREADABLE: 包装文件为空：{file_path}")
            except OSError as error:
                raise RuntimeError(f"PACKAGING_FILE_UNREADABLE: 包装文件不可读取：{file_path}") from error
        if slot in ("bgm", "sfx") and file_path is not None and not _audio_file_readable(file_path):
            raise RuntimeError(f"PACKAGING_FILE_UNREADABLE: 包装音频不可读取：{file_path}")


def _validate_confirmation(connection, plan: dict) -> None:
    _validate_items(plan["items"], plan["route"], require_required=True, db=connection)
    if int(plan.get("schema_version", 1)) >= 2:
        from .packaging_instances import _validate_instance

        _validate_pending_instances(plan)
        for instance in active_instances(plan):
            _validate_instance(connection, plan, instance)


def _validate_pending_instances(plan: dict) -> None:
    selected = {item["slot"] for item in active_items(plan) if not item["is_backup"]}
    placed = {instance["role"] for instance in active_instances(plan)}
    pending = sorted((selected & {"title_card", "info_card", "fancy_text",
                                  "sfx", "sticker"}) - placed)
    if pending:
        raise ValueError("PACKAGING_ELEMENT_PENDING: 包装元素缺少可验证内容和定位："
                         + ", ".join(pending))


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
    if not selection_matches_picture_lock(db, task_id, lock["selection_digest"]):
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
        _validate_items(plan["items"], route, require_required=True, db=connection)
        if int(plan.get("schema_version", 1)) >= 2:
            from .packaging_instances import _validate_instance

            _validate_pending_instances(plan)
            for instance in active_instances(plan):
                _validate_instance(connection, plan, instance)
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
                   COUNT(CASE WHEN i.enabled=1 THEN i.item_id END) AS item_count
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
        _validate_confirmation(conn, plan)
        gate_payload = canonical_packaging_gate_payload(plan)
        gate_digest = canonical_digest(gate_payload)
        previous_gate = conn.execute(
            "SELECT gate_digest,details_json,rowid AS gate_order FROM task_gate_events "
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
                    picture_event = conn.execute(
                        "SELECT rowid AS gate_order FROM task_gate_events "
                        "WHERE task_id=? AND gate_type='picture_lock' ORDER BY rowid DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    current_picture = picture_event is None or previous_gate["gate_order"] > picture_event["gate_order"]
                    same_evidence = evidence is None or previous_details.get("confirmation_evidence") == evidence
                    if current_picture and same_evidence:
                        return
                    # Existing choices stay immutable; explicit current-preview confirmation renews the gate.
                else:
                    raise RuntimeError("PACKAGING_CONFIRM_CONFLICT: 已确认方案与当前包装门禁不一致")
            else:
                raise RuntimeError("PACKAGING_CONFIRM_CONFLICT: 已确认方案缺少包装门禁")
        if plan["status"] not in {"draft", "confirmed"}:
            raise RuntimeError(
                f"PACKAGING_CONFIRM_CONFLICT: 方案状态不可确认：{plan['status']}"
            )
        if plan["status"] == "draft":
            changed = conn.execute(
                "UPDATE packaging_plans "
                "SET status='confirmed',confirmed_at=datetime('now') "
                "WHERE plan_id=? AND status='draft'",
                (plan_id,),
            ).rowcount
            if changed != 1:
                raise RuntimeError("PACKAGING_CONFIRM_CONFLICT: 方案已被其他操作修改")
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
        if item.get("_local_root"):
            return bool(item.get("local_available") and item.get("duration_s"))
        return _audio_file_readable(path)
    return True


def _eligible_items(manifests: dict, slot: str, route: str) -> list[dict]:
    category = "title_card" if slot in ("title_card", "info_card", "brand") else slot
    return [
        item
        for item in manifests.get(category, {}).get("items", [])
        if _item_is_recommendable(item, route, category)
        and (slot != "brand" or item.get("kind") == "watermark")
        and (slot not in {"title_card", "info_card"} or item.get("kind") != "watermark")
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


def propose(db, task_id: str, route: str | None, *,
            project_root: Path | None = None) -> list[str]:
    """为任务推 2–3 套包装方案（全部 draft 落库），返回 plan_id 列表。

    每套方案结构：
      - 主 BGM（route 默认）→ 备选 BGM1 → 备选 BGM2（若素材够）
      - 配音沿用任务声音，不作为包装元素
      - 简洁商务只用字幕和 BGM；重点方案再添加标题卡与音效；品牌水印仅由用户主动添加
      - 第三套推荐保留贴纸与花字供历史方案兼容
    """
    route = _require_canonical_route(db.connection, task_id, route)
    semantic = None
    if project_root is not None and route == "数字人混剪":
        from .packaging_semantics import (
            load_bound_digital_evidence, suggest_digital_instances,
        )
        evidence = load_bound_digital_evidence(db, project_root, task_id)
        scenes = [dict(row) for row in db.connection.execute(
            "SELECT card_id,narrative_text,narrative_start,narrative_end "
            "FROM scene_cards WHERE task_id=? ORDER BY idx,card_id", (task_id,),
        )]
        semantic = (suggest_digital_instances(*evidence, scenes)
                    if evidence is not None else {})
    elif project_root is not None and route == "信息流广告":
        from .packaging_semantics import load_bound_ad_evidence, suggest_ad_instances

        evidence = load_bound_ad_evidence(db, project_root, task_id)
        if evidence is None:
            semantic = {}
        else:
            # Evidence already binds this exact UTF-8 script to the current brief.
            # Rework keeps prior clocks as history, including identical old copy.
            script_sha = hashlib.sha256(evidence[0].encode("utf-8")).hexdigest()
            segments = [dict(row) for row in db.connection.execute(
                "SELECT idx,segment_id,text FROM script_segments "
                "WHERE task_id=? AND script_revision=? ORDER BY idx",
                (task_id, script_sha),
            )]
            scenes = [dict(row) for row in db.connection.execute(
                "SELECT card_id,script_segment_id,narrative_text,narrative_start,narrative_end "
                "FROM scene_cards WHERE task_id=? ORDER BY idx,card_id", (task_id,),
            )]
            semantic = suggest_ad_instances(*evidence, segments, scenes)
    manifests = load_all_manifests()
    subtitle = _first_available(manifests, "subtitle", route)
    title_card = _first_available(manifests, "title_card", route)
    sticker = _first_available(manifests, "sticker", route)
    fancy = _first_available(manifests, "fancy_text", route)
    eligible_sfx = _eligible_items(manifests, "sfx", route)
    sfx = eligible_sfx[0] if eligible_sfx else None
    model_sound = semantic.get("sfx") if semantic is not None else None
    selected_sfx = next(
        (item for item in eligible_sfx
         if model_sound and item["id"] == model_sound.get("asset_ref")), None,
    )
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
        # Product requests use P09 for music; a style template is not a content recommendation.
        bgm = _pick_bgm(manifests, route, used) if project_root is None else None
        if bgm is not None:
            used.add(bgm["id"])

        plan_id = create_plan(db, task_id, route, name)
        # BGM
        if bgm is not None:
            add_item(
                db, plan_id, "bgm", bgm["id"], bgm.get("file"),
                asset_version=bgm.get("mood"),
                notes=bgm.get("note"),
            )
        # 字幕模板
        if subtitle is not None:
            add_item(db, plan_id, "subtitle", subtitle["id"], subtitle.get("file"))
        # 文字卡（与 brand 同目录，区别 slot）
        if idx > 0 and title_card is not None:
            title_suggestion = semantic.get("title_card", {}) if semantic is not None else None
            add_item(db, plan_id, "title_card", title_card["id"],
                     title_card.get("file"),
                     notes=title_suggestion.get("reason") if title_suggestion else None,
                     recommendation=title_suggestion)
        # Model selects whether and which sound follows the verified title event.
        # Without such evidence the category stays a visible, unset style choice.
        if idx > 0 and sfx is not None:
            sound = selected_sfx or sfx
            sound_recommendation = None
            if selected_sfx is not None and model_sound.get("follow_role") == "title_card":
                title_instance = db.connection.execute(
                    "SELECT instance_id,start_seconds,end_seconds FROM packaging_plan_instances "
                    "WHERE plan_id=? AND role='title_card' ORDER BY start_seconds LIMIT 1",
                    (plan_id,),
                ).fetchone()
                source_path = _packaging_path(sound, None)
                source_duration = (audio_duration_seconds(source_path)
                                   if source_path is not None else None)
                if title_instance is not None and source_duration is not None:
                    start = float(title_instance["start_seconds"])
                    end = round(min(float(title_instance["end_seconds"]),
                                    start + source_duration), 3)
                    if end > start:
                        sound_recommendation = {
                            "anchor_kind": "instance",
                            "anchor_ref": title_instance["instance_id"],
                            "start_seconds": start, "end_seconds": end,
                            "gain": 0.35,
                        }
            add_item(db, plan_id, "sfx", sound["id"], sound.get("file"),
                     notes=(model_sound.get("reason") if sound_recommendation
                            else "音效待选择本片事件"),
                     recommendation=sound_recommendation)
        # 贴纸 / 花字（方案 B/C 纳入，素材存在才加）
        if allow_sticker:
            if sticker is not None:
                add_item(db, plan_id, "sticker", sticker["id"], sticker.get("file"),
                         notes="贴纸库（PIL 生成透明 PNG）")
            if fancy is not None:
                fancy_suggestion = semantic.get("fancy_text", {}) if semantic is not None else None
                add_item(db, plan_id, "fancy_text", fancy["id"], fancy.get("file"),
                         notes=(fancy_suggestion.get("reason") if fancy_suggestion
                                else "花字预设"),
                         recommendation=fancy_suggestion)
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
