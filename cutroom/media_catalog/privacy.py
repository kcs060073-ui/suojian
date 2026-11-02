"""隐私标记（P1 隐私红线的一部分）：自动标出"可能要打码"的片段，绝不自动打码。

当前可靠项：转写文本里的手机号 / 微信 / 邮箱（来自 audio_analysis 阶段）。
可选项（依赖 OpenCV / pyzbar）：人脸、二维码。屏幕、工牌暂不做自动识别，
留给人工在预览里确认。

所有命中只写 privacy_flags 表，needs_human_confirm=1，等你在候选确认环节拍板
遮不遮。这是"提醒 + 留人工确认"，不是替你做决定。
"""

from collections.abc import Callable
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile

from .path_utils import filesystem_path
from .audio_analysis import CONTACT_PATTERN
from .preview import _asset_source, _duration_of

FACE_KIND = "face"
QR_KIND = "qrcode"
CONTACT_KIND = "contact"
SAMPLE_FRAMES = 8


def _audio_transcript(db, asset_id: str) -> str | None:
    row = db.connection.execute(
        """
        SELECT result_json FROM analysis_runs
        WHERE asset_id=? AND stage='audio_analysis' AND status='completed'
        """,
        (asset_id,),
    ).fetchone()
    if row is None or not row["result_json"]:
        return None
    import json

    try:
        data = json.loads(row["result_json"])
    except json.JSONDecodeError:
        return None
    segments = data.get("segments") or []
    return " ".join(s.get("text", "") for s in segments)


def _contact_flags(db, asset_id: str) -> list[dict]:
    text = _audio_transcript(db, asset_id)
    if not text:
        return []
    flags = []
    for match in CONTACT_PATTERN.finditer(text):
        kind = next(k for k, v in match.groupdict().items() if v)
        flags.append(
            {
                "flag_type": CONTACT_KIND,
                "confidence": 0.9,
                "notes": f"转写命中{kind}：{match.group(0)}",
            }
        )
    return flags


def _sample_frames(ffmpeg: Path, source: Path, duration: float, runner) -> list[tuple[float, Path]]:
    if not duration or duration < 1:
        return []
    fps = SAMPLE_FRAMES / duration
    work = Path(tempfile.mkdtemp(prefix="priv_"))
    completed = runner(
        [
            str(ffmpeg),
            "-y",
            "-ss",
            "0",
            "-i",
            str(source),
            "-vf",
            f"fps={fps:.6f}",
            "-frames:v",
            str(SAMPLE_FRAMES),
            str(work / "f_%03d.jpg"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    frames = []
    files = sorted(work.glob("f_*.jpg"))
    for i, f in enumerate(files):
        t = (i + 0.5) * (duration / max(1, len(files)))
        frames.append((t, f))
    return frames


def _visual_flags(ffmpeg: Path, source: Path, duration: float, runner) -> list[dict]:
    flags: list[dict] = []
    try:
        import cv2
    except ImportError:
        return flags  # 未装 OpenCV，跳过人脸/二维码自动检测
    frames = _sample_frames(ffmpeg, source, duration, runner)
    if not frames:
        return flags
    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    qr_available = True
    try:
        from pyzbar.pyzbar import decode as qr_decode
    except ImportError:
        qr_available = False
    for t, fpath in frames:
        image = cv2.imread(str(fpath))
        if image is None:
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.3, 4)
        if len(faces) > 0:
            flags.append(
                {
                    "flag_type": FACE_KIND,
                    "start_sec": round(t, 2),
                    "end_sec": round(t + 0.5, 2),
                    "confidence": 0.7,
                    "notes": f"第 {len(faces)} 张人脸（约 {t:.1f}s）",
                }
            )
        if qr_available:
            try:
                for _ in qr_decode(image):
                    flags.append(
                        {
                            "flag_type": QR_KIND,
                            "start_sec": round(t, 2),
                            "end_sec": round(t + 0.5, 2),
                            "confidence": 0.8,
                            "notes": f"可能含二维码（约 {t:.1f}s）",
                        }
                    )
                    break
            except Exception:
                pass
    import shutil

    shutil.rmtree(Path(fpath).parent, ignore_errors=True)
    return flags


def scan_privacy(
    db, asset_id: str, ffmpeg: Path, runner: Callable[..., object] = subprocess.run
) -> dict:
    info = _asset_source(db, asset_id)
    if info is None:
        raise RuntimeError(f"素材当前不可访问：{asset_id}")
    mount_path, relative_path, media_kind = info
    flags: list[dict] = []
    flags.extend(_contact_flags(db, asset_id))
    if media_kind == "video":
        source = filesystem_path(Path(mount_path) / relative_path)
        duration = _duration_of(db, asset_id)
        flags.extend(_visual_flags(ffmpeg, source, duration or 0.0, runner))
    _persist(db, asset_id, flags)
    return {"asset_id": asset_id, "flags_created": len(flags), "types": _types(flags)}


def _types(flags: list[dict]) -> list[str]:
    seen = []
    for f in flags:
        if f["flag_type"] not in seen:
            seen.append(f["flag_type"])
    return seen


def _persist(db, asset_id: str, flags: list[dict]) -> None:
    with db.transaction() as connection:
        def identity(flag):
            return tuple(flag.get(k) for k in ('flag_type', 'start_sec', 'end_sec', 'notes'))
        existing = {}
        for row in connection.execute('SELECT * FROM privacy_flags WHERE asset_id=?', (asset_id,)):
            item = dict(row)
            key = identity(item)
            previous = existing.get(key)
            # A merge can retain several rows for the same hit. A completed
            # human decision (including "no masking") wins over an unreviewed row.
            if previous is None or (previous['needs_human_confirm'] and not item['needs_human_confirm']):
                existing[key] = item
        retained = set()
        for f in flags:
            key = identity(f)
            previous = existing.get(key)
            flag_id = previous['flag_id'] if previous else asset_id + ':' + hashlib.sha256(
                json.dumps(key, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]
            retained.add(flag_id)
            connection.execute(
                """
                INSERT INTO privacy_flags(
                  flag_id, asset_id, flag_type, start_sec, end_sec,
                  confidence, needs_human_confirm, confirmed, notes
                ) VALUES(?, ?, ?, ?, ?, ?, 1, 0, ?)
                ON CONFLICT(flag_id) DO UPDATE SET confidence=excluded.confidence
                """,
                (
                    flag_id,
                    asset_id,
                    f["flag_type"],
                    f.get("start_sec"),
                    f.get("end_sec"),
                    f.get("confidence"),
                    f.get("notes"),
                ),
            )
        for old in connection.execute('SELECT flag_id FROM privacy_flags WHERE asset_id=?', (asset_id,)).fetchall():
            if old['flag_id'] not in retained:
                connection.execute('DELETE FROM privacy_flags WHERE flag_id=?', (old['flag_id'],))


def confirm_flag(db, flag_id: str, confirmed: bool) -> None:
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE privacy_flags
            SET confirmed=?, needs_human_confirm=0
            WHERE flag_id=?
            """,
            (int(confirmed), flag_id),
        )


def list_flags(db, asset_id: str | None = None, unconfirmed_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM privacy_flags"
    clauses = []
    params: list = []
    if asset_id is not None:
        clauses.append("asset_id=?")
        params.append(asset_id)
    if unconfirmed_only:
        clauses.append("needs_human_confirm=1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY asset_id, start_sec"
    return [dict(r) for r in db.connection.execute(sql, params).fetchall()]
