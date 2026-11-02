"""Small native Windows file/directory picker with no persistent UI state."""

from __future__ import annotations

import threading


class NativePicker:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def pick_video(self) -> str | None:
        from tkinter import Tk, filedialog

        with self._lock:
            root = Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                value = filedialog.askopenfilename(
                    parent=root,
                    title="选择本次数字人口播视频",
                    filetypes=[
                        ("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"),
                        ("所有文件", "*.*"),
                    ],
                )
                return value or None
            finally:
                root.destroy()

    def pick_directory(self) -> str | None:
        from tkinter import Tk, filedialog

        with self._lock:
            root = Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                value = filedialog.askdirectory(
                    parent=root,
                    title="选择素材文件夹",
                    mustexist=True,
                )
                return value or None
            finally:
                root.destroy()

    def pick_image(self) -> str | None:
        from tkinter import Tk, filedialog

        with self._lock:
            root = Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                value = filedialog.askopenfilename(
                    parent=root,
                    title="选择发布封面图片",
                    filetypes=[
                        ("封面图片", "*.jpg *.jpeg *.png *.webp"),
                        ("所有文件", "*.*"),
                    ],
                )
                return value or None
            finally:
                root.destroy()
