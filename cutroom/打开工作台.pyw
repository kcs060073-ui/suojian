# -*- coding: utf-8 -*-
"""打开 CUTROOM 工作台（双击桌面快捷方式时运行）。

流程：
  1. 确保服务在 127.0.0.1:8765 运行 —— 通过 daemon 幂等启动，已在跑则跳过
  2. 从 runtime/_run.log 读取当前进程有效的 bootstrap 凭据
  3. 用默认浏览器打开

设计要点：
  - 用 .pyw 后缀 + pythonw.exe 运行，全程无控制台黑窗
  - 凭据从日志按字节正则提取，不受日志编码（GBK/UTF-8 混杂）影响
  - daemon 不可用时退化为直接打开站点根路径
"""
import pathlib
import re
import socket
import subprocess
import sys
import time
import webbrowser

HOST = "127.0.0.1"
PORT = 8765
ROOT = pathlib.Path(__file__).resolve().parent
LOG = ROOT / "runtime" / "_run.log"
DAEMON = pathlib.Path(r"C:\Users\Tanikawa\tools\cutroom_daemon.py")

URL_RE = re.compile(
    rb"http://127\.0\.0\.1:\d+/auth/bootstrap\?key=[A-Za-z0-9_\-]+"
)
WAIT_SECONDS = 40


def port_is_up(timeout: float = 1.0) -> bool:
    probe = socket.socket()
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((HOST, PORT)) == 0
    finally:
        probe.close()


def ensure_service() -> None:
    """幂等确保服务在跑；daemon 不可用则静默跳过。"""
    if port_is_up():
        return
    if not DAEMON.exists():
        return
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    launcher = str(python) if python.exists() else sys.executable
    try:
        subprocess.run(
            [launcher, str(DAEMON), "--port", str(PORT)],
            cwd=str(ROOT),
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if port_is_up():
            return
        time.sleep(0.5)


def bootstrap_url() -> str:
    """读取当前有效凭据；读不到就退化为站点根路径。"""
    try:
        raw = LOG.read_bytes()
    except OSError:
        return f"http://{HOST}:{PORT}/"
    match = URL_RE.search(raw)
    return match.group(0).decode("ascii") if match else f"http://{HOST}:{PORT}/"


def main() -> int:
    ensure_service()
    webbrowser.open(bootstrap_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
