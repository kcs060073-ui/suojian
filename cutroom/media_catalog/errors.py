"""Stable errors shared by Codex, WorkBuddy, and human CLI output."""

from dataclasses import asdict, dataclass
import sqlite3

from .jobs import HeavyJobBusy
from .scanner import LibraryOffline
from .tools import ToolNotFound


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    retryable: bool
    user_message: str
    next_action: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def classify_error(error: BaseException) -> ErrorInfo:
    structured_code = getattr(error, "code", None)
    if structured_code == "DIGITAL_TASK_PREFLIGHT_FAILED":
        return ErrorInfo(
            structured_code,
            True,
            str(error),
            "修正创建前预检中的阻断项后重新预检。",
        )
    if isinstance(structured_code, str) and structured_code:
        return ErrorInfo(
            structured_code,
            structured_code in {"RENDER_TIMEOUT", "RENDER_PROCESS_FAILED"},
            str(error),
            "按错误代码修复门禁、路径或渲染环境后重试。",
        )
    if isinstance(error, LibraryOffline):
        return ErrorInfo(
            "LIBRARY_OFFLINE",
            True,
            str(error),
            "连接本机硬盘或移动硬盘后告诉我继续。",
        )
    if isinstance(error, HeavyJobBusy):
        return ErrorInfo(
            "HEAVY_JOB_BUSY",
            True,
            str(error),
            "等待当前任务完成后继续。",
        )
    if isinstance(error, ToolNotFound):
        return ErrorInfo(
            "TOOL_NOT_FOUND",
            True,
            str(error),
            "安装或配置 ffprobe 后继续；快速盘点结果仍保留。",
        )
    if isinstance(error, sqlite3.Error):
        return ErrorInfo(
            "DATABASE_ERROR",
            False,
            "素材目录数据库无法安全读写。",
            "停止任务并检查数据库完整性或从备份恢复。",
        )
    if isinstance(error, FileNotFoundError):
        return ErrorInfo(
            "PATH_NOT_FOUND",
            True,
            str(error),
            "确认目录或文件位置后重试。",
        )
    if isinstance(error, OSError):
        return ErrorInfo(
            "FILESYSTEM_ERROR",
            True,
            str(error),
            "检查磁盘连接、权限和剩余空间后重试。",
        )
    if isinstance(error, (KeyError, ValueError)):
        return ErrorInfo(
            "INVALID_REQUEST",
            False,
            str(error).strip("'"),
            "修正素材库名称或参数后重试。",
        )
    return ErrorInfo(
        "OPERATION_FAILED",
        False,
        str(error),
        "查看错误摘要并由维护人员处理。",
    )
