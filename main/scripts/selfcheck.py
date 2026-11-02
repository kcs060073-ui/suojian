from __future__ import annotations

import importlib.util
import json
import shutil
import sys

MODULES = {
    "Flask workbench": "flask",
    "OpenCV motion analysis": "cv2",
    "PySceneDetect shot detection": "scenedetect",
    "librosa beat analysis": "librosa",
    "Whisper subtitles": "whisper",
    "pyJianYingDraft export": "pyJianYingDraft",
}

def main() -> int:
    checks = {name: bool(importlib.util.find_spec(module)) for name, module in MODULES.items()}
    checks["FFmpeg"] = bool(shutil.which("ffmpeg"))
    checks["FFprobe"] = bool(shutil.which("ffprobe"))
    result = {"python": sys.version.split()[0], "ready": all(checks.values()), "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
