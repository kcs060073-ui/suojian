"""Check implementation evidence without modifying the frozen 99-item contract."""

from __future__ import annotations

import csv
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/ux/final-2026-09-24/requirements.csv"
PROGRESS = ROOT / "docs/validation/requirements-99-progress.json"
FIELDS = ("ID", "状态", "代码提交", "验收证据", "已验证范围", "未闭合内容", "实现层级", "证据类型", "阻断原因")
LEVELS = {"未实现", "领域模块", "入口已接", "真实闭环"}
EVIDENCE_TYPES = {"静态源码", "离线单测", "隔离服务", "真实模型", "真实媒体", "真人", "目标软件"}
# Narrow input/UI requirements can be proven with real service interaction.
# Real media, AI and target-editor capabilities require their corresponding evidence.
SERVICE_REQUIREMENTS = {"CR-01", "CR-03", "CR-04", "SC-01", "ML-05", "DL-08"}
MODEL_REQUIREMENTS = {"ML-02", "SB-01", "AI-01", "AI-02", "AI-03", "AI-04", "AI-05", "AI-06", "AI-09", "AI-10", "AI-11", "AI-12", "AI-13", "AI-14"}
TARGET_REQUIREMENTS = {"ML-07", "DL-07"}


OBJECTIVE_REQUIREMENTS = {
    # Native picker acceptance is actual service/UI operation and on-site visible timing.
    "PF-01": {"隔离服务", "真人"},
    "PF-02": {"隔离服务", "真人"},
    # Timing and downstream impact are objective service/media contracts, not film approval.
    "PF-07": {"隔离服务", "真实媒体"},
    "RC-07": {"隔离服务", "真实媒体"},
    "ML-03": {"隔离服务", "真实媒体"},
    "AI-09": {"隔离服务", "真实媒体", "真实模型"},
    "MO-02": {"隔离服务", "真实媒体"},
    "ML-01": {"隔离服务", "真实媒体"},
    "FR-01": {"隔离服务", "真实媒体"},
    "PF-06": {"隔离服务"},
    "MO-04": {"隔离服务", "真实媒体"},
    "X-07": {"隔离服务", "真实媒体"},
    # Structured suggestion validation/anchor resolution is not aesthetic approval.
    "PK-05": {"隔离服务", "真实媒体", "真实模型"},
    "AI-03": {"隔离服务", "真实媒体", "真实模型"},
    # RC-02 is observable navigation/clock linkage, not a human film approval.
    "RC-02": {"隔离服务", "真实媒体"},
    "AI-02": {"隔离服务", "真实媒体", "真实模型"},
    # FR-04 is issue navigation/version linkage, not a human film approval.
    "FR-04": {"隔离服务", "真实媒体"},
    "ML-02": {"隔离服务", "真实媒体", "真实模型"},
    "AI-14": {"隔离服务", "真实媒体"},
    "X-06": {"隔离服务", "真实媒体", "真实模型"},
    "MO-01": {"隔离服务", "真实媒体", "真实模型"},
    "MO-03": {"隔离服务", "真实媒体"},
    "PF-04": {"隔离服务", "真实媒体"},
    "SB-01": {"隔离服务", "真实媒体", "真实模型"},
    "ML-09": {"隔离服务", "真实媒体"},
    "X-01": {"隔离服务", "真实媒体"},
    "SB-08": {"隔离服务", "真实媒体"},
    "X-04": {"隔离服务", "真实媒体"},
    "X-03": {"隔离服务", "真实媒体"},
    "X-02": {"隔离服务", "真实媒体"},
    "SB-02": {"隔离服务", "真实媒体"},
    "AI-12": {"隔离服务", "真实模型"},
    "ML-10": {"隔离服务", "真实媒体"},
    "FR-02": {"隔离服务", "真实媒体"},
    "PK-02": {"隔离服务"},
    "PK-10": {"隔离服务", "真实媒体"},
    "PF-05": {"隔离服务", "真实媒体"},
}


def required_evidence(item_id: str) -> set[str]:
    if item_id in OBJECTIVE_REQUIREMENTS:
        return OBJECTIVE_REQUIREMENTS[item_id]
    if item_id in SERVICE_REQUIREMENTS:
        return {"隔离服务"}
    if item_id in TARGET_REQUIREMENTS:
        return {"隔离服务", "目标软件"}
    if item_id in MODEL_REQUIREMENTS:
        return {"隔离服务", "真实模型", "真人"}
    return {"隔离服务", "真实媒体", "真人"}
