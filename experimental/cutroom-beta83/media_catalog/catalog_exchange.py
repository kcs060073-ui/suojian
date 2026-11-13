"""Excel exchange for catalog annotations; SQLite remains the sole ledger."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import BytesIO
import hashlib
import json
import math
import uuid
from zipfile import BadZipFile, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.packaging.custom import StringProperty
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .asset_index import get_asset_detail, list_asset_index
from .segment_marks import CAMERA_MOTIONS, MOTION_KINDS, _duration_us


EXCHANGE_VERSION = "1.0.0"
ASSET_COLUMNS = (
    "asset_id", "asset_version", "export_revision", "文件名", "相对路径", "素材库ID",
    "媒体类型", "方向", "源时长秒", "显示宽高", "内容描述", "标签", "备注",
    "使用状态", "源内字幕", "源内水印", "源内音乐",
)
SEGMENT_COLUMNS = (
    "segment_id", "asset_id", "asset_version", "export_revision", "源开始秒",
    "源结束秒", "描述", "标签", "备注", "镜头方向", "运动方式", "主体运动",
    "使用状态", "质量问题", "可用理由", "重复关系说明",
)
ASSET_EDITABLE = ASSET_COLUMNS[10:]
SEGMENT_EDITABLE = SEGMENT_COLUMNS[4:]
ASSET_FIELDS = {
    "内容描述": "description", "标签": "tags", "备注": "note", "使用状态": "usage_status",
    "源内字幕": "embedded_subtitle", "源内水印": "embedded_watermark",
    "源内音乐": "embedded_music",
}
SEGMENT_FIELDS = {
    "描述": "description", "标签": "tags", "备注": "note", "镜头方向": "camera_motion",
    "运动方式": "motion_kind", "主体运动": "subject_motion", "使用状态": "usage_status",
    "质量问题": "quality_issue", "可用理由": "usable_reason", "重复关系说明": "duplicate_note",
}
ASSET_USAGE = {"", "可用", "禁用", "待核对"}
SEGMENT_USAGE = {"可用", "禁用", "待核对"}
EMBEDDED_STATUS = {"", "已观察", "疑似", "未知", "未见"}
_TO_EXCHANGE_USAGE = {"可用": "可用", "待核验": "待核对", "不建议使用": "禁用"}
_FROM_EXCHANGE_USAGE = {value: key for key, value in _TO_EXCHANGE_USAGE.items()}
MAX_WORKBOOK_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
MAX_ROWS = 100_000
_SCHEMA_HASH = hashlib.sha256(json.dumps(
    [ASSET_COLUMNS, SEGMENT_COLUMNS], ensure_ascii=False, separators=(",", ":")
).encode("utf-8")).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _asset_annotations(db, asset_id: str, fingerprint: str) -> tuple[dict, int]:
    row = db.connection.execute(
        "SELECT fields_json,revision,asset_fingerprint FROM catalog_asset_annotations WHERE asset_id=?",
        (asset_id,),
    ).fetchone()
    if row is None or row["asset_fingerprint"] != fingerprint:
        return {}, 0
    try:
        fields = json.loads(row["fields_json"])
    except (TypeError, json.JSONDecodeError):
        fields = {}
    return fields if isinstance(fields, dict) else {}, row["revision"]


def _probe(db, asset_id: str) -> dict:
    row = db.connection.execute(
        "SELECT result_json FROM analysis_runs WHERE asset_id=? AND stage='technical_probe' "
        "AND status='completed'", (asset_id,),
    ).fetchone()
    if row is None:
        return {}
    try:
        result = json.loads(row["result_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _display_dimensions(kind: str, probe: dict) -> tuple[str, str]:
    if kind == "image":
        width, height = probe.get("width"), probe.get("height")
        if probe.get("orientation") in {5, 6, 7, 8}:
            width, height = height, width
    else:
        stream = next((item for item in probe.get("streams", [])
                       if isinstance(item, dict) and item.get("codec_type") == "video"), {})
        width, height = stream.get("width"), stream.get("height")
        angles = [item.get("rotation") for item in stream.get("side_data_list", [])
                  if isinstance(item, dict) and "rotation" in item]
        if "rotate" in (stream.get("tags") or {}):
            angles.append(stream["tags"]["rotate"])
        try:
            if angles and all(int(float(angle)) % 180 == 90 for angle in angles):
                width, height = height, width
        except (TypeError, ValueError):
            pass
    if not all(isinstance(value, int) and value > 0 for value in (width, height)):
        return "未知", ""
    direction = "横屏" if width > height else "竖屏" if height > width else "方形"
    return direction, f"{width}×{height}"


def _asset_row(db, item: dict) -> dict:
    asset_id = item["assetId"]
    fingerprint = db.connection.execute(
        "SELECT quick_fingerprint FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()[0]
    fields, revision = _asset_annotations(db, asset_id, fingerprint)
    direction, dimensions = _display_dimensions(item["mediaKind"], _probe(db, asset_id))
    kind_label = {"video": "视频", "image": "图片", "audio": "音频", "other": "其他"}[item["mediaKind"]]
    description = fields.get("description", "")
    if "description" not in fields and item["mediaKind"] in {"video", "image"}:
        descriptions = [segment.get("description") or segment.get("title")
                        for segment in get_asset_detail(db, asset_id)["segments"]]
        description = "；".join(dict.fromkeys(text for text in descriptions if text))[:4000]
    return {
        "asset_id": asset_id, "asset_version": fingerprint, "export_revision": f"r{revision}",
        "文件名": item["relativePath"].replace("\\", "/").split("/")[-1],
        "相对路径": item["relativePath"], "素材库ID": item["libraryId"],
        "媒体类型": kind_label, "方向": direction,
        "源时长秒": item["durationSeconds"] if item["mediaKind"] == "video" else None,
        "显示宽高": dimensions,
        "内容描述": description,
        "标签": fields.get("tags", ""), "备注": fields.get("note", ""),
        "使用状态": fields.get("usage_status", ""),
        "源内字幕": fields.get("embedded_subtitle", ""),
        "源内水印": fields.get("embedded_watermark", ""),
        "源内音乐": fields.get("embedded_music", ""),
    }


def _segment_row(segment: dict, asset_id: str, fingerprint: str, kind: str) -> dict:
    return {
        "segment_id": segment["segmentId"], "asset_id": asset_id,
        "asset_version": fingerprint, "export_revision": f"r{segment['revision']}",
        "源开始秒": segment["sourceStartUs"] / 1_000_000 if kind == "video" else None,
        "源结束秒": segment["sourceEndUs"] / 1_000_000 if kind == "video" else None,
        "描述": segment.get("description") or segment.get("title") or "",
        "标签": segment.get("tags", ""), "备注": segment.get("note", ""),
        "镜头方向": segment.get("cameraMotion") or ("不适用" if kind == "image" else "未知"),
        "运动方式": segment.get("motionKind") or "",
        "主体运动": segment.get("subjectMotion") or "",
        "使用状态": _TO_EXCHANGE_USAGE.get(segment.get("usageStatus"), "待核对"),
        "质量问题": segment.get("qualityIssue", ""),
        "可用理由": segment.get("usableReason", ""),
        "重复关系说明": segment.get("duplicateNote", ""),
    }


def _write_sheet(workbook: Workbook, name: str, columns: tuple[str, ...], rows: list[dict],
                 metadata: str, first_editable: int) -> None:
    sheet = workbook.create_sheet(name)
    sheet.sheet_view.showGridLines = False
    sheet["A2"] = "剪辑工作台 素材交换台账"
    sheet["A2"].font = Font(name="Arial", size=14, bold=True, color="27342B")
    sheet["A3"] = "浅黄色可编辑；灰色身份与源信息不可修改。删除行不会删除素材或片段。"
    sheet["A4"] = metadata
    sheet["A4"].font = Font(name="Arial", size=9, color="68746B")
    for index, name_ in enumerate(columns, 1):
        cell = sheet.cell(6, index, name_)
        cell.fill = PatternFill("solid", fgColor="324538")
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row_index, record in enumerate(rows, 7):
        for col_index, name_ in enumerate(columns, 1):
            value = record.get(name_)
            cell = sheet.cell(row_index, col_index, value)
            if isinstance(value, str):
                cell.data_type = "s"  # =, +, -, @ remain literal text, never formulas.
            cell.fill = PatternFill("solid", fgColor="FFF2CF" if col_index >= first_editable else "EEF0ED")
            cell.font = Font(name="Arial", size=10, color="27342B")
            cell.alignment = Alignment(vertical="center")
    sheet.freeze_panes = "A7"
    sheet.auto_filter.ref = f"A6:{get_column_letter(len(columns))}{max(7, len(rows) + 6)}"
    sheet.row_dimensions[6].height = 27
    for index, name_ in enumerate(columns, 1):
        width = 18 if index <= first_editable else 22
        if name_ in {"相对路径", "内容描述", "描述", "重复关系说明"}:
            width = 35
        sheet.column_dimensions[get_column_letter(index)].width = width
    if name == "资产":
        sheet.column_dimensions["I"].width = 14
    else:
        for col in ("E", "F"):
            for cell in sheet[col][6:]:
                cell.number_format = "0.000000"


def export_catalog_workbook(db, *, task_id: str | None = None,
                            filters: dict | None = None) -> bytes:
    """Export every matching asset, including pages after the current UI page."""
    filters = filters or {}
    assets = []
    segments = []
    offset = 0
    while True:
        page = list_asset_index(db, task_id=task_id, limit=200, offset=offset, **filters)
        for item in page["items"]:
            asset_row = _asset_row(db, item)
            assets.append(asset_row)
            if item["mediaKind"] in {"video", "image"}:
                detail = get_asset_detail(db, item["assetId"], task_id=task_id)
                segments.extend(_segment_row(segment, item["assetId"], asset_row["asset_version"],
                                             item["mediaKind"]) for segment in detail["segments"])
        offset += len(page["items"])
        if offset >= page["total"]:
            break
    export_id = uuid.uuid4().hex
    catalog_id = db.connection.execute("SELECT catalog_id FROM catalog_identity WHERE id=1").fetchone()[0]
    metadata = (f"exchange_version={EXCHANGE_VERSION}; export_id={export_id}; "
                f"catalog_id={catalog_id}; revision={export_id}; schema_hash={_SCHEMA_HASH}")
    workbook = Workbook()
    workbook.remove(workbook.active)
    _write_sheet(workbook, "资产", ASSET_COLUMNS, assets, metadata, 11)
    _write_sheet(workbook, "可用片段", SEGMENT_COLUMNS, segments, metadata, 5)
    for name, value in (("exchange_version", EXCHANGE_VERSION), ("export_id", export_id),
                        ("catalog_id", catalog_id), ("revision", export_id),
                        ("schema_hash", _SCHEMA_HASH)):
        workbook.custom_doc_props.append(StringProperty(name=name, value=value))
    out = BytesIO()
    workbook.save(out)
    snapshot = {"资产": {row["asset_id"]: row for row in assets},
                "可用片段": {row["segment_id"]: row for row in segments}}
    with db.transaction() as conn:
        conn.execute("INSERT INTO catalog_exchange_exports(export_id,catalog_id,exchange_version,"
                     "schema_hash,snapshot_json) VALUES(?,?,?,?,?)",
                     (export_id, catalog_id, EXCHANGE_VERSION, _SCHEMA_HASH, _json(snapshot)))
    return out.getvalue()


def _issue(sheet: str, cell: str, message: str) -> dict:
    return {"sheet": sheet, "cell": cell, "message": message}


def _safe_workbook(data: bytes):
    if not isinstance(data, bytes) or not data or len(data) > MAX_WORKBOOK_BYTES:
        raise ValueError("WORKBOOK_SIZE_INVALID: Excel 文件为空或超过 20 MB")
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("WORKBOOK_SIZE_INVALID: Excel 解压内容超过限制")
            forbidden = ("vbaproject", "externallinks/", "connections.xml", "querytables/",
                         "embeddings/", "activex/", "oleobjects/")
            if any(any(pattern in entry.filename.lower() for pattern in forbidden) for entry in entries):
                raise ValueError("WORKBOOK_ACTIVE_CONTENT: 不接受宏、外部连接或嵌入对象")
    except BadZipFile as error:
        raise ValueError("WORKBOOK_INVALID: 文件不是有效的 XLSX") from error
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
    except Exception as error:
        raise ValueError("WORKBOOK_INVALID: Excel 文件无法解析") from error
    return workbook


def _parse_sheet(workbook, sheet_name: str, columns: tuple[str, ...], errors: list[dict]) -> list[dict]:
    sheet = workbook[sheet_name]
    if sheet.max_row > MAX_ROWS + 6:
        errors.append(_issue(sheet_name, "A7", "数据行数超过 100000"))
        return []
    headers = [sheet.cell(6, index).value for index in range(1, len(columns) + 1)]
    if headers != list(columns):
        errors.append(_issue(sheet_name, "A6", "表头或列顺序与导出模式不一致"))
        return []
    rows = []
    for row_number, raw in enumerate(sheet.iter_rows(min_row=7, max_col=len(columns)), 7):
        if all(cell.value is None for cell in raw):
            continue
        row = {"_row": row_number}
        for index, (name, cell) in enumerate(zip(columns, raw), 1):
            if cell.data_type == "f":
                errors.append(_issue(sheet_name, f"{get_column_letter(index)}{row_number}",
                                     "公式不能作为台账数据导入"))
            row[name] = cell.value
        rows.append(row)
    return rows


def _parse_workbook(db, data: bytes) -> tuple[str, dict, list[dict], list[dict], list[dict]]:
    workbook = _safe_workbook(data)
    errors = []
    try:
        if workbook.sheetnames != ["资产", "可用片段"]:
            raise ValueError("WORKBOOK_SCHEMA_INVALID: 工作簿必须只有资产、可用片段两张数据表")
        props = {prop.name: prop.value for prop in workbook.custom_doc_props}
        export_id = props.get("export_id")
        if not isinstance(export_id, str) or not export_id:
            raise ValueError("EXPORT_ID_MISSING: 缺少导出身份")
        export_row = db.connection.execute(
            "SELECT * FROM catalog_exchange_exports WHERE export_id=?", (export_id,)
        ).fetchone()
        if export_row is None:
            raise ValueError("EXPORT_NOT_FOUND: 导出记录不存在，不能只相信表内版本")
        catalog_id = db.connection.execute("SELECT catalog_id FROM catalog_identity WHERE id=1").fetchone()[0]
        expected = {"exchange_version": EXCHANGE_VERSION, "export_id": export_id,
                    "catalog_id": catalog_id, "revision": export_id,
                    "schema_hash": _SCHEMA_HASH}
        if any(props.get(name) != value for name, value in expected.items()) or any(
            export_row[column] != expected[column] for column in
            ("catalog_id", "exchange_version", "schema_hash")
        ):
            raise ValueError("WORKBOOK_METADATA_INVALID: 交换版本、库身份或列模式不匹配")
        for sheet_name in ("资产", "可用片段"):
            line = workbook[sheet_name]["A4"].value
            if not isinstance(line, str) or any(
                f"{key}={value}" not in line for key, value in expected.items()
            ):
                raise ValueError("WORKBOOK_METADATA_INVALID: 两表说明元信息不一致")
        assets = _parse_sheet(workbook, "资产", ASSET_COLUMNS, errors)
        segments = _parse_sheet(workbook, "可用片段", SEGMENT_COLUMNS, errors)
        return export_id, json.loads(export_row["snapshot_json"]), assets, segments, errors
    finally:
        workbook.close()


def _text(value, sheet: str, cell: str, errors: list[dict], *, max_length: int = 4000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_length:
        errors.append(_issue(sheet, cell, "需要不超过 4000 字的文本"))
        return ""
    return value


def _time_us(value, sheet: str, cell: str, errors: list[dict], *, required: bool) -> int | None:
    if value is None or value == "":
        if required:
            errors.append(_issue(sheet, cell, "视频片段需要真实数值时间码"))
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        errors.append(_issue(sheet, cell, "时间码必须是真实数值，不能使用文本或公式"))
        return None
    try:
        microseconds = Decimal(str(value)) * 1_000_000
        if microseconds != microseconds.to_integral_value() or microseconds < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        errors.append(_issue(sheet, cell, "时间码须为非负数且最多六位小数"))
        return None
    return int(microseconds)


def _normal_editable(row: dict, columns: tuple[str, ...], sheet: str,
                     errors: list[dict]) -> dict:
    values = {}
    for column in columns:
        index = (ASSET_COLUMNS if sheet == "资产" else SEGMENT_COLUMNS).index(column) + 1
        cell = f"{get_column_letter(index)}{row['_row']}"
        value = row.get(column)
        if column in {"源开始秒", "源结束秒"}:
            values[column] = _time_us(value, sheet, cell, errors, required=False)
        else:
            values[column] = _text(value, sheet, cell, errors)
    if sheet == "资产":
        if values["使用状态"] not in ASSET_USAGE:
            errors.append(_issue(sheet, f"N{row['_row']}", "使用状态只能为可用、禁用、待核对或空白"))
        for column in ("源内字幕", "源内水印", "源内音乐"):
            if values[column] not in EMBEDDED_STATUS:
                errors.append(_issue(sheet, f"{get_column_letter(ASSET_COLUMNS.index(column)+1)}{row['_row']}",
                                     "核对状态只能为已观察、疑似、未知、未见或空白"))
    else:
        if values["使用状态"] not in SEGMENT_USAGE:
            errors.append(_issue(sheet, f"M{row['_row']}", "片段使用状态只能为可用、禁用、待核对"))
        if values["镜头方向"] not in CAMERA_MOTIONS | {"不适用"}:
            errors.append(_issue(sheet, f"J{row['_row']}", "镜头方向无效"))
        if values["运动方式"] not in MOTION_KINDS | {""}:
            errors.append(_issue(sheet, f"K{row['_row']}", "运动方式无效"))
    return values


def _check_readonly(row: dict, baseline: dict, columns: tuple[str, ...],
                    sheet: str, errors: list[dict]) -> None:
    for index, column in enumerate(columns, 1):
        imported, exported = row.get(column), baseline.get(column)
        if imported == exported or (imported in (None, "") and exported in (None, "")):
            continue
        if imported != exported:
            errors.append(_issue(sheet, f"{get_column_letter(index)}{row['_row']}",
                                 "只读身份或源信息与服务端导出记录不一致"))


def _current_asset_row(db, asset_id: str) -> dict | None:
    page = list_asset_index(db, asset_id=asset_id, limit=1)
    return _asset_row(db, page["items"][0]) if page["items"] else None


def _current_segment_row(db, asset_id: str, segment_id: str, fingerprint: str) -> dict | None:
    try:
        detail = get_asset_detail(db, asset_id)
    except KeyError:
        return None
    segment = next((item for item in detail["segments"] if item["segmentId"] == segment_id), None)
    return _segment_row(segment, asset_id, fingerprint, detail["mediaKind"]) if segment else None


def _field_diff(sheet: str, row_number: int, columns: tuple[str, ...], imported: dict,
                baseline: dict, current: dict, conflicts: list[dict]) -> dict:
    changed = {}
    for column in columns:
        wanted = imported[column]
        old = baseline[column]
        now = current[column]
        if wanted == old:
            continue
        if now != old and now != wanted:
            conflicts.append(_issue(sheet,
                                    f"{get_column_letter((ASSET_COLUMNS if sheet == '资产' else SEGMENT_COLUMNS).index(column)+1)}{row_number}",
                                    "导出后同一字段已由其他人修改"))
        elif now != wanted:
            changed[column] = {"old": now, "new": wanted}
    return changed


def _validate_segment_range(db, row: dict, values: dict, asset_baseline: dict,
                            errors: list[dict]) -> None:
    row_number = row["_row"]
    start, end = values["源开始秒"], values["源结束秒"]
    if asset_baseline["媒体类型"] == "图片":
        if start is not None or end is not None:
            errors.append(_issue("可用片段", f"E{row_number}", "图片不使用源视频时间码"))
        if values["镜头方向"] != "不适用" or values["运动方式"]:
            errors.append(_issue("可用片段", f"J{row_number}", "图片没有源镜头运动"))
        return
    if asset_baseline["媒体类型"] != "视频":
        errors.append(_issue("可用片段", f"B{row_number}", "仅视频或图片可标注画面片段"))
        return
    duration = _duration_us(db, row["asset_id"])
    if start is None:
        errors.append(_issue("可用片段", f"E{row_number}", "视频片段需要起点秒数"))
    if end is None:
        errors.append(_issue("可用片段", f"F{row_number}", "视频片段需要终点秒数"))
    if start is not None and end is not None and (end <= start or duration is None or end > duration):
        errors.append(_issue("可用片段", f"F{row_number}", "终点须大于起点且不超过当前原片时长"))
    if values["镜头方向"] == "不适用":
        errors.append(_issue("可用片段", f"J{row_number}", "视频镜头方向不能为不适用"))


def _plan_import(db, export_id: str, snapshot: dict, assets: list[dict],
                 segments: list[dict], errors: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    errors = list(errors)
    conflicts = []
    changes = []
    seen_assets = set()
    seen_segments = set()
    seen_new = set()
    exported_assets = snapshot["资产"]
    exported_segments = snapshot["可用片段"]
    for row in assets:
        asset_id = row.get("asset_id")
        row_number = row["_row"]
        if not isinstance(asset_id, str) or asset_id not in exported_assets:
            errors.append(_issue("资产", f"A{row_number}", "素材 ID 不在本次服务端导出记录中"))
            continue
        if asset_id in seen_assets:
            errors.append(_issue("资产", f"A{row_number}", "重复素材 ID"))
            continue
        seen_assets.add(asset_id)
        baseline = exported_assets[asset_id]
        _check_readonly(row, baseline, ASSET_COLUMNS[:10], "资产", errors)
        wanted = _normal_editable(row, ASSET_EDITABLE, "资产", errors)
        old = _normal_editable({**baseline, "_row": row_number}, ASSET_EDITABLE, "资产", errors)
        current_row = _current_asset_row(db, asset_id)
        if current_row is None or current_row["asset_version"] != baseline["asset_version"]:
            conflicts.append(_issue("资产", f"B{row_number}", "源素材版本已变化，请重新导出"))
            continue
        current = _normal_editable({**current_row, "_row": row_number}, ASSET_EDITABLE, "资产", errors)
        fields = _field_diff("资产", row_number, ASSET_EDITABLE, wanted, old, current, conflicts)
        if fields:
            changes.append({"kind": "asset_update", "sheet": "资产", "row": row_number,
                            "assetId": asset_id, "fields": fields})

    for row in segments:
        row_number = row["_row"]
        segment_id = row.get("segment_id")
        asset_id = row.get("asset_id")
        if not isinstance(asset_id, str) or asset_id not in exported_assets:
            errors.append(_issue("可用片段", f"B{row_number}", "素材 ID 不在本次导出范围中"))
            continue
        asset_baseline = exported_assets[asset_id]
        if row.get("asset_version") != asset_baseline["asset_version"]:
            errors.append(_issue("可用片段", f"C{row_number}", "素材版本与导出记录不一致"))
            continue
        wanted = _normal_editable(row, SEGMENT_EDITABLE, "可用片段", errors)
        _validate_segment_range(db, row, wanted, asset_baseline, errors)
        current_asset = db.connection.execute("SELECT quick_fingerprint FROM assets WHERE asset_id=?",
                                              (asset_id,)).fetchone()
        if current_asset is None or current_asset["quick_fingerprint"] != asset_baseline["asset_version"]:
            conflicts.append(_issue("可用片段", f"C{row_number}", "源素材版本已变化，请重新导出"))
            continue
        if segment_id in (None, ""):
            if row.get("export_revision") not in (None, ""):
                errors.append(_issue("可用片段", f"D{row_number}", "新增片段的导出修订须为空"))
                continue
            fingerprint = hashlib.sha256(_json({"exportId": export_id, "assetId": asset_id,
                                                 "values": wanted}).encode("utf-8")).hexdigest()
            if fingerprint in seen_new:
                continue
            seen_new.add(fingerprint)
            existing = db.connection.execute(
                "SELECT segment_id FROM catalog_exchange_new_rows WHERE export_id=? AND row_fingerprint=?",
                (export_id, fingerprint),
            ).fetchone()
            if existing is None:
                changes.append({"kind": "new_segment", "sheet": "可用片段", "row": row_number,
                                "assetId": asset_id, "rowFingerprint": fingerprint, "values": wanted})
            continue
        if not isinstance(segment_id, str) or segment_id not in exported_segments:
            errors.append(_issue("可用片段", f"A{row_number}", "片段 ID 不在本次服务端导出记录中"))
            continue
        if segment_id in seen_segments:
            errors.append(_issue("可用片段", f"A{row_number}", "重复片段 ID"))
            continue
        seen_segments.add(segment_id)
        baseline = exported_segments[segment_id]
        _check_readonly(row, baseline, SEGMENT_COLUMNS[:4], "可用片段", errors)
        old = _normal_editable({**baseline, "_row": row_number}, SEGMENT_EDITABLE, "可用片段", errors)
        current_row = _current_segment_row(db, asset_id, segment_id, asset_baseline["asset_version"])
        if current_row is None:
            conflicts.append(_issue("可用片段", f"A{row_number}", "片段已变化或不再属于当前素材"))
            continue
        current = _normal_editable({**current_row, "_row": row_number}, SEGMENT_EDITABLE,
                                   "可用片段", errors)
        fields = _field_diff("可用片段", row_number, SEGMENT_EDITABLE, wanted, old, current, conflicts)
        if fields:
            final = {**current, **{column: change["new"] for column, change in fields.items()}}
            _validate_segment_range(db, row, final, asset_baseline, errors)
            changes.append({"kind": "segment_update", "sheet": "可用片段", "row": row_number,
                            "assetId": asset_id, "segmentId": segment_id, "fields": fields,
                            "values": final, "baseValues": old})
    return changes, errors, conflicts


def preview_catalog_import(db, workbook_bytes: bytes) -> dict:
    """Parse workbook as inert data and persist an inspectable, unapplied preview."""
    export_id, snapshot, assets, segments, parsing_errors = _parse_workbook(db, workbook_bytes)
    changes, errors, conflicts = _plan_import(db, export_id, snapshot, assets, segments,
                                              parsing_errors)
    preview_id = uuid.uuid4().hex
    workbook_sha = hashlib.sha256(workbook_bytes).hexdigest()
    parsed = {"snapshot": snapshot, "assets": assets, "segments": segments,
              "parsingErrors": parsing_errors}
    with db.transaction() as conn:
        conn.execute("INSERT INTO catalog_exchange_import_previews("
                     "preview_id,export_id,workbook_sha256,parsed_json) VALUES(?,?,?,?)",
                     (preview_id, export_id, workbook_sha, _json(parsed)))
    return {"previewId": preview_id, "exportId": export_id, "changes": changes,
            "errors": errors, "conflicts": conflicts}


def _write_asset_fields(conn, asset_id: str, fingerprint: str, changed: dict) -> None:
    prior = conn.execute("SELECT asset_fingerprint,fields_json,revision FROM catalog_asset_annotations "
                         "WHERE asset_id=?", (asset_id,)).fetchone()
    fields = json.loads(prior["fields_json"]) if prior and prior["asset_fingerprint"] == fingerprint else {}
    for column, difference in changed.items():
        field = ASSET_FIELDS[column]
        if field == "usage_status" and difference["new"] == "":
            fields.pop(field, None)
        else:
            fields[field] = difference["new"]
    revision = (prior["revision"] + 1 if prior and prior["asset_fingerprint"] == fingerprint else 1)
    conn.execute(
        """INSERT INTO catalog_asset_annotations(asset_id,asset_fingerprint,fields_json,revision)
           VALUES(?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET
           asset_fingerprint=excluded.asset_fingerprint,fields_json=excluded.fields_json,
           revision=excluded.revision,updated_at=datetime('now')""",
        (asset_id, fingerprint, _json(fields), revision),
    )


def _write_segment_fields(conn, asset_id: str, fingerprint: str, segment_id: str,
                          values: dict, *, base_values: dict | None = None,
                          changed_columns: set[str] | None = None) -> None:
    prior = conn.execute("SELECT base_start_us,base_end_us,revision,asset_fingerprint,edited_fields_json "
                         "FROM catalog_segment_marks WHERE segment_id=? AND asset_id=?",
                         (segment_id, asset_id)).fetchone()
    if prior is not None:
        if prior["asset_fingerprint"] != fingerprint:
            raise ValueError("ASSET_STALE: 片段指向旧素材内容")
        base_start, base_end, revision = (prior["base_start_us"], prior["base_end_us"],
                                          prior["revision"] + 1)
    else:
        original = base_values or values
        base_start = original["源开始秒"] or 0
        base_end = original["源结束秒"] or 0
        revision = 1
    if changed_columns is None or (prior is not None and prior["edited_fields_json"] is None):
        edited_json = None
    else:
        earlier = set(json.loads(prior["edited_fields_json"])) if prior else set()
        edited_json = _json(sorted(earlier | changed_columns))
    start, end = values["源开始秒"] or 0, values["源结束秒"] or 0
    conn.execute(
        """INSERT INTO catalog_segment_marks(
           segment_id,asset_id,base_start_us,base_end_us,asset_fingerprint,
           source_start_us,source_end_us,title,description,camera_motion,motion_kind,
           subject_motion,usage_status,note,revision,tags,quality_issue,usable_reason,
           duplicate_note,edited_fields_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(segment_id) DO UPDATE SET
             source_start_us=excluded.source_start_us,source_end_us=excluded.source_end_us,
             title=excluded.title,description=excluded.description,
             camera_motion=excluded.camera_motion,motion_kind=excluded.motion_kind,
             subject_motion=excluded.subject_motion,usage_status=excluded.usage_status,
             note=excluded.note,revision=excluded.revision,tags=excluded.tags,
             quality_issue=excluded.quality_issue,usable_reason=excluded.usable_reason,
             duplicate_note=excluded.duplicate_note,
             edited_fields_json=excluded.edited_fields_json,updated_at=datetime('now')""",
        (segment_id, asset_id, base_start, base_end, fingerprint, start, end,
         values["描述"], values["描述"], values["镜头方向"], values["运动方式"] or None,
         values["主体运动"] or None, _FROM_EXCHANGE_USAGE[values["使用状态"]],
         values["备注"], revision, values["标签"], values["质量问题"],
         values["可用理由"], values["重复关系说明"], edited_json),
    )


def apply_catalog_import(db, preview_id: str, request_key: str) -> dict:
    """Recheck every field inside one transaction; replay is keyed by request + content."""
    if not isinstance(preview_id, str) or not preview_id:
        raise ValueError("PREVIEW_ID_INVALID: 请选择已生成的导入预览")
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 100 or not all(
        character.isalnum() or character in "_-" for character in request_key
    ):
        raise ValueError("REQUEST_KEY_INVALID: 导入请求编号无效")
    with db.transaction(immediate=True) as conn:
        preview = conn.execute(
            "SELECT * FROM catalog_exchange_import_previews WHERE preview_id=?", (preview_id,)
        ).fetchone()
        if preview is None:
            raise ValueError("PREVIEW_NOT_FOUND: 导入预览不存在")
        prior_receipt = conn.execute(
            "SELECT * FROM catalog_exchange_import_receipts WHERE request_key=?", (request_key,)
        ).fetchone()
        if prior_receipt is not None:
            if prior_receipt["workbook_sha256"] != preview["workbook_sha256"]:
                raise ValueError("REQUEST_REPLAY_CONFLICT: 同一请求编号对应不同文件")
            return json.loads(prior_receipt["receipt_json"])
        parsed = json.loads(preview["parsed_json"])
        changes, errors, conflicts = _plan_import(
            db, preview["export_id"], parsed["snapshot"], parsed["assets"],
            parsed["segments"], parsed["parsingErrors"],
        )
        if errors or conflicts:
            problem = (errors or conflicts)[0]
            raise ValueError(f"IMPORT_RECHECK_FAILED: {problem['sheet']}!{problem['cell']} {problem['message']}")
        applied = []
        for change in changes:
            asset_id = change["assetId"]
            fingerprint = parsed["snapshot"]["资产"][asset_id]["asset_version"]
            if change["kind"] == "asset_update":
                _write_asset_fields(conn, asset_id, fingerprint, change["fields"])
                applied.append(change)
            elif change["kind"] == "segment_update":
                _write_segment_fields(conn, asset_id, fingerprint, change["segmentId"],
                                      change["values"], base_values=change["baseValues"],
                                      changed_columns=set(change["fields"]))
                applied.append({key: value for key, value in change.items() if key != "baseValues"})
            else:
                segment_id = "seg-" + uuid.uuid4().hex
                _write_segment_fields(conn, asset_id, fingerprint, segment_id, change["values"])
                conn.execute("INSERT INTO catalog_exchange_new_rows(export_id,row_fingerprint,segment_id) "
                             "VALUES(?,?,?)", (preview["export_id"], change["rowFingerprint"], segment_id))
                applied.append({**change, "segmentId": segment_id})
        receipt = {"status": "applied", "previewId": preview_id,
                   "exportId": preview["export_id"], "requestKey": request_key,
                   "operator": "local-workbench", "changes": applied}
        conn.execute("INSERT INTO catalog_exchange_import_receipts("
                     "request_key,preview_id,workbook_sha256,receipt_json) VALUES(?,?,?,?)",
                     (request_key, preview_id, preview["workbook_sha256"], _json(receipt)))
        return receipt
