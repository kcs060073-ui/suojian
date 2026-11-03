"""Human-readable CLI and stable JSON contract for the P0 media catalog."""

import argparse
import json
import math
import os
from pathlib import Path

from .path_utils import filesystem_path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

from .backup import backup_database, restore_database
from .database import CatalogDatabase
from .errors import ErrorInfo, classify_error
from .estimate import estimate_p0_bytes
from .jobs import HeavyJobBusy
from .workbench_worker import DurableCliJob
from .libraries import register_library, update_mount
from .probe import probe_pending_assets
from .scanner import LibraryOffline, scan_library
from .tools import (
    ToolNotFound,
    default_ffprobe_search_roots,
    find_ffmpeg,
    find_ffprobe,
)
from .audio_analysis import analyze_asset_audio, analyze_audio_pending, _load_model
from .cache import cache_status, clean_cache
from .packaging import list_packaging, register_packaging, unregister
from .packaging_plan import (
    get_plan,
    list_plans,
    propose,
    validate_manifests,
)
from .packaging_preview import confirm_plan_after_preview
from .privacy import confirm_flag, list_flags, scan_privacy
from .qa import list_reports, run_qa
from .publish import list_publish
from .snapshots import list_snapshots
from .assemble import assemble, gate_status
from .reviews import approve_review, reject_review
from .render_mix_adapter import PROJECT_ROOT
from .dedup import list_reports as dedup_list_reports, run_dedup
from .match import match_for_task, rank
from .script_adapter import (
    confirm_draft,
    draft_script,
    get_draft,
    list_drafts,
    reject_draft,
    ROUTES as SCRIPT_ROUTES,
)
from .preview import generate_asset_previews, generate_previews
from .selection import list_decisions, set_decision
from .segments import detect_segments, segments_pending
from .tasks import (
    add_asset,
    create_task,
    get_task,
    list_task_assets,
    list_tasks,
    remove_asset,
    set_status,
)
from .timecode import get_color_params, set_color_params, verify_timecode_consistency
from .scene_cards import (
    add_scene_card,
    confirm_scene_card,
    get_picture_lock,
    list_scene_cards,
    picture_view_digest,
    set_audio_policy,
    set_mix_form,
    set_picture_lock,
)
from .rough_media import (
    RoughMediaError, assert_picture_media_available, describe_rough_media,
)
from .ad_workflow import require_ad_picture_assets


DEFAULT_DATABASE = Path("runtime/media-catalog/catalog.sqlite")


def configure_stdio(stdout: Any = None, stderr: Any = None) -> None:
    """Use UTF-8 for stable human and JSON output on Windows consoles."""
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    for stream in (output_stream, error_stream):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


class MediaCatalogArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if getattr(parsed, "command", None) == "assemble":
            script = getattr(parsed, "script", None)
            words = getattr(parsed, "words", None)
            semantics = getattr(parsed, "semantics", None)
            ass = getattr(parsed, "ass", None)
            if any((script, words, semantics)) and not all((script, words, semantics)):
                self.error("assemble --script、--words 与 --semantics 必须成套提供")
            if ass and any((script, words, semantics)):
                self.error("assemble --ass 不能与 --script/--words/--semantics 同时使用")
        return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = MediaCatalogArgumentParser(
        prog="media-catalog",
        description="本地视频素材盘点工具（不会修改或复制原片）",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DATABASE,
        help="素材目录数据库位置",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="初始化素材目录数据库")

    library = commands.add_parser(
        "library",
        help="管理本机或移动硬盘素材库",
        description="管理本机或移动硬盘中的素材目录。",
    )
    library_commands = library.add_subparsers(dest="library_command", required=True)
    add = library_commands.add_parser("add", help="登记一个素材目录")
    add.add_argument("--id", required=True, dest="library_id")
    add.add_argument("--name", required=True)
    add.add_argument("--kind", required=True, choices=("local", "nas"))
    add.add_argument("--mount", required=True)
    mount = library_commands.add_parser("mount", help="更新素材库所在位置")
    mount.add_argument("--id", required=True, dest="library_id")
    mount.add_argument("--mount", required=True)

    scan = commands.add_parser("scan", help="扫描素材并完成基础技术检查")
    scan.add_argument("--library", required=True, dest="library_id")
    scan.add_argument(
        "--catalog-only",
        action="store_true",
        help="只登记文件，暂不运行 ffprobe 技术探测",
    )
    scan.add_argument("--json", action="store_true")

    probe = commands.add_parser("probe", help="继续素材技术检查")
    probe.add_argument("--library", required=True, dest="library_id")
    probe.add_argument("--workers", type=int)
    probe.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="查看素材盘点进度")
    status.add_argument("--json", action="store_true")

    search = commands.add_parser("search", help="按文件名或路径搜索素材")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--json", action="store_true")

    backup = commands.add_parser("backup", help="备份素材目录数据库")
    backup.add_argument("--dir", type=Path)
    backup.add_argument("--json", action="store_true")

    restore = commands.add_parser("restore", help="从备份恢复素材目录数据库")
    restore.add_argument("--from", required=True, type=Path, dest="backup_path")
    restore.add_argument("--json", action="store_true")

    doctor = commands.add_parser("doctor", help="检查运行环境并给出修复提示")
    doctor.add_argument("--json", action="store_true")

    # ---- P1 预览选材 ----
    preview = commands.add_parser("preview", help="生成预览产物（缩略图/接触表/代理/音频预览）")
    _add_scope_args(preview)
    preview.add_argument("--kinds", help="逗号分隔：thumbnail,contact_sheet,proxy,audio_preview")
    preview.add_argument("--workers", type=int)
    preview.add_argument("--json", action="store_true")

    audio = commands.add_parser("analyze-audio", help="音频分析：人声区间+粗转写+响度")
    _add_scope_args(audio)
    audio.add_argument("--model", default="tiny", help="whisper 模型大小（tiny/base/small）")
    audio.add_argument("--workers", type=int)
    audio.add_argument("--json", action="store_true")

    seg = commands.add_parser("segments", help="把视频切成候选镜头（带原片时间码）")
    _add_scope_args(seg)
    seg.add_argument("--workers", type=int)
    seg.add_argument("--json", action="store_true")

    select = commands.add_parser("select", help="给素材打候选/拒绝/确认")
    select.add_argument("--asset", required=True, dest="asset_id")
    select.add_argument("--decision", required=True, choices=("candidate", "rejected", "confirmed"))
    select.add_argument("--reason")
    select.add_argument("--by", dest="decided_by")
    select.add_argument("--json", action="store_true")

    task = commands.add_parser("tasks", help="任务目录（素材检查表）")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    t_create = task_commands.add_parser("create", help="登记任务")
    t_create.add_argument("--id", required=True, dest="task_id")
    t_create.add_argument("--title", required=True)
    t_create.add_argument("--type")
    t_create.add_argument("--brief", help="JSON 字符串")
    t_create.add_argument("--json", action="store_true")
    t_list = task_commands.add_parser("list", help="列出任务")
    t_list.add_argument("--limit", type=int, default=100)
    t_list.add_argument("--json", action="store_true")
    t_add = task_commands.add_parser("add-asset", help="把素材挂到任务")
    t_add.add_argument("--id", required=True, dest="task_id")
    t_add.add_argument("--asset", required=True, dest="asset_id")
    t_add.add_argument("--role")
    t_add.add_argument("--json", action="store_true")
    t_rm = task_commands.add_parser("remove-asset", help="从任务移除素材")
    t_rm.add_argument("--id", required=True, dest="task_id")
    t_rm.add_argument("--asset", required=True, dest="asset_id")
    t_rm.add_argument("--json", action="store_true")
    t_status = task_commands.add_parser("status", help="更新任务状态")
    t_status.add_argument("--id", required=True, dest="task_id")
    t_status.add_argument(
        "--status",
        required=True,
        choices=("draft", "brief_locked", "selection_locked", "packaging_confirmed", "reviewed"),
    )
    t_status.add_argument("--json", action="store_true")
    task.add_argument("--json", action="store_true")

    pkg = commands.add_parser("packaging", help="团队包装资产清单")
    pkg_commands = pkg.add_subparsers(dest="pkg_command", required=True)
    p_reg = pkg_commands.add_parser("register", help="登记包装件")
    p_reg.add_argument("--asset", required=True, dest="asset_id")
    p_reg.add_argument(
        "--kind",
        required=True,
        choices=("watermark", "title_card", "bgm", "subtitle_style", "transition", "font", "sticker", "other"),
    )
    p_reg.add_argument("--name")
    p_reg.add_argument("--notes")
    p_reg.add_argument("--json", action="store_true")
    p_list = pkg_commands.add_parser("list", help="列出包装件")
    p_list.add_argument("--kind")
    p_list.add_argument("--json", action="store_true")
    p_unreg = pkg_commands.add_parser("unregister", help="取消登记")
    p_unreg.add_argument("--asset", required=True, dest="asset_id")
    p_unreg.add_argument("--json", action="store_true")
    pkg.add_argument("--json", action="store_true")

    priv = commands.add_parser("privacy", help="隐私标记（只标记不打码）")
    priv_commands = priv.add_subparsers(dest="priv_command", required=True)
    p_scan = priv_commands.add_parser("scan", help="扫描隐私风险")
    _add_scope_args(p_scan)
    p_conf = priv_commands.add_parser("confirm", help="人工确认标记")
    p_conf.add_argument("--flag", required=True, dest="flag_id")
    p_conf.add_argument("--confirmed", action="store_true")
    p_listf = priv_commands.add_parser("list", help="列出标记")
    p_listf.add_argument("--asset")
    p_listf.add_argument("--unconfirmed", action="store_true")
    priv.add_argument("--json", action="store_true")

    csh = commands.add_parser("cache", help="预览缓存安全清理")
    csh_commands = csh.add_subparsers(dest="cache_command", required=True)
    c_status = csh_commands.add_parser("status", help="查看缓存占用")
    c_status.add_argument("--json", action="store_true")
    c_clean = csh_commands.add_parser("clean", help="清理最旧缓存到预算内")
    c_clean.add_argument("--max-bytes", type=int)
    c_clean.add_argument("--keep", help="逗号分隔保留的类型")
    c_clean.add_argument("--dry-run", action="store_true")
    c_clean.add_argument("--json", action="store_true")
    csh.add_argument("--json", action="store_true")

    # ---- P2 场景卡片 + 画面锁定 ----
    sc = commands.add_parser("scene-card", help="场景卡片（文案—事实—素材映射）")
    sc_commands = sc.add_subparsers(dest="sc_command", required=True)
    sc_add = sc_commands.add_parser("add", help="新增/更新一张场景卡片")
    sc_add.add_argument("--task", required=True, dest="task_id")
    sc_add.add_argument("--idx", required=True, type=int)
    sc_add.add_argument("--narrative", help="旁白文本")
    sc_add.add_argument("--start", type=float, help="旁白开始秒")
    sc_add.add_argument("--end", type=float, help="旁白结束秒")
    sc_add.add_argument("--asset")
    sc_add.add_argument("--segment")
    sc_add.add_argument(
        "--mix",
        choices=("数字人全屏", "broll全屏", "画中画", "信息卡"),
    )
    sc_add.add_argument("--enter", type=float, help="原片进入时间码")
    sc_add.add_argument("--exit", type=float, help="原片退出时间码")
    sc_add.add_argument(
        "--audio",
        choices=("keep", "mute", "duck", "clean", "replace"),
        help="原声策略草稿",
    )
    sc_add.add_argument(
        "--source",
        choices=("用户指定", "系统推荐", "长期库", "缺口替代"),
    )
    sc_add.add_argument("--suggestion", help="系统建议")
    sc_add.add_argument("--decision", help="最终决定")
    sc_add.add_argument("--json", action="store_true")
    sc_list = sc_commands.add_parser("list", help="列出任务全部场景卡片")
    sc_list.add_argument("--task", required=True, dest="task_id")
    sc_list.add_argument("--json", action="store_true")
    sc_mix = sc_commands.add_parser("set-mix", help="设置混剪形态/进入退出时间")
    sc_mix.add_argument("--card", required=True, dest="card_id")
    sc_mix.add_argument(
        "--mix",
        required=True,
        choices=("数字人全屏", "broll全屏", "画中画", "信息卡"),
    )
    sc_mix.add_argument("--enter", type=float)
    sc_mix.add_argument("--exit", type=float)
    sc_mix.add_argument("--json", action="store_true")
    sc_audio = sc_commands.add_parser("set-audio", help="设置原声策略")
    sc_audio.add_argument("--card", required=True, dest="card_id")
    sc_audio.add_argument(
        "--audio",
        required=True,
        choices=("keep", "mute", "duck", "clean", "replace"),
    )
    sc_audio.add_argument("--json", action="store_true")
    sc_conf = sc_commands.add_parser("confirm", help="确认场景卡片")
    sc_conf.add_argument("--card", required=True, dest="card_id")
    sc_conf.add_argument("--decision")
    sc_conf.add_argument("--json", action="store_true")

    pl = commands.add_parser("picture-lock", help="画面锁定门禁")
    pl_commands = pl.add_subparsers(dest="pl_command", required=True)
    pl_inspect = pl_commands.add_parser("inspect", help="查看当前逐镜素材版本，JSON 可保存后用于锁定")
    pl_inspect.add_argument("--task", required=True, dest="task_id")
    pl_inspect.add_argument("--json", action="store_true")
    pl_inspect.add_argument("--out", type=Path, help="以 UTF-8 写入新版本清单文件")
    pl_set = pl_commands.add_parser("set", help="锁定画面与顺序")
    pl_set.add_argument("--task", required=True, dest="task_id")
    pl_set.add_argument("--media-versions-file", required=True, type=Path,
                        help="inspect --out 导出的逐镜版本 JSON 文件")
    pl_set.add_argument("--version", default="picture-lock-v1")
    pl_set.add_argument("--json", action="store_true")

    # ---- P2-M4 包装资产库 + 方案 ----
    ast = commands.add_parser("assets", help="包装资产库（音色/BGM/字幕/文字卡/贴纸/花字）+ 方案推荐")
    ast_commands = ast.add_subparsers(dest="ast_command", required=True)
    ast_cat = ast_commands.add_parser("catalog", help="生成离线 HTML 资产目录（试听/预览）")
    ast_cat.add_argument("--out", default="assets/manifests/catalog.html")
    ast_cat.add_argument("--json", action="store_true")
    ast_rec = ast_commands.add_parser("recommend", help="为任务推 2–3 套包装方案（落库 draft）")
    ast_rec.add_argument("--task", required=True, dest="task_id")
    ast_rec.add_argument(
        "--route",
        required=True,
        choices=("数字人混剪", "园区宣传", "信息流广告"),
    )
    ast_rec.add_argument("--json", action="store_true")
    ast_val = ast_commands.add_parser("validate", help="校验所有 manifest（文件存在/授权合法）")
    ast_val.add_argument("--json", action="store_true")
    ast_plans = ast_commands.add_parser("plans", help="列出任务的包装方案")
    ast_plans.add_argument("--task", required=True, dest="task_id")
    ast_plans.add_argument("--json", action="store_true")
    ast_show = ast_commands.add_parser("show", help="查看某方案明细")
    ast_show.add_argument("--plan", required=True, dest="plan_id")
    ast_show.add_argument("--json", action="store_true")
    ast_conf = ast_commands.add_parser("confirm", help="人工确认某方案（包装确认门禁）")
    ast_conf.add_argument("--plan", required=True, dest="plan_id")
    ast_conf.add_argument("--preview-id", dest="preview_id", required=True)
    ast_conf.add_argument("--json", action="store_true")
    ast.add_argument("--json", action="store_true")

    # ---- P3-M1 规则版本化 ----
    rl = commands.add_parser("rules", help="规则版本管理（新任务默认最新，旧任务锁定创建时版本）")
    rl_commands = rl.add_subparsers(dest="rl_command", required=True)
    rl_list = rl_commands.add_parser("list", help="列出所有规则版本")
    rl_list.add_argument("--json", action="store_true")
    rl_cur = rl_commands.add_parser("current", help="显示当前最新版本号")
    rl_cur.add_argument("--json", action="store_true")
    rl_show = rl_commands.add_parser("show", help="查看某版本 payload")
    rl_show.add_argument("--version", type=int, required=True)
    rl_show.add_argument("--json", action="store_true")
    rl_bump = rl_commands.add_parser("bump", help="基于当前最新版本派生新版本（可覆盖阈值）")
    rl_bump.add_argument("--notes", default="规则迭代")
    rl_bump.add_argument("--patch", help="JSON 片段，合并进新版本 payload（如 '{\"qa\":{\"black\":{\"block_above\":0.5}}}'）")
    rl_bump.add_argument("--json", action="store_true")
    rl.add_argument("--json", action="store_true")

    # ---- P2-M5 自动质检 ----
    qa = commands.add_parser("qa", help="自动质检（黑场/冻结/静音/削波 + 工程一致性）")
    qa_commands = qa.add_subparsers(dest="qa_command", required=True)
    qa_run = qa_commands.add_parser("run", help="对任务（可选成片）跑一轮质检")
    qa_run.add_argument("--task", required=True, dest="task_id")
    qa_run.add_argument("--video", help="成片/样片路径（缺省只做工程一致性检查）")
    qa_run.add_argument("--json", action="store_true")
    qa_rep = qa_commands.add_parser("reports", help="列出任务的质检报告")
    qa_rep.add_argument("--task", required=True, dest="task_id")
    qa_rep.add_argument("--json", action="store_true")
    qa.add_argument("--json", action="store_true")

    # ---- P2-M6 发布准备与合规 ----
    pub = commands.add_parser("publish", help="发布准备与合规（封面/标题/简介/标签 + 敏感词）")
    pub_commands = pub.add_subparsers(dest="pub_command", required=True)
    pub_list = pub_commands.add_parser("list", help="列出发布记录")
    pub_list.add_argument("--task", required=True, dest="task_id")
    pub_list.add_argument("--json", action="store_true")
    pub.add_argument("--json", action="store_true")

    # ---- P2-M7 渲染快照 + 局部重跑 ----
    snap = commands.add_parser("snapshot", help="渲染快照（参数/产物可追溯，支撑局部重跑）")
    snap_commands = snap.add_subparsers(dest="snap_command", required=True)
    snap_list = snap_commands.add_parser("list", help="列出快照")
    snap_list.add_argument("--task", required=True, dest="task_id")
    snap_list.add_argument("--stage")
    snap_list.add_argument("--json", action="store_true")
    snap.add_argument("--json", action="store_true")

    # ---- P2-M8 端到端编排 + 样片验收 ----
    asm = commands.add_parser("assemble", help="两段式编排（真实渲染→QA→人工审片→发布）")
    asm.add_argument("--task", required=True, dest="task_id")
    asm_mode = asm.add_mutually_exclusive_group()
    asm_mode.add_argument(
        "--video",
        help="导入外部已有视频，仅做 QA（不代表端到端闭环）",
    )
    asm_mode.add_argument(
        "--render",
        action="store_true",
        help="从任务素材 ID 编译并执行 render-mix",
    )
    asm.add_argument("--ass", help="可选 ASS 字幕文件路径（需同名 .metadata.json）")
    asm.add_argument("--script", help="锁定文案路径；必须与 --words/--semantics 成套提供")
    asm.add_argument("--words", help="Whisper 词级时间戳路径；必须与 --script/--semantics 成套提供")
    asm.add_argument("--semantics", help="字幕语义 JSON 路径；必须与 --script/--words 成套提供")
    asm.add_argument("--output", help="真实渲染输出路径；首次 --render 必填")
    asm.add_argument("--platform", help="仅旧平台专用发布包使用；本地交付无需指定")
    asm.add_argument("--title")
    asm.add_argument("--desc", help="简介")
    asm.add_argument("--tags", help="逗号分隔标签")
    asm.add_argument("--json", action="store_true")

    review = commands.add_parser("review", help="审批 assemble 生成的可信审片候选")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_approve = review_commands.add_parser("approve", help="绑定 snapshot + QA 审批")
    review_approve.add_argument("--task", required=True, dest="task_id")
    review_approve.add_argument("--snapshot", required=True)
    review_approve.add_argument("--qa", required=True)
    review_approve.add_argument("--by", required=True, dest="reviewed_by")
    review_approve.add_argument(
        "--watch-session", required=True, dest="watch_session_id"
    )
    review_approve.add_argument("--notes")
    review_approve.add_argument("--json", action="store_true")

    review_reject = review_commands.add_parser("reject", help="绑定 snapshot + QA 记录拒绝")
    review_reject.add_argument("--task", required=True, dest="task_id")
    review_reject.add_argument("--snapshot", required=True)
    review_reject.add_argument("--qa", required=True)
    review_reject.add_argument("--by", required=True, dest="reviewed_by")
    review_reject.add_argument("--notes", required=True)
    review_reject.add_argument("--json", action="store_true")

    # ---- P3-M2 重复检测 ----
    dd = commands.add_parser("dedup", help="镜头级近重复检测（感知哈希，PIL+numpy）")
    dd_commands = dd.add_subparsers(dest="dd_command", required=True)
    dd_scan = dd_commands.add_parser("scan", help="对一组视频抽帧并检测近重复")
    dd_scan.add_argument("--videos", required=True, help="逗号分隔视频路径")
    dd_scan.add_argument("--task", dest="task_id", help="关联任务（写报告用）")
    dd_scan.add_argument("--scope", default="manual")
    dd_scan.add_argument("--threshold", type=int, default=10)
    dd_scan.add_argument("--json", action="store_true")
    dd_list = dd_commands.add_parser("list", help="列出重复检测报告")
    dd_list.add_argument("--task", dest="task_id")
    dd_list.add_argument("--json", action="store_true")
    dd.add_argument("--json", action="store_true")

    # ---- P3-M3 语义/内容匹配 ----
    mt = commands.add_parser("match", help="脚本↔候选镜头语义匹配（词法打分）")
    mt_commands = mt.add_subparsers(dest="mt_command", required=True)
    mt_shots = mt_commands.add_parser("shots", help="按脚本给任务候选镜头排序")
    mt_shots.add_argument("--task", required=True, dest="task_id")
    mt_shots.add_argument("--script", required=True)
    mt_shots.add_argument("--json", action="store_true")
    mt_list = mt_commands.add_parser("list", help="按脚本对给出条目排序")
    mt_list.add_argument("--script", required=True)
    mt_list.add_argument("--items", required=True, help="逗号分隔条目（文件名/asset_id）")
    mt_list.add_argument("--json", action="store_true")
    mt.add_argument("--json", action="store_true")

    edits = commands.add_parser('edits', help='限定文字修改：先预览、明确确认后应用，可安全撤销')
    edit_commands = edits.add_subparsers(dest='edit_command', required=True)
    for action in ('propose','list','get','apply','cancel','undo'):
        sub = edit_commands.add_parser(action)
        sub.add_argument('--task', required=True, dest='task_id')
        sub.add_argument('--json', action='store_true')
        if action in ('propose','apply','cancel','undo'): sub.add_argument('--request', required=True)
        if action in ('propose','apply','undo'): sub.add_argument('--revision', required=True)
        if action in ('get','apply','cancel','undo'): sub.add_argument('--proposal', required=True)
        if action == 'propose':
            sub.add_argument('--instruction', required=True)
            sub.add_argument('--plan')
        if action in ('apply','undo'): sub.add_argument('--confirm', action='store_true')

    rhythm = commands.add_parser('rhythm', help='本地节奏结构建议与修改草稿，不渲染、不自动应用')
    rhythm_commands = rhythm.add_subparsers(dest='rhythm_command', required=True)
    for action in ('build', 'list', 'preview', 'locate'):
        sub = rhythm_commands.add_parser(action)
        sub.add_argument('--task', required=True, dest='task_id')
        sub.add_argument('--json', action='store_true')
        if action != 'list': sub.add_argument('--revision', required=True)
        if action in ('preview', 'locate'):
            sub.add_argument('--report', required=True)
            sub.add_argument('--suggestion', required=True)

    rs = commands.add_parser('reshoot', help='本地补拍清单与缺口原因；不上传、不渲染、不替换镜头')
    rs_commands = rs.add_subparsers(dest='rs_command', required=True)
    for action in ('build', 'list', 'export'):
        sub = rs_commands.add_parser(action)
        sub.add_argument('--task', required=True, dest='task_id')
        sub.add_argument('--json', action='store_true')
        if action != 'list': sub.add_argument('--revision', required=True)
        if action == 'build': sub.add_argument('--project-root', type=Path, default=Path.cwd())
        if action == 'export':
            sub.add_argument('--report', required=True)
            sub.add_argument('--output', type=Path, required=True)
            sub.add_argument('--format', choices=('text', 'json'), default='text')

    # ---- P3-M4 脚本写作适配器 ----
    sc = commands.add_parser("script", help="脚本写作适配器（园区无文案入口草稿 + 人工确认回退）")
    sc_commands = sc.add_subparsers(dest="sc_command", required=True)
    sc_draft = sc_commands.add_parser("draft", help="为任务生成脚本草稿（落库 draft）")
    sc_draft.add_argument("--task", required=True, dest="task_id")
    sc_draft.add_argument("--route", required=True, choices=SCRIPT_ROUTES)
    sc_draft.add_argument("--assets", help="可选 asset_id 列表（逗号分隔，缺省取任务全部）")
    sc_draft.add_argument("--json", action="store_true")
    sc_confirm = sc_commands.add_parser("confirm", help="人工确认草稿生效（文案回退门禁）")
    sc_confirm.add_argument("--draft", required=True, dest="draft_id")
    sc_confirm.add_argument("--json", action="store_true")
    sc_reject = sc_commands.add_parser("reject", help="驳回草稿（改手写）")
    sc_reject.add_argument("--draft", required=True, dest="draft_id")
    sc_reject.add_argument("--json", action="store_true")
    sc_show = sc_commands.add_parser("show", help="查看草稿明细")
    sc_show.add_argument("--draft", required=True, dest="draft_id")
    sc_show.add_argument("--json", action="store_true")
    sc_list = sc_commands.add_parser("list", help="列出草稿")
    sc_list.add_argument("--task", dest="task_id")
    sc_list.add_argument("--json", action="store_true")
    sc.add_argument("--json", action="store_true")
    ve = commands.add_parser("visual-evidence", help="本地画质、运动及源区间相似证据（不上传素材）")
    ve_commands = ve.add_subparsers(dest="ve_command", required=True)
    va = ve_commands.add_parser("analyze", help="分析一个已登记素材；当前方法结果自动复用")
    va.add_argument("--asset", required=True, dest="asset_id")
    va.add_argument("--json", action="store_true")
    vc = ve_commands.add_parser("compare", help="直接比对两段已分析素材，不传播相似分组")
    vc.add_argument("--left", required=True)
    vc.add_argument("--right", required=True)
    for field in ("left-start", "left-end", "right-start", "right-end"):
        vc.add_argument("--" + field, type=float, help="原素材秒数；每侧起止同时填写")
    vc.add_argument("--json", action="store_true")
    vd = ve_commands.add_parser("correct", help="保存重复判断的人工修订，重分析保留")
    vd.add_argument("--evidence", type=int, required=True)
    vd.add_argument("--revision", type=int, required=True)
    vd.add_argument("--decision", choices=("same", "different", "unknown"), required=True)
    vd.add_argument("--reason", required=True)
    vd.add_argument("--json", action="store_true")
    return parser


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--library")
    parser.add_argument("--asset", dest="asset_id")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    json_requested = bool(getattr(args, "json", False))
    try:
        if args.command == "restore":
            return _restore(args)
        if args.command == "backup":
            # Preserve the source schema before any upgrade or legacy backfill.
            if not filesystem_path(args.db).is_file():
                raise FileNotFoundError(f"找不到素材目录数据库：{args.db}")
            return _backup(args)
        with CatalogDatabase.open(args.db) as db:
            db.migrate()
            if args.command == "init":
                print(f"素材目录已初始化：{args.db}")
                return 0
            if args.command == "library":
                return _library(db, args)
            if args.command == "scan":
                return _scan(db, args)
            if args.command == "probe":
                return _probe(db, args)
            if args.command == "status":
                return _status(db, args.json)
            if args.command == "search":
                return _search(db, args.query, args.limit, args.json)
            if args.command == "doctor":
                return _doctor(db, args)
            if args.command == "preview":
                return _preview(db, args)
            if args.command == "analyze-audio":
                return _analyze_audio(db, args)
            if args.command == "segments":
                return _segments(db, args)
            if args.command == "select":
                return _select(db, args)
            if args.command == "tasks":
                return _tasks(db, args)
            if args.command == "packaging":
                return _packaging(db, args)
            if args.command == "privacy":
                return _privacy(db, args)
            if args.command == "cache":
                return _cache(db, args)
            if args.command == "scene-card":
                return _scene_card(db, args)
            if args.command == "picture-lock":
                return _picture_lock(db, args)
            if args.command == "assets":
                return _assets(db, args)
            if args.command == "rules":
                return _rules(db, args)
            if args.command == "qa":
                return _qa(db, args)
            if args.command == "publish":
                return _publish(db, args)
            if args.command == "snapshot":
                return _snapshot(db, args)
            if args.command == "assemble":
                return _assemble(db, args)
            if args.command == "review":
                return _review(db, args)
            if args.command == "visual-evidence":
                return _visual_evidence(db, args)
            if args.command == "dedup":
                return _dedup(db, args)
            if args.command == 'edits':
                from .edit_proposals import propose_edit, get_edit_proposal, list_edit_proposals, apply_edit, cancel_edit, undo_edit
                action = args.edit_command
                if action == 'propose': result = propose_edit(db,args.task_id,args.request,args.revision,args.instruction,plan_id=args.plan)
                elif action == 'list': result = {'proposals': list_edit_proposals(db,args.task_id)}
                elif action == 'get': result = get_edit_proposal(db,args.task_id,args.proposal)
                elif action == 'cancel': result = cancel_edit(db,args.task_id,args.proposal,args.request)
                else:
                    if not args.confirm: raise ValueError('PROPOSAL_CONFIRM_REQUIRED: 查看差异后使用 --confirm 明确确认')
                    operation = apply_edit if action == 'apply' else undo_edit
                    result = operation(db,args.task_id,args.proposal,args.request,args.revision)
                print(json.dumps(result,ensure_ascii=False,indent=2))
                return 0
            if args.command == 'rhythm':
                from .rhythm_advice import build_rhythm_advice, get_rhythm_advice, preview_rhythm_edit, locate_rhythm_suggestion
                if args.rhythm_command == 'build': result = build_rhythm_advice(db, args.task_id, args.revision)
                elif args.rhythm_command == 'list': result = get_rhythm_advice(db, args.task_id)
                elif args.rhythm_command == 'preview': result = preview_rhythm_edit(db, args.task_id, args.report, args.suggestion, args.revision)
                else: result = locate_rhythm_suggestion(db, args.task_id, args.report, args.suggestion, args.revision)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == 'reshoot':
                from .creative_assistance import build_reshoot_list, get_reshoot_list, export_reshoot_list
                if args.rs_command == 'build': result = build_reshoot_list(db, args.task_id, args.revision, project_root=args.project_root)
                elif args.rs_command == 'list': result = get_reshoot_list(db, args.task_id)
                else:
                    content = export_reshoot_list(db, args.task_id, args.report, args.revision, format=args.format)
                    # Exclusive creation preserves an existing user file and any failed export.
                    with args.output.open('x', encoding='utf-8') as stream: stream.write(content)
                    result = {'path': str(args.output.resolve()), 'reportId': args.report}
                _print_json(result)
                return 0
            if args.command == "match":
                return _match(db, args)
            if args.command == "script":
                return _script(db, args)
    except KeyboardInterrupt:
        _print_error(
            ErrorInfo(
                "INTERRUPTED",
                True,
                "操作已中断；已经完成的进度会保留。",
                "下次告诉我继续即可。",
            ),
            json_requested,
        )
        return 130
    except (
        FileNotFoundError,
        HeavyJobBusy,
        KeyError,
        LibraryOffline,
        OSError,
        RuntimeError,
        sqlite3.Error,
        ToolNotFound,
        ValueError,
    ) as error:
        _print_error(classify_error(error), json_requested)
        return 1
    return 2