STATUSES = {"待核验", "开发中", "待验收", "通过", "阻塞"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def check(root: Path | None = None) -> Counter[str]:
    project = ROOT if root is None else Path(root)
    contract_path = CONTRACT if root is None else project / "docs/ux/final-2026-09-24/requirements.csv"
    progress_path = PROGRESS if root is None else project / "docs/validation/requirements-99-progress.json"
    contract = read_csv(contract_path)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    expected = [row["ID"] for row in contract]
    if not isinstance(progress, list) or any(not isinstance(row, dict) for row in progress):
        raise ValueError("实施账本必须是对象数组")
    actual = [row.get("ID") for row in progress]
    errors: list[str] = []
    if len(expected) != 99 or len(set(expected)) != 99:
        errors.append("冻结需求清单不是 99 个唯一 ID")
    if actual != expected:
        errors.append("实施账本 ID 或顺序与冻结需求不一致")
    checked_commits: dict[str, bool] = {}
    for index, row in enumerate(progress):
        item_id = row.get("ID", f"row {index + 1}")
        if set(row) != set(FIELDS):
            errors.append(f"{item_id}: 实施账本列名不符合约定")
            continue
        if any(not isinstance(row[field], str) for field in FIELDS if field != "证据类型"):
            errors.append(f"{item_id}: 文本字段类型错误")
            continue
        status = row["状态"]
        if status not in STATUSES:
            errors.append(f"{item_id}: 未知状态 {status}")
        level = row["实现层级"]
        if level not in LEVELS:
            errors.append(f"{item_id}: 未知实现层级 {level}")
        types = row["证据类型"]
        if not isinstance(types, list) or any(not isinstance(x, str) or x not in EVIDENCE_TYPES for x in types):
            errors.append(f"{item_id}: 未知证据类型或类型错误")
            types = []
        if len(set(types)) != len(types):
            errors.append(f"{item_id}: 证据类型重复")
        if status != "待核验":
            for field in ("代码提交", "验收证据", "已验证范围"):
                if not row[field].strip():
                    errors.append(f"{item_id}: {status} 缺少 {field}")
        if status != "通过" and not row["未闭合内容"].strip():
            errors.append(f"{item_id}: {status} 缺少未闭合内容")
        if status == "阻塞" and not row["阻断原因"].strip():
            errors.append(f"{item_id}: 阻塞缺少阻断原因")
        if status == "通过":
            if not required_evidence(item_id).issubset(types):
                errors.append(f"{item_id}: 通过缺少适用证据 {sorted(required_evidence(item_id) - set(types))}")
            if row["未闭合内容"].strip() or row["阻断原因"].strip() or level == "未实现":
                errors.append(f"{item_id}: 通过不能同时存在未闭合内容或阻断")
        if level == "真实闭环" and not {"隔离服务", "真人"}.issubset(types):
            errors.append(f"{item_id}: 真实闭环缺少实际操作证据")
        for ref in row["代码提交"].split(";"):
            ref = ref.strip()
            if not ref:
                continue
            if not re.fullmatch(r"[0-9a-f]{7,40}", ref):
                errors.append(f"{item_id}: 无效提交 {ref}")
                continue
            if ref not in checked_commits:
                result = subprocess.run(
                    ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
                    cwd=project, capture_output=True, check=False,
                )
                checked_commits[ref] = result.returncode == 0
            if not checked_commits[ref]:
                errors.append(f"{item_id}: 提交不存在 {ref}")
        for ref in row["验收证据"].split(";"):
            ref = ref.strip()
            path = (project / ref).resolve()
            if ref and (not path.is_relative_to(project.resolve()) or not path.is_file()):
                errors.append(f"{item_id}: 证据文件不存在或越出项目 {ref}")
    if errors:
        raise ValueError("\n".join(errors))
    return Counter(row["状态"] for row in progress)


if __name__ == "__main__":
    try:
        counts = check()
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    print("99 项实施账本：" + "、".join(f"{status} {counts[status]}" for status in sorted(STATUSES) if counts[status]))
