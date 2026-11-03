"""One native dialog at a time; competing requests never queue dialogs."""
from __future__ import annotations

from contextvars import ContextVar
import logging
import threading
import time

from .security import HttpError

picker_request_id = ContextVar("picker_request_id", default="untracked")
_log = logging.getLogger(__name__)


class NativePicker:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _pick(self, kind: str) -> str | None:
        started = time.monotonic()
        def mark(event):
            _log.info("picker request=%s kind=%s event=%s elapsed_ms=%.3f",
                      picker_request_id.get(), kind, event,
                      (time.monotonic() - started) * 1000)
        if not self._lock.acquire(blocking=False):
            mark("busy")
            raise HttpError(409, "PICKER_BUSY", "已有文件选择窗口打开，请先完成或取消该窗口。")
        root = None
        try:
            mark("lock_acquired")
            from tkinter import Tk, filedialog
            root = Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            mark("tk_ready")
            # Dialog entry is not evidence of when Windows made it visible.
            mark("dialog_entered")
            if kind == "packaging":
                value = filedialog.askopenfilename(parent=root, title="选择包装素材文件", filetypes=[("音频与图片", "*.mp3 *.wav *.ogg *.m4a *.aac *.flac *.opus *.png *.jpg *.jpeg *.webp *.gif")])
            elif kind == "directory":
                value = filedialog.askdirectory(parent=root, title="选择素材文件夹", mustexist=True)
            else:
                title, types = (
                    ("选择本次数字人口播视频", [("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v")])
                    if kind == "video" else
                    ("选择发布封面图片", [("封面图片", "*.jpg *.jpeg *.png *.webp")])
                )
                value = filedialog.askopenfilename(parent=root, title=title,
                                                  filetypes=[*types, ("所有文件", "*.*")])
            mark("selected" if value else "cancelled")
            return value or None
        finally:
            try:
                if root is not None:
                    root.destroy()
            finally:
                self._lock.release()
                mark("released")

    def pick_packaging(self) -> str | None:
        return self._pick("packaging")

    def pick_video(self) -> str | None:
        return self._pick("video")

    def pick_directory(self) -> str | None:
        return self._pick("directory")

    def pick_image(self) -> str | None:
        return self._pick("image")