def _visual_evidence(db, args):
    from .quality_windows import analyze_quality_windows
    from .duplicate_evidence import compare_quality_results, correct_duplicate_evidence
    if args.ve_command == "analyze":
        row = db.connection.execute("SELECT quick_fingerprint FROM assets WHERE asset_id=?", (args.asset_id,)).fetchone()
        if row is None: raise KeyError("ASSET_NOT_FOUND: 素材不存在")
        result = analyze_quality_windows(db, args.asset_id, row[0])
        # Pixel descriptors are internal evidence, not a console payload.
        result = {**result, "frames": [{k:v for k,v in f.items() if k != "gray96"} for f in result["frames"]]}
    elif args.ve_command == "compare":
        def window(side):
            start, end = getattr(args, side + "_start"), getattr(args, side + "_end")
            if start is None and end is None: return None
            if start is None or end is None or not all(math.isfinite(t) for t in (start, end)) or start < 0 or end < start:
                raise ValueError("SOURCE_WINDOW_INVALID: 请同时填写有限的源起止秒数")
            return round(start * 1_000_000), round(end * 1_000_000)
        result = compare_quality_results(db, args.left, args.right, left_window=window("left"), right_window=window("right"))
    else:
        result = correct_duplicate_evidence(db, args.evidence, args.decision,
            expected_revision=args.revision, reason=args.reason)
    _print_json(result)
    return 0


