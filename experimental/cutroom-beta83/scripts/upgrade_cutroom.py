"""Maintenance-only plan, consistent snapshot and no-overwrite data restore."""
import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from media_catalog.path_utils import filesystem_path
from media_catalog.upgrade_recovery import RecoveryPlanError, _plain_path, build_recovery_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="维护盘点、一致快照与不覆盖恢复；不自动迁移或切换服务。")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="检查托管输入并列出后续缺项")
    plan.add_argument("--source-root", type=Path, required=True)
    plan.add_argument("--db", type=Path, required=True)
    plan.add_argument("--destination-root", type=Path, required=True)
    plan.add_argument("--out", type=Path, help="可选的新JSON文件；不可位于新旧安装内或覆盖已有文件")
    snapshot = commands.add_parser("snapshot", help="维护人员明确停机后复制已完整核验的数据")
    snapshot.add_argument("--source-root", type=Path, required=True)
    snapshot.add_argument("--db", type=Path, required=True)
    snapshot.add_argument("--destination-root", type=Path, required=True)
    snapshot.add_argument("--snapshot-root", type=Path, required=True)
    snapshot.add_argument("--source-stopped", action="store_true", help="维护人员已核明并停止源安装服务；本命令不停止进程")
    restore = commands.add_parser("restore", help="恢复完整快照到独立安装的数据空目录；拒绝覆盖")
    restore.add_argument("--snapshot-root", type=Path, required=True)
    restore.add_argument("--destination-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"snapshot", "restore"}:
            from media_catalog.upgrade_snapshot import create_upgrade_snapshot, restore_upgrade_snapshot
            if args.command == "restore":
                result = restore_upgrade_snapshot(args.snapshot_root, args.destination_root)
            else:
                if not args.source_stopped:
                    raise RecoveryPlanError("SOURCE_STOP_REQUIRED", "维护人员需先停止已核明的源服务；本命令不会停止进程。")
                recovery_plan = build_recovery_plan(args.source_root, args.db, args.destination_root)
                result = create_upgrade_snapshot(recovery_plan, args.snapshot_root, source_stopped=True)
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
            return 0
        output = _plain_path(args.out) if args.out else None
        if output:
            source = _plain_path(args.source_root)
            destination = _plain_path(args.destination_root)
            if output == source or output.is_relative_to(source) or output == destination or output.is_relative_to(destination):
                raise RecoveryPlanError("PLAN_OUTPUT_OVERLAP", "盘点报告须放在新旧安装目录之外。")
        value = build_recovery_plan(args.source_root, args.db, args.destination_root)
        payload = {"ok": True, "plan": value}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if output:
            with filesystem_path(output).open("x", encoding="utf-8") as stream:
                stream.write(text)
            print(json.dumps({"ok": True, "report": str(output), "canSwitchInstallation": False}, ensure_ascii=False))
        else:
            print(text, end="")
        return 0
    except FileExistsError:
        error = {"code": "PLAN_OUTPUT_EXISTS", "message": "报告文件已存在，已保留；请指定新文件。"}
    except RecoveryPlanError as exc:
        error = {"code": exc.code, "message": str(exc)}
    except OSError:
        error = {"code": "RECOVERY_PATH_UNAVAILABLE", "message": "维护路径不可访问；未切换安装，原目录和已有文件保留。"}
    print(json.dumps({"ok": False, "error": error}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