def _library(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.library_command == "add":
        register_library(
            db,
            args.library_id,
            args.name,
            args.kind,
            args.mount,
        )
        _set_library_online(db, args.library_id, Path(args.mount).is_dir())
        print(f"素材库已登记：{args.name}（{args.library_id}）")
        if not Path(args.mount).is_dir():
            print("提示：目录当前不可访问，连接本机硬盘或移动硬盘后再开始扫描。")
        return 0
    update_mount(db, args.library_id, args.mount)
    _set_library_online(db, args.library_id, Path(args.mount).is_dir())
    print(f"素材库位置已更新：{args.library_id} → {args.mount}")
    return 0


def _scan(db: CatalogDatabase, args: argparse.Namespace) -> int:
    job = DurableCliJob(db, f"scan:{args.library_id}", "scan", args.library_id)
    job.recover_interrupted(300)
    job.start()
    previous_cursor = job.resume_cursor()
    failures: list[dict[str, str]] = []
    start_after = None
    if previous_cursor and previous_cursor.startswith("scan:"):
        start_after = previous_cursor.removeprefix("scan:")
    try:
        catalog = scan_library(
            db,
            args.library_id,
            start_after=start_after,
            on_progress=lambda path, _completed, _failed: job.heartbeat(
                f"scan:{path}"
            ),
            on_error=lambda path, error: failures.append(
                {"path": path, "error": error}
            )
            if len(failures) < 20
            else None,
        )
        probe = None
        warnings: list[str] = []
        if not args.catalog_only:
            try:
                ffprobe = find_ffprobe(
                    os.environ,
                    search_roots=default_ffprobe_search_roots(),
                )
                job.heartbeat("probe:")
                probe = probe_pending_assets(
                    db,
                    args.library_id,
                    ffprobe,
                    on_progress=lambda path, _completed, _failed: job.heartbeat(
                        f"probe:{path}"
                    ),
                )
            except ToolNotFound as error:
                warnings.append(str(error))
        job.complete()
    except KeyboardInterrupt as error:
        job.interrupt(error)
        raise
    except BaseException as error:
        job.fail(error)
        raise

    total_assets = db.connection.execute(
        """
        SELECT count(*)
        FROM asset_locations
        WHERE library_id=? AND state='available'
        """,
        (args.library_id,),
    ).fetchone()[0]
    result = {
        "library_id": args.library_id,
        "catalog": _summary_dict(catalog),
        "technical_probe": None if probe is None else _summary_dict(probe),
        "assets": total_assets,
        "failures": failures,
        "warnings": warnings,
    }
    if args.json:
        _print_json(result)
    else:
        print("素材扫描完成")
        print(f"已登记素材：{total_assets}")
        print(f"本次成功：{catalog.completed}，失败：{catalog.failed}")
        for failure in failures:
            print(f"待处理：{failure['path']}（{failure['error']}）")
        if probe is not None:
            print(f"技术探测：{probe.completed} / {probe.discovered}")
        for warning in warnings:
            print(f"待处理：{warning}")
    return 0


def _status(db: CatalogDatabase, as_json: bool) -> int:
    result = _status_data(db)
    if as_json:
        _print_json(result)
        return 0
    assets = result["assets"]
    probes = assets["technical_probe"]
    warnings = result["warnings"]
    p1 = result["p1"]
    print("快速盘点：完成" if assets["total"] else "快速盘点：尚未开始")
    print(f"已登记素材：{assets['total']}")
    print(f"技术探测：{probes['completed']} / {assets['available']}")
    print(
        "P1 预览选材："
        f"代理 {p1['preview_proxy']} 个｜音频分析 {p1['audio_analysis']} 个｜"
        f"切片 {p1['segment_assets']} 个视频｜隐私待确认 {p1['privacy_open']} 个｜"
        f"任务 {p1['tasks']} 个"
    )
    if p1["decisions"]:
        print(
            "候选决策："
            + "，".join(f"{k} {v}" for k, v in p1["decisions"].items())
        )
    print(f"当前阻塞：{'；'.join(warnings) if warnings else '无'}")
    print(
        "预计新增磁盘："
        f"约 {_format_bytes(estimate_p0_bytes(assets['total']))}，"
        "仅数据库与日志，不生成代理"
    )
    return 0


def _probe(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.workers is not None and not 1 <= args.workers <= 4:
        raise ValueError("技术探测工作单元必须在 1 到 4 之间")
    library = db.connection.execute(
        """
        SELECT kind, mount_path, online
        FROM libraries
        WHERE library_id=?
        """,
        (args.library_id,),
    ).fetchone()
    if library is None:
        raise KeyError(f"找不到素材库：{args.library_id}")
    if not Path(library["mount_path"]).is_dir():
        _set_library_online(db, args.library_id, False)
        raise LibraryOffline(
            f"素材目录当前不可访问：{library['mount_path']}。"
            "请连接本机硬盘或移动硬盘后重试。"
        )
    workers = args.workers or (1 if library["kind"] == "nas" else 2)
    pending_audio_video = db.connection.execute(
        """
        SELECT count(*)
        FROM assets a
        JOIN asset_locations al USING(asset_id)
        LEFT JOIN analysis_runs ar
          ON ar.asset_id=a.asset_id AND ar.stage='technical_probe'
        WHERE al.library_id=?
          AND al.state='available'
          AND a.media_kind IN ('video', 'audio')
          AND (ar.status IS NULL OR ar.status IN ('failed','interrupted','stale'))
        """,
        (args.library_id,),
    ).fetchone()[0]
    ffprobe = None
    if pending_audio_video:
        ffprobe = find_ffprobe(
            os.environ,
            search_roots=default_ffprobe_search_roots(),
        )

    job = DurableCliJob(
        db,
        f"probe:{args.library_id}",
        "probe",
        args.library_id,
    )
    job.recover_interrupted(300)
    job.start()
    try:
        summary = probe_pending_assets(
            db,
            args.library_id,
            ffprobe,
            workers=workers,
            on_progress=lambda _path, completed, failed: job.heartbeat(
                f"probe:{completed + failed}:{failed}"
            ),
        )
        job.complete()
    except KeyboardInterrupt as error:
        job.interrupt(error)
        raise
    except BaseException as error:
        job.fail(error)
        raise

    status = _status_data(db)
    progress = status["assets"]["technical_probe"]
    if summary.failed:
        next_action = "查看失败文件摘要；修复后可以只重试失败项。"
    elif progress["waiting"]:
        next_action = "稍后继续技术检查；已完成结果不会重做。"
    else:
        next_action = "技术检查已完成。"
    result = {
        "ok": True,
        "library_id": args.library_id,
        "technical_probe": _summary_dict(summary),
        "progress": progress,
        "warnings": status["warnings"],
        "next_action": next_action,
    }
    if args.json:
        _print_json(result)
    else:
        print(f"当前结果：本次完成 {summary.completed} 个素材技术检查")
        print(f"处理进度：剩余 {progress['waiting']} 个")
        print(
            "需要处理："
            f"{'；'.join(result['warnings']) if result['warnings'] else '无'}"
        )
        print(f"下一步：{next_action}")
    return 0


def _status_data(db: CatalogDatabase) -> dict[str, Any]:
    libraries_total, libraries_online = db.connection.execute(
        "SELECT count(*), coalesce(sum(online), 0) FROM libraries"
    ).fetchone()
    assets_total = db.connection.execute("SELECT count(*) FROM assets").fetchone()[0]
    available = db.connection.execute(
        "SELECT count(*) FROM asset_locations WHERE state='available'"
    ).fetchone()[0]
    missing = db.connection.execute(
        "SELECT count(*) FROM asset_locations WHERE state='missing'"
    ).fetchone()[0]
    probe_completed = db.connection.execute(
        """
        SELECT count(*) FROM analysis_runs
        WHERE stage='technical_probe' AND status='completed'
        """
    ).fetchone()[0]
    probe_failed = db.connection.execute(
        """
        SELECT count(*) FROM analysis_runs
        WHERE stage='technical_probe' AND status='failed'
        """
    ).fetchone()[0]
    preview_proxy = db.connection.execute(
        "SELECT count(*) FROM preview_cache WHERE kind='proxy'"
    ).fetchone()[0]
    audio_done = db.connection.execute(
        "SELECT count(*) FROM analysis_runs WHERE stage='audio_analysis' AND status='completed'"
    ).fetchone()[0]
    seg_assets = db.connection.execute(
        "SELECT count(DISTINCT asset_id) FROM segments"
    ).fetchone()[0]
    privacy_open = db.connection.execute(
        "SELECT count(*) FROM privacy_flags WHERE needs_human_confirm=1"
    ).fetchone()[0]
    decision_rows = db.connection.execute(
        "SELECT decision, count(*) AS count FROM selection_decisions GROUP BY decision"
    ).fetchall()
    decisions = {row["decision"]: row["count"] for row in decision_rows}
    task_count = db.connection.execute("SELECT count(*) FROM tasks").fetchone()[0]
    job_rows = db.connection.execute(
        "SELECT status, count(*) AS count FROM jobs GROUP BY status"
    ).fetchall()
    jobs = {row["status"]: row["count"] for row in job_rows}
    warnings: list[str] = []
    offline = libraries_total - libraries_online
    waiting = max(0, available - probe_completed - probe_failed)
    if offline:
        warnings.append(f"{offline} 个素材库离线")
    if missing:
        warnings.append(f"{missing} 个文件已不在原位置")
    if probe_failed:
        warnings.append(f"{probe_failed} 个文件技术探测失败")
    if waiting:
        warnings.append(f"{waiting} 个文件等待技术探测")
    if jobs.get("running", 0):
        warnings.append(f"{jobs['running']} 个任务正在运行")
    return {
        "libraries": {
            "total": libraries_total,
            "online": libraries_online,
            "offline": offline,
        },
        "assets": {
            "total": assets_total,
            "available": available,
            "missing": missing,
            "technical_probe": {
                "completed": probe_completed,
                "failed": probe_failed,
                "waiting": waiting,
            },
        },
        "p1": {
            "preview_proxy": preview_proxy,
            "audio_analysis": audio_done,
            "segment_assets": seg_assets,
            "privacy_open": privacy_open,
            "decisions": decisions,
            "tasks": task_count,
        },
        "jobs": jobs,
        "warnings": warnings,
    }


def _search(
    db: CatalogDatabase,
    query: str,
    limit: int,
    as_json: bool,
) -> int:
    if not 1 <= limit <= 500:
        raise ValueError("搜索条数必须在 1 到 500 之间")
    rows = db.connection.execute(
        """
        SELECT
          a.asset_id,
          a.media_kind,
          a.size_bytes,
          al.library_id,
          al.relative_path,
          al.state
        FROM asset_locations al
        JOIN assets a USING(asset_id)
        WHERE al.relative_path LIKE ? ESCAPE '\\'
        ORDER BY al.state='available' DESC, al.relative_path
        LIMIT ?
        """,
        (f"%{_escape_like(query)}%", limit),
    ).fetchall()
    result = [dict(row) for row in rows]
    if as_json:
        _print_json(result)
    elif not result:
        print("没有找到匹配素材。")
    else:
        print(f"找到 {len(result)} 条素材：")
        for row in result:
            print(f"- [{row['state']}] {row['relative_path']}")
    return 0


def _backup(args: argparse.Namespace) -> int:
    backup_dir = args.dir or args.db.parent / "backups"
    target = backup_database(args.db, backup_dir)
    if args.json:
        _print_json({"backup": str(target), "verified": True})
    else:
        print(f"数据库备份完成：{target}")
    return 0


def _restore(args: argparse.Namespace) -> int:
    preserved = restore_database(args.backup_path, args.db)
    result = {
        "restored": str(args.db),
        "previous_database": None if preserved is None else str(preserved),
        "verified": True,
    }
    if args.json:
        _print_json(result)
    else:
        print(f"数据库恢复完成：{args.db}")
        if preserved:
            print(f"恢复前数据库已保留：{preserved}")
    return 0


def _doctor(db: CatalogDatabase, args: argparse.Namespace) -> int:
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "python": {
            "ok": sys.version_info >= (3, 11),
            "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        },
        "database_directory": {
            "ok": os.access(args.db.parent, os.W_OK),
            "path": str(args.db.parent),
        },
    }
    try:
        ffprobe = find_ffprobe(
            os.environ,
            search_roots=default_ffprobe_search_roots(),
        )
        checks["ffprobe"] = {"ok": True, "path": str(ffprobe)}
    except ToolNotFound as error:
        checks["ffprobe"] = {"ok": False, "message": str(error)}
        warnings.append(str(error))
    library_rows = db.connection.execute(
        "SELECT library_id, mount_path FROM libraries ORDER BY library_id"
    ).fetchall()
    offline_libraries = [
        row["library_id"] for row in library_rows if not Path(row["mount_path"]).is_dir()
    ]
    checks["libraries"] = {
        "ok": not offline_libraries,
        "offline": offline_libraries,
    }
    if offline_libraries:
        warnings.append(f"以下素材库当前离线：{', '.join(offline_libraries)}")
    free = shutil.disk_usage(args.db.parent).free
    checks["disk"] = {"ok": free > 1024**3, "free_bytes": free}
    if free <= 1024**3:
        warnings.append("数据库所在磁盘剩余空间不足 1GB")
    running = db.connection.execute(
        """
        SELECT count(*) FROM jobs
        WHERE status='running' AND job_type IN ('scan', 'probe', 'proxy', 'render')
        """
    ).fetchone()[0]
    checks["heavy_job"] = {"ok": running == 0, "running": running}
    if running:
        warnings.append("已有重任务正在运行")
    result = {"ok": not warnings, "checks": checks, "warnings": warnings}
    if args.json:
        _print_json(result)
    else:
        for name, check in checks.items():
            print(f"{'通过' if check['ok'] else '待处理'}：{name}")
        for warning in warnings:
            print(f"修复建议：{warning}")
    return 0 if not warnings else 1


def _ffmpeg_path() -> Path:
    return find_ffmpeg(os.environ, search_roots=default_ffprobe_search_roots())


def _kinds_set(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    return {k.strip() for k in raw.split(",") if k.strip()}


def _preview(db: CatalogDatabase, args: argparse.Namespace) -> int:
    ffmpeg = _ffmpeg_path()
    kinds = _kinds_set(getattr(args, "kinds", None))
    if args.asset_id:
        produced = generate_asset_previews(db, args.asset_id, ffmpeg, kinds)
        result = {"asset_id": args.asset_id, "produced": produced}
        if args.json:
            _print_json(result)
        else:
            print(f"已生成预览：{args.asset_id}")
            for k, v in produced.items():
                print(f"  - {k}: {v}")
        return 0
    if not args.library:
        raise ValueError("需要 --library 或 --asset")
    job = DurableCliJob(db, f"preview:{args.library}", "preview", args.library)
    job.recover_interrupted(300)
    job.start()
    try:
        summary = generate_previews(
            db,
            args.library,
            ffmpeg,
            kinds=kinds,
            workers=args.workers or 2,
            on_progress=lambda _p, c, f: job.heartbeat(f"preview:{c}:{f}"),
        )
        job.complete()
    except KeyboardInterrupt as error:
        job.interrupt(error)
        raise
    except BaseException as error:
        job.fail(error)
        raise
    result = {"ok": True, "library_id": args.library, "preview": _summary_dict(summary)}
    if args.json:
        _print_json(result)
    else:
        print(f"预览生成完成：本次 {summary.completed} 个，失败 {summary.failed} 个")
    return 0


def _analyze_audio(db: CatalogDatabase, args: argparse.Namespace) -> int:
    ffmpeg = _ffmpeg_path()
    if args.asset_id:
        model = _load_model(args.model)
        result = analyze_asset_audio(db, args.asset_id, model, ffmpeg)
        if args.json:
            _print_json(result)
        else:
            print(f"音频分析：{args.asset_id}（语言 {result.get('language')}）")
            print(f"  人声区间：{len(result.get('segments', []))} 段，共 {result.get('speech_seconds')}s")
            if result.get("loud_anomaly"):
                print(f"  响度异常：{result['loud_anomaly']}")
            if result.get("contact_hits"):
                print(f"  转写命中联系方式：{len(result['contact_hits'])} 处（建议复核）")
        return 0
    if not args.library:
        raise ValueError("需要 --library 或 --asset")
    job = DurableCliJob(db, f"audio:{args.library}", "audio", args.library)
    job.recover_interrupted(300)
    job.start()
    try:
        summary = analyze_audio_pending(
            db,
            args.library,
            ffmpeg,
            model_size=args.model,
            workers=args.workers or 1,
            on_progress=lambda _p, c, f: job.heartbeat(f"audio:{c}:{f}"),
        )
        job.complete()
    except KeyboardInterrupt as error:
        job.interrupt(error)
        raise
    except BaseException as error:
        job.fail(error)
        raise
    result = {"ok": True, "library_id": args.library, "audio_analysis": _summary_dict(summary)}
    if args.json:
        _print_json(result)
    else:
        print(f"音频分析完成：本次 {summary.completed} 个，失败 {summary.failed} 个")
    return 0


def _segments(db: CatalogDatabase, args: argparse.Namespace) -> int:
    ffmpeg = _ffmpeg_path()
    if args.asset_id:
        segs = detect_segments(db, args.asset_id, ffmpeg)
        result = {"asset_id": args.asset_id, "segments": len(segs)}
        if args.json:
            _print_json(result)
        else:
            print(f"已切分 {len(segs)} 个候选镜头：{args.asset_id}")
        return 0
    if not args.library:
        raise ValueError("需要 --library 或 --asset")
    job = DurableCliJob(db, f"segments:{args.library}", "segments", args.library)
    job.recover_interrupted(300)
    job.start()
    try:
        summary = segments_pending(
            db, args.library, ffmpeg, workers=args.workers or 2,
            on_progress=lambda _p, c, f: job.heartbeat(f"segments:{c}:{f}"),
        )
        job.complete()
    except KeyboardInterrupt as error:
        job.interrupt(error)
        raise
    except BaseException as error:
        job.fail(error)
        raise
    result = {"ok": True, "library_id": args.library, "segments": _summary_dict(summary)}
    if args.json:
        _print_json(result)
    else:
        print(f"场景切片完成：本次 {summary.completed} 个视频，失败 {summary.failed} 个")
    return 0


def _select(db: CatalogDatabase, args: argparse.Namespace) -> int:
    decision = set_decision(db, args.asset_id, args.decision, args.reason, args.decided_by)
    if args.json:
        _print_json(decision)
    else:
        print(f"已记录决策：{args.asset_id} → {args.decision}")
        if args.reason:
            print(f"  理由：{args.reason}")
    return 0


def _tasks(db: CatalogDatabase, args: argparse.Namespace) -> int:
    cmd = args.task_command
    if cmd == "create":
        brief = None
        if args.brief:
            import json as _json_mod

            brief = _json_mod.loads(args.brief)
        task = create_task(db, args.task_id, args.title, args.type, brief)
        result = task
    elif cmd == "list":
        result = list_tasks(db, args.limit)
    elif cmd == "add-asset":
        add_asset(db, args.task_id, args.asset_id, args.role)
        result = list_task_assets(db, args.task_id)
    elif cmd == "remove-asset":
        remove_asset(db, args.task_id, args.asset_id)
        result = list_task_assets(db, args.task_id)
    elif cmd == "status":
        result = set_status(db, args.task_id, args.status, project_root=PROJECT_ROOT)
    else:
        raise ValueError(f"未知任务子命令：{cmd}")
    if args.json:
        _print_json(result)
    elif cmd in ("add-asset", "remove-asset"):
        print(f"任务 {args.task_id} 当前素材：")
        for r in result:
            print(f"  - [{r.get('decision') or '-'}] {r['relative_path']}（{r['role'] or '未指定'}）")
    elif cmd == "list":
        for r in result:
            print(f"- {r['task_id']} | {r['title']} | {r['status']}")
    else:
        print(f"任务：{result.get('task_id')} | {result.get('title')} | {result.get('status')}")
    return 0


def _packaging(db: CatalogDatabase, args: argparse.Namespace) -> int:
    cmd = args.pkg_command
    if cmd == "register":
        result = register_packaging(db, args.asset_id, args.kind, args.name, args.notes)
    elif cmd == "list":
        result = list_packaging(db, args.kind)
    elif cmd == "unregister":
        unregister(db, args.asset_id)
        result = {"unregistered": args.asset_id}
    else:
        raise ValueError(f"未知包装件子命令：{cmd}")
    if args.json:
        _print_json(result)
    elif cmd == "list":
        for r in result:
            print(f"- [{r['kind']}] {r.get('name') or r['relative_path']}")
    else:
        print(f"包装件操作完成：{cmd}")
    return 0


def _privacy(db: CatalogDatabase, args: argparse.Namespace) -> int:
    cmd = args.priv_command
    if cmd == "list":
        rows = list_flags(db, args.asset, args.unconfirmed)
        if args.json:
            _print_json(rows)
        elif not rows:
            print("没有隐私标记。")
        else:
            for r in rows:
                print(f"- {r['flag_type']} {r['asset_id']} "
                      f"[{r['start_sec']}–{r['end_sec']}] "
                      f"{'待人工确认' if r['needs_human_confirm'] else '已确认'}")
        return 0
    if cmd == "confirm":
        confirm_flag(db, args.flag_id, args.confirmed)
        if args.json:
            _print_json({"flag_id": args.flag_id, "confirmed": args.confirmed})
        else:
            print(f"已{'确认' if args.confirmed else '取消确认'}标记：{args.flag_id}")
        return 0
    # scan
    ffmpeg = _ffmpeg_path()
    if args.asset_id:
        res = scan_privacy(db, args.asset_id, ffmpeg)
        if args.json:
            _print_json(res)
        else:
            print(f"隐私扫描：{args.asset_id} 新建 {res['flags_created']} 个标记（{res['types']}）")
        return 0
    if not args.library:
        raise ValueError("需要 --library 或 --asset")
    library = db.connection.execute(
        "SELECT mount_path, online FROM libraries WHERE library_id=?", (args.library,)
    ).fetchone()
    if library is None:
        raise KeyError(f"找不到素材库：{args.library}")
    if not Path(library["mount_path"]).is_dir():
        raise LibraryOffline(f"素材目录当前不可访问：{library['mount_path']}。请连接后重试。")
    job = DurableCliJob(db, f"privacy:{args.library}", "privacy", args.library)
    job.recover_interrupted(300)
    job.start()
    try:
        rows = db.connection.execute(
            """
            SELECT DISTINCT a.asset_id
            FROM assets a
            JOIN asset_locations al USING(asset_id)
            JOIN libraries l USING(library_id)
            JOIN analysis_runs ar
              ON ar.asset_id=a.asset_id AND ar.stage='technical_probe' AND ar.status='completed'
            WHERE al.library_id=? AND al.state='available' AND l.online=1
              AND a.media_kind='video'
            """,
            (args.library,),
        ).fetchall()
        total = 0
        failures = []
        for r in rows:
            try:
                scan_privacy(db, r["asset_id"], ffmpeg)
                total += 1
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, sqlite3.Error) as error:
                failures.append({'asset_id': r['asset_id'], 'error': str(error)[-2000:]})
            job.heartbeat(f"privacy:{r['asset_id']}")
        if failures:
            job.fail(RuntimeError(f"隐私扫描 {len(failures)} 个素材失败"))
        else:
            job.complete()
    except KeyboardInterrupt as error:
        job.interrupt(error)
        raise
    except BaseException as error:
        job.fail(error)
        raise
    result = {"ok": not failures, "library_id": args.library, "videos_scanned": total,
              "failed": len(failures), "errors": failures}
    if args.json:
        _print_json(result)
    else:
        print(f"隐私扫描完成：{total} 个视频，失败 {len(failures)} 个")
    return 1 if failures else 0


def _cache(db: CatalogDatabase, args: argparse.Namespace) -> int:
    cmd = args.cache_command
    if cmd == "status":
        result = cache_status(db)
    elif cmd == "clean":
        keep = _kinds_set(args.keep) if getattr(args, "keep", None) else None
        result = clean_cache(
            db, args.max_bytes, keep_kinds=keep, dry_run=args.dry_run
        )
    else:
        raise ValueError(f"未知缓存子命令：{cmd}")
    if args.json:
        _print_json(result)
    elif cmd == "status":
        print(f"缓存目录：{result['cache_dir']}")
        print(f"条目数：{result['entries']}，已占用：{_format_bytes(result['total_bytes'])}")
        print(f"按类型：{result['by_kind']}")
        if result["missing_files"]:
            print(f"注：{result['missing_files']} 个登记项文件已不在磁盘")
    else:
        verb = "将清理" if result["dry_run"] else "已清理"
        print(
            f"{verb} {len(result['deleted'])} 个文件，"
            f"释放 {_format_bytes(result['freed_bytes'])}，"
            f"失败 {len(result['failed'])}"
        )
    return 1 if cmd == "clean" and result["failed"] else 0


def _scene_card(db: CatalogDatabase, args: argparse.Namespace) -> int:
    cmd = args.sc_command
    if cmd == "add":
        result = add_scene_card(
            db, args.task_id, args.idx, args.narrative, args.start, args.end,
            args.asset, args.segment, args.mix, args.enter, args.exit,
            args.audio, args.source, args.suggestion, args.decision,
        )
    elif cmd == "list":
        result = list_scene_cards(db, args.task_id)
    elif cmd == "set-mix":
        result = set_mix_form(db, args.card_id, args.mix, args.enter, args.exit)
    elif cmd == "set-audio":
        result = set_audio_policy(db, args.card_id, args.audio)
    elif cmd == "confirm":
        result = confirm_scene_card(db, args.card_id, args.decision)
    else:
        raise ValueError(f"未知场景卡片子命令：{cmd}")
    if args.json:
        _print_json(result)
    elif cmd == "list":
        if not result:
            print(f"任务 {args.task_id} 还没有场景卡片。")
        for r in result:
            mix = r.get("mix_form") or "-"
            aud = r.get("audio_policy_draft") or "-"
            flag = "✓" if r.get("confirm_status") else " "
            print(f"  [{flag}] #{r['idx']} {mix} | 原声:{aud} | {r.get('relative_path') or r.get('asset_id') or '-'}")
            if r.get("narrative_text"):
                print(f"      旁白：{r['narrative_text']}")
    else:
        ident = getattr(args, "card_id", None) or getattr(args, "task_id", "")
        print(f"场景卡片操作完成：{cmd} → {ident}")
    return 0


def _picture_lock(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.pl_command == "inspect":
        if get_task(db, args.task_id) is None:
            raise KeyError(f"任务不存在：{args.task_id}")
        shots = []
        versions = {}
        cards = list_scene_cards(db, args.task_id)
        view_digest = picture_view_digest(db, args.task_id, cards)
        for card in cards:
            asset_id = card.get("asset_id")
            media = describe_rough_media(db, args.task_id, asset_id) if asset_id else None
            version = media.get("preview_version") if media else None
            if version:
                versions[card["card_id"]] = version
            shots.append({
                "cardId": card["card_id"], "position": card["idx"] + 1,
                "narrativeText": card.get("narrative_text"), "assetId": asset_id,
                "previewAvailable": bool(media and media.get("preview_available")),
                "problem": media.get("preview_problem") if media else "尚未绑定画面素材",
            })
        result = {"taskId": args.task_id, "mediaVersions": versions, "shots": shots,
                  "pictureViewDigest": view_digest,
                  "versionsComplete": bool(shots) and len(versions) == len(shots)}
        if args.out is not None:
            with args.out.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        if args.json:
            _print_json(result)
        else:
            for shot in shots:
                print(f"镜头 {shot['position']}：{shot['narrativeText'] or ''} "
                      f"{'可预览' if shot['previewAvailable'] else shot['problem']}")
            print("全部镜头有可访问版本；锁定时会重新验证解码"
                  if result["versionsComplete"] else "请先恢复缺失素材")
            if args.out is not None:
                print(f"版本清单：{args.out}")
        return 0
    if args.pl_command != "set":
        raise ValueError(f"未知画面锁定子命令：{args.pl_command}")
    try:
        version_file = args.media_versions_file
        if version_file.stat().st_size > 1_000_000:
            raise ValueError("版本清单过大")
        examined = json.loads(version_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise RoughMediaError(400, "ROUGH_MEDIA_VERSION_REQUIRED",
                              "请先导出当前逐镜素材版本清单。") from error
    if not isinstance(examined, dict) or examined.get("taskId") != args.task_id:
        raise RoughMediaError(409, "ROUGH_MEDIA_VERSION_CHANGED",
                              "版本清单与当前任务不一致，请重新查看画面。")
    versions = examined.get("mediaVersions")
    if not isinstance(versions, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in versions.items()
    ):
        raise RoughMediaError(400, "ROUGH_MEDIA_VERSION_REQUIRED",
                              "逐镜素材版本清单无效，请重新导出。")
    view_digest = examined.get("pictureViewDigest")
    if not isinstance(view_digest, str) or len(view_digest) != 64 or any(
        char not in "0123456789abcdef" for char in view_digest
    ):
        raise RoughMediaError(400, "PICTURE_VIEW_REQUIRED",
                              "请重新导出当前画面状态清单。")
    assert_picture_media_available(db, args.task_id, versions, verify_decoding=True)
    task = get_task(db, args.task_id)
    if task is None:
        raise KeyError(f"任务不存在：{args.task_id}")
    if task["task_type"] == "ad_mix":
        require_ad_picture_assets(db, args.task_id)
    result = set_picture_lock(db, args.task_id, args.version,
                              confirm_all=True, project_root=PROJECT_ROOT,
                              expected_view_digest=view_digest,
                              expected_media_versions=versions)
    if args.json:
        _print_json(result)
    else:
        print(f"画面已锁定：{args.task_id}（{result['lock_version']}）")
        print("提示：锁定后修改场景卡片会被标记 changed_since_lock，需说明重跑范围。")
    return 0


def _set_library_online(
    db: CatalogDatabase,
    library_id: str,
    online: bool,
) -> None:
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE libraries
            SET online=?, updated_at=datetime('now')
            WHERE library_id=?
            """,
            (int(online), library_id),
        )
        if not online:
            connection.execute(
                "UPDATE asset_locations SET state='offline' WHERE library_id=?",
                (library_id,),
            )


def _summary_dict(summary) -> dict[str, int]:
    return {
        "discovered": summary.discovered,
        "completed": summary.completed,
        "failed": summary.failed,
        "remaining": summary.remaining,
    }


def _error_text(error: BaseException) -> str:
    if isinstance(error, KeyError) and error.args:
        return str(error.args[0])
    return str(error)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024**2:
        return f"{value / 1024:.1f} KB"
    return f"{value / 1024**2:.1f} MB"


def _rules(db: CatalogDatabase, args: argparse.Namespace) -> int:
    import json as _json
    from .rules import add_version, current_version, list_versions, show_version

    if args.rl_command == "list":
        rows = list_versions(db)
        if args.json:
            _print_json(rows)
        else:
            for r in rows:
                print(f"v{r['version']}  {r['applied_at']}  {r['notes']}")
        return 0
    if args.rl_command == "current":
        v = current_version(db)
        if args.json:
            _print_json({"version": v})
        else:
            print(f"当前最新规则版本：v{v}")
        return 0
    if args.rl_command == "show":
        row = show_version(db, args.version)
        if row is None:
            print(f"版本 v{args.version} 不存在")
            return 1
        if args.json:
            _print_json(row)
        else:
            print(f"版本 v{row['version']}（{row['applied_at']}）：{row['notes']}")
            print(_json.dumps(row["payload"], ensure_ascii=False, indent=2))
        return 0
    if args.rl_command == "bump":
        base = show_version(db, current_version(db)) or {}
        payload = dict(base.get("payload", {}) or {})
        if args.patch:
            try:
                patch = _json.loads(args.patch)
            except _json.JSONDecodeError as e:
                print(f"--patch 不是合法 JSON：{e}")
                return 1
            _deep_merge(payload, patch)
        new_v = add_version(db, payload, args.notes)
        if args.json:
            _print_json({"version": new_v})
        else:
            print(f"已派生新版本 v{new_v}（notes: {args.notes}）")
        return 0
    return 1


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def _assets(db: CatalogDatabase, args: argparse.Namespace) -> int:
    from .packaging_plan import (
        MANIFEST_DIR,
        load_all_manifests,
        load_manifest,
    )

    cmd = args.ast_command
    if cmd == "validate":
        report = validate_manifests()
        if args.json:
            _print_json(report)
        elif report["ok"]:
            print("✓ 所有 manifest 校验通过")
            for c in report["categories"]:
                print(f"  - {c['category']}: {c['count']} 项")
        else:
            print("✗ manifest 校验未通过：")
            for e in report["errors"]:
                print(f"  ! {e}")
            for c in report["categories"]:
                if c["missing_files"]:
                    print(f"  ! {c['category']} 缺文件：{c['missing_files']}")
                if c["bad_license"]:
                    print(f"  ! {c['category']} 非法授权：{c['bad_license']}")
        return 0 if report["ok"] else 1

    if cmd == "recommend":
        plan_ids = propose(db, args.task_id, args.route,
                           project_root=PROJECT_ROOT)
        plans = [get_plan(db, pid) for pid in plan_ids]
        if args.json:
            _print_json({"task_id": args.task_id, "route": args.route, "plans": plans})
        else:
            print(f"已为任务 {args.task_id}（{args.route}）推 {len(plans)} 套草案：")
            for p in plans:
                print(f"\n● {p['plan_name']}  [{p['plan_id']}]  {p['status']}")
                for it in p["items"]:
                    ref = it["asset_ref"] or "-"
                    print(f"    - {it['slot']:10s} {ref}  {it['asset_file'] or ''}")
            print("\n试听后用 `assets confirm --plan <plan_id>` 确认其中一套。")
        return 0

    if cmd == "plans":
        rows = list_plans(db, args.task_id)
        if args.json:
            _print_json(rows)
        elif not rows:
            print(f"任务 {args.task_id} 暂无包装方案，先用 `assets recommend` 生成。")
        else:
            for r in rows:
                print(f"- {r['plan_name']}  [{r['plan_id']}]  {r['status']}  ({r['item_count']} 槽位)")
        return 0

    if cmd == "show":
        plan = get_plan(db, args.plan_id)
        if plan is None:
            raise KeyError(f"找不到包装方案：{args.plan_id}")
        if args.json:
            _print_json(plan)
        else:
            print(f"{plan['plan_name']}  [{plan['plan_id']}]  {plan['status']}  route={plan['route']}")
            for it in plan["items"]:
                print(f"  - {it['slot']:10s} {it['asset_ref'] or '-'}  {it['asset_file'] or ''}  "
                      f"{'备份' if it['is_backup'] else ''} {it['notes'] or ''}")
        return 0

    if cmd == "confirm":
        draft = get_plan(db, args.plan_id)
        if draft is None:
            raise KeyError(f"找不到包装方案：{args.plan_id}")
        plan = confirm_plan_after_preview(
            db,
            draft["task_id"],
            args.plan_id,
            args.preview_id,
            project_root=PROJECT_ROOT,
        )
        if args.json:
            _print_json(plan)
        else:
            print(f"✓ 已确认方案：{plan['plan_name']} [{plan['plan_id']}]，任务状态推进到 packaging_confirmed")
        return 0

    # catalog: 生成离线 HTML 目录
    out_path = Path(args.out)
    root = MANIFEST_DIR.resolve().parents[1]
    html = _build_assets_catalog_html(load_all_manifests(), root, out_path.parent)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    if args.json:
        _print_json({"catalog": str(out_path)})
    else:
        print(f"✓ 资产目录已生成：{out_path}")
        print(f"  用浏览器打开即可试听 BGM / 预览品牌框。")
    return 0


def _build_assets_catalog_html(manifests: dict, root: Path, html_dir: Path) -> str:
    from html import escape

    sections: list[str] = []
    for cat, data in manifests.items():
        items = data.get("items", [])
        if not items:
            sections.append(
                f"<section><h2>{escape(cat)}</h2>"
                f"<p class='muted'>（空壳，待补充素材）</p></section>"
            )
            continue
        rows = []
        for it in items:
            f = it.get("file")
            media = ""
            if f:
                rel = Path(os.path.relpath(root / f, html_dir)).as_posix()
                if rel.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg")):
                    media = (
                        f"<img src='{escape(rel)}' alt='{escape(str(it.get('name', '')))}' "
                        f"style='max-height:96px;max-width:220px;vertical-align:middle'>"
                    )
                else:
                    media = (
                        f"<audio controls preload='none' src='{escape(rel)}'></audio>"
                    )
            lic = it.get("license", "unknown")
            rows.append(
                f"<li><div class='name'>{escape(str(it.get('name', it.get('id'))))}</div>"
                f"<div class='meta'>{escape(it.get('id',''))} · "
                f"<span class='lic lic-{escape(lic)}'>{escape(lic)}</span> "
                f"{escape(it.get('mood') or it.get('note') or '')}</div>"
                f"{media}</li>"
            )
        sections.append(
            f"<section><h2>{escape(cat)}</h2><ul>{''.join(rows)}</ul></section>"
        )
    cats = ", ".join(manifests.keys())
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>包装资产目录</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;margin:0;padding:24px;background:#fafafa;color:#222}}
 h1{{font-size:20px}} h2{{font-size:15px;border-left:4px solid #FD9437;padding-left:8px;margin-top:24px}}
 section{{background:#fff;border:1px solid #eee;border-radius:8px;padding:8px 16px;margin-bottom:12px}}
 ul{{list-style:none;padding:0;margin:0}} li{{padding:10px 0;border-bottom:1px solid #f0f0f0}}
 .name{{font-weight:600}} .meta{{color:#666;font-size:12px;margin:2px 0 6px}}
 .muted{{color:#999}} audio{{width:100%;max-width:420px}}
 .lic{{padding:1px 6px;border-radius:10px;font-size:11px}}
 .lic-commercial-approved{{background:#e6f4ea;color:#137333}}
 .lic-internal-only{{background:#fef7e0;color:#b06000}}
 .lic-unknown{{background:#eee;color:#666}}
 .lic-restricted,.lic-prohibited{{background:#fce8e8;color:#c5221f}}
</style></head>
<body>
<h1>包装资产目录</h1>
<p class='muted'>分类：{escape(cats)}。BGM 可点播放试听；音色为 TTS，无音频文件。</p>
{''.join(sections)}
</body></html>"""


def _qa(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.qa_command == "reports":
        rows = list_reports(db, args.task_id)
        if args.json:
            _print_json(rows)
        elif not rows:
            print(f"任务 {args.task_id} 暂无质检报告，先用 `qa run` 生成。")
        else:
            for r in rows:
                mark = "✓ passed" if r["status"] == "passed" else "✗ blocked"
                print(f"- {mark}  [{r['report_id']}]  问题 {r['issue_count']} "
                      f"(阻断 {r['blocking_count']})  {r['created_at']}")
        return 0

    # run
    report = run_qa(db, args.task_id, args.video)
    if args.json:
        _print_json(report)
        return 0 if report["status"] == "passed" else 1
    icon = "✓ 通过" if report["status"] == "passed" else "✗ 阻断"
    print(f"质检 {icon}：任务 {report['task_id']}  问题 {report['issue_count']} "
          f"(阻断 {report['blocking_count']})  report_id={report['report_id']}")
    for it in report["issues"]:
        sev = {"blocking": "🔴阻断", "warn": "🟡警告", "info": "🟢正常"}.get(it["severity"], it["severity"])
        print(f"  [{sev}] {it['check']}: {it['detail']}")
    return 0 if report["status"] == "passed" else 1


def _publish(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.pub_command == "list":
        rows = list_publish(db, args.task_id)
        if args.json:
            _print_json(rows)
        elif not rows:
            print(f"任务 {args.task_id} 暂无发布记录；请通过 assemble 审批闭环生成。")
        else:
            for r in rows:
                mark = {"cleared": "✓已合规", "blocked": "✗有命中", "draft": "草稿"}.get(r["status"], r["status"])
                print(f"- [{mark}] {r['platform']}  [{r['record_id']}]  {r['created_at']}")
        return 0

    raise RuntimeError("PUBLISH_BUILD_DISABLED: 发布包只能由 assemble 审批闭环生成")


def _snapshot(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.snap_command == "list":
        rows = list_snapshots(db, args.task_id, args.stage)
        if args.json:
            _print_json(rows)
        elif not rows:
            print(f"任务 {args.task_id} 暂无渲染快照。")
        else:
            for r in rows:
                print(f"- [{r['stage']}] {r['snapshot_id']}  out={r['output_path']}  "
                      f"parent={r['parent_snapshot']}  {r['created_at']}")
        return 0

    raise RuntimeError("SNAPSHOT_ADD_DISABLED: 快照只能由 assemble 可信生产器写入")


def _review(db: CatalogDatabase, args: argparse.Namespace) -> int:
    record = approve_review if args.review_command == "approve" else reject_review
    kwargs = {}
    if args.review_command == "approve":
        kwargs["watch_session_id"] = args.watch_session_id
    review = record(
        db, args.task_id, args.snapshot, args.qa, args.reviewed_by, args.notes,
        **kwargs,
    )
    if args.json:
        _print_json(review)
    elif review["decision"] == "approved":
        print(f"✓ 审片已绑定批准：{review['approval_id']}")
    else:
        print(f"✓ 审片已绑定拒绝：{review['approval_id']}")
    return 0


def _dedup(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.dd_command == "scan":
        videos = [v.strip() for v in args.videos.split(",") if v.strip()]
        report = run_dedup(db, args.task_id, videos, args.scope, args.threshold)
        if args.json:
            _print_json(report)
        else:
            print(f"报告 {report['report_id']}：{report['video_count']} 视频 / "
                  f"{report['frame_count']} 帧 / 发现 {report['group_count']} 个近重复组")
            for g in report["groups"]:
                print(f"  组({g['size']}): " + ", ".join(
                    f"{m['src']}#{m['frame']}" for m in g["members"]))
        return 0
    if args.dd_command == "list":
        for r in dedup_list_reports(db, args.task_id):
            print(f"{r['report_id']}  {r['created_at']}  {r['scope']}  "
                  f"组数={r['group_count']}")
        return 0
    return 1


def _match(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.mt_command == "shots":
        ranked = match_for_task(db, args.task_id, args.script)
        if args.json:
            _print_json(ranked)
        else:
            print(f"按脚本相关度排序（任务 {args.task_id}）：")
            for c in ranked:
                print(f"  {c['score']:.3f}  {c['id']}  {c.get('path','')}")
        return 0
    if args.mt_command == "list":
        items = [i.strip() for i in args.items.split(",") if i.strip()]
        cands = [{"id": it, "text": it} for it in items]
        ranked = rank(args.script, cands)
        if args.json:
            _print_json(ranked)
        else:
            for c in ranked:
                print(f"  {c['score']:.3f}  {c['id']}")
        return 0
    return 1


def _script(db: CatalogDatabase, args: argparse.Namespace) -> int:
    if args.sc_command == "draft":
        assets = [a.strip() for a in args.assets.split(",")] if args.assets else None
        draft = draft_script(db, args.task_id, args.route, assets=assets)
        if args.json:
            _print_json(draft)
        else:
            print(f"草稿 {draft['draft_id']}（{draft['source']}）状态={draft['status']}")
            print("脚本：", draft["script"])
            print("要点：", draft["outline"])
            print("→ 需人工 confirm 才生效（脚本回退门禁）")
        return 0
    if args.sc_command == "confirm":
        d = confirm_draft(db, args.draft_id)
        if args.json:
            _print_json(d)
        else:
            print(f"草稿 {d['draft_id']} 已确认生效（status={d['status']}）")
        return 0
    if args.sc_command == "reject":
        d = reject_draft(db, args.draft_id)
        if args.json:
            _print_json(d)
        else:
            print(f"草稿 {d['draft_id']} 已驳回（status={d['status']}，可改手写）")
        return 0
    if args.sc_command == "show":
        d = get_draft(db, args.draft_id)
        if d is None:
            print(f"草稿 {args.draft_id} 不存在")
            return 1
        if args.json:
            _print_json(d)
        else:
            print(f"草稿 {d['draft_id']} | {d['route']} | {d['source']} | {d['status']}")
            print("脚本：", d["script"])
            print("要点：", d["outline"])
        return 0
    if args.sc_command == "list":
        for d in list_drafts(db, args.task_id):
            print(f"{d['draft_id']}  {d['task_id']}  {d['route']}  {d['status']}")
        return 0
    return 1


def _assemble(db: CatalogDatabase, args: argparse.Namespace) -> int:
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    result = assemble(
        db, args.task_id, video_path=args.video, platform=args.platform,
        title=args.title, description=args.desc, tags=tags,
        render=args.render, ass_path=args.ass, output_path=args.output,
        work_dir=PROJECT_ROOT,
        subtitle_sources=(
            {
                "script_path": args.script,
                "words_path": args.words,
                "semantics_path": args.semantics,
            }
            if args.script is not None else None
        ),
    )
    if args.json:
        _print_json(result)
    else:
        print(f"端到端编排：任务 {args.task_id}")
        for k, v in result["gates"].items():
            print(f"  - {'✓' if v else '✗'} {k}")
        print(f"结论：{result['message']}")
        if result.get("publish"):
            print(f"  发布包：{result['publish']['record_id']} [{result['publish']['status']}]")
    if result["status"] in ("awaiting_human_review", "imported_video_qa"):
        return 0 if result.get("qa", {}).get("status") == "passed" else 1
    return 0 if result["go"] else 1


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _print_error(info: ErrorInfo, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {"ok": False, "error": info.as_dict()},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
    else:
        print(f"错误：{info.user_message}", file=sys.stderr)
