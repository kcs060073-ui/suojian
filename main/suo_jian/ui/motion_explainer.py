"""Local, experimental Remotion explainer; existing 剪辑工作台 exports stay independent."""
import json
import math
import re
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, render_template, request, send_from_directory
from .motion_music import options as music_options, prepare_music, LIBRARY, NAMES
from .hypit_bridge import compose as hypit_compose

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"
CONFIG = ROOT / "templates" / "motion-engine.json"
bp = Blueprint("motion_explainer", __name__)
LOCK = threading.Lock()
TEXT_DEFAULTS = {
    "title": "厂房选址，先看什么？",
    "park": "嘉兴平湖 · 新仓智谷天地",
    "identity": "国资 · 可租可售 · 无二房东",
    "height": "11–12", "load": "5", "crane": "10",
    "areaMin": "2200", "areaMax": "12000", "rent": "3–6",
    "ending": "带着生产需求，现场核对空间",
}


def engine():
    return Path(json.loads(CONFIG.read_text(encoding="utf-8"))["engine"]).resolve()


def folder(job):
    if not re.fullmatch(r"[0-9a-f]{8}", job):
        raise ValueError("无效任务编号")
    path = RESULTS / job / "motion_explainer"
    if not (engine() / "public" / job / "provenance.json").exists():
        raise FileNotFoundError("这个任务尚未准备动画素材；首轮请使用嘉兴项目示例。")
    return path


def validate_props(value):
    if not isinstance(value, dict) or set(value) != set(TEXT_DEFAULTS):
        raise ValueError("请提交完整的画面文字参数")
    for key, text in value.items():
        limit = 26 if key in {"title", "park", "identity", "ending"} else 9
        if not isinstance(text, str) or not text.strip() or len(text) > limit or any(ord(c) < 32 for c in text):
            raise ValueError("画面文字为空或过长，请缩短后再试")
    return {key: text.strip() for key, text in value.items()}


def local_mutation():
    origin = request.headers.get("Origin")
    return (request.host.split(":")[0] in {"localhost", "127.0.0.1"}
            and request.headers.get("Sec-Fetch-Site") != "cross-site"
            and (not origin or urlsplit(origin).netloc == request.host))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_music(path):
    return json.loads((path / "music.json").read_text(encoding="utf-8")) if (path / "music.json").exists() else {"visualStyle": "green"}


@bp.get("/motion")
def page():
    return render_template("motion_explainer.html")


@bp.get("/api/motion/<job>/project")
def project(job):
    try:
        path = folder(job)
        props = json.loads((path / "project.json").read_text(encoding="utf-8")) if (path / "project.json").exists() else TEXT_DEFAULTS
        state = json.loads((path / "state.json").read_text(encoding="utf-8")) if (path / "state.json").exists() else {"status": "idle"}
        if state.get("status") == "rendering" and not LOCK.locked():
            state = {"status": "interrupted", "error": "服务重启中断了导出，请重新生成。"}
        return jsonify(props=props, state=state, music=read_music(path), musicOptions=music_options(),
                       hypit={"installed": Path(json.loads(CONFIG.read_text(encoding="utf-8")).get("hypit_cli", "missing")).is_file(), "version": "0.3.1"},
                       assetBase=f"/api/motion/{job}/assets", durationInFrames=1067)
    except (ValueError, FileNotFoundError) as error:
        return jsonify(error=str(error)), 404


@bp.post("/api/motion/<job>/save")
def save(job):
    if not local_mutation():
        return jsonify(error="请从本机工作台操作"), 403
    try:
        path = folder(job)
        props = validate_props(request.get_json(silent=True))
        if not LOCK.acquire(blocking=False):
            return jsonify(error="正在导出，请稍后保存"), 409
        try:
            write_json(path / "project.json", props)
        finally:
            LOCK.release()
        return jsonify(status="saved")
    except (ValueError, FileNotFoundError) as error:
        return jsonify(error=str(error)), 400


def render_worker(job, path, run, props, music=None):
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        use_hypit = bool(config.get("hypit_cli"))
        music = music or {}
        def phase(message, **extra):
            write_json(path / "state.json", {"status": "rendering", "run": run, "phase": message, **extra})
        run_props = path / f"{run}.json"
        write_json(run_props, {**props, "job": job, "music": music, "silent": use_hypit})
        executable = engine() / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
        if not executable.exists():
            raise RuntimeError("动画引擎尚未安装完成")
        with (path / f"{run}.log").open("w", encoding="utf-8") as log:
            phase("正在生成跟随 BGM 节拍的动画画面")
            visual = path / f"{run}-visual.mp4" if use_hypit else path / f"{run}.mp4"
            subprocess.run(["node", str(executable), "render", "src/index.ts", "ParkExplainer",
                            str(visual), "--props", str(run_props), "--codec=h264",
                            "--concurrency=2", "--crf=20", "--log=error"],
                           cwd=engine(), stdout=log, stderr=log, check=True, timeout=1200)
        hypit_report = None
        if use_hypit:
            public = engine() / "public" / job
            music_asset = music.get("asset")
            if music_asset and not re.fullmatch(r"music-[0-9a-f]{12}\.m4a", music_asset):
                raise ValueError("无效音乐素材")
            hypit_report = hypit_compose(config, run, visual, public / "narration.mp3",
                public / music_asset if music_asset else None, path / f"{run}.mp4", phase,
                music_gain=music.get("gain", 1))
        probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type,width,height", "-of", "json", str(path / f"{run}.mp4")]))
        streams = probe.get("streams", [])
        if not any(s.get("codec_type") == "video" and s.get("width") == 1280 and s.get("height") == 720 for s in streams) or not any(s.get("codec_type") == "audio" for s in streams) or abs(float(probe["format"]["duration"]) - 1067 / 30) > .2:
            raise RuntimeError("输出文件验证未通过")
        write_json(path / "state.json", {"status": "completed", "run": run,
                    "url": f"/api/motion/{job}/files/{run}.mp4", "validation": probe, "props": props,
                    "music": music, "hypit": hypit_report})
    except Exception as error:
        (path / f"{run}-error.log").write_text(str(error), encoding="utf-8")
        write_json(path / "state.json", {"status": "failed", "run": run,
                    "error": "动画导出失败，旧成片已保留。可查看本机导出日志后重试。"})
    finally:
        LOCK.release()


@bp.post("/api/motion/<job>/render")
def render(job):
    if not local_mutation():
        return jsonify(error="请从本机工作台操作"), 403
    try:
        path = folder(job)
        props = validate_props(request.get_json(silent=True))
    except (ValueError, FileNotFoundError) as error:
        return jsonify(error=str(error)), 400
    if not LOCK.acquire(blocking=False):
        return jsonify(error="已有动画正在导出，请稍候"), 409
    run = "run-" + uuid.uuid4().hex[:12]
    try:
        write_json(path / "project.json", props)
        write_json(path / "state.json", {"status": "rendering", "run": run})
        threading.Thread(target=render_worker, args=(job, path, run, props, read_music(path)), daemon=True).start()
    except Exception:
        LOCK.release()
        raise
    return jsonify(status="rendering", run=run)


@bp.get("/api/motion/<job>/assets/<name>")
def asset(job, name):
    try:
        folder(job)
        if name not in {"park-bed.mp4", "park-bed-v2.mp4", "narration.mp3", "captions.json", "provenance.json"} and not re.fullmatch(r"music-[0-9a-f]{12}\.m4a", name):
            return jsonify(error="素材不存在"), 404
        return send_from_directory(engine() / "public" / job, name)
    except (ValueError, FileNotFoundError) as error:
        return jsonify(error=str(error)), 404


@bp.post("/api/motion/<job>/music")
def music(job):
    if not local_mutation():
        return jsonify(error="请从本机工作台操作"), 403
    if request.content_length and request.content_length > 101 * 1024 * 1024:
        return jsonify(error="BGM 最多 100 MB"), 400
    if not LOCK.acquire(blocking=False):
        return jsonify(error="正在处理视频或音乐，请稍候"), 409
    try:
        path = folder(job)
        uploaded = request.files.get("bgm")
        if uploaded:
            if Path(uploaded.filename or "").suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
                raise ValueError("请选择 MP3、WAV、M4A、AAC、OGG 或 FLAC 音频")
            source = path / "uploaded-music" / (uuid.uuid4().hex + Path(uploaded.filename).suffix.lower())
            source.parent.mkdir(parents=True, exist_ok=True)
            uploaded.save(source)
            identity, name, offset = "upload", Path(uploaded.filename).name[:80], 0
        else:
            data = request.get_json(silent=True) or {}
            identity = data.get("id")
            if identity not in NAMES:
                raise ValueError("请选择 剪辑工作台 音乐或上传 BGM")
            source, name, offset = LIBRARY / (identity + ".mp3"), NAMES[identity], 164 if identity == "machina" else 0
        value = prepare_music(source, name, path / "music-work", engine() / "public" / job, offset, identity)
        value["visualStyle"] = read_music(path).get("visualStyle", "gold")
        value["gain"] = read_music(path).get("gain", 1.8)
        write_json(path / "music.json", value)
        return jsonify(music=value)
    except (ValueError, FileNotFoundError, subprocess.SubprocessError) as error:
        return jsonify(error=str(error) if isinstance(error, (ValueError, FileNotFoundError)) else "音乐处理未成功，请检查音频后重试"), 400
    finally:
        LOCK.release()


@bp.post("/api/motion/<job>/look")
def look(job):
    if not local_mutation():
        return jsonify(error="请从本机工作台操作"), 403
    data = request.get_json(silent=True) or {}
    if data.get("visualStyle") not in {"gold", "green", "story"}:
        return jsonify(error="请选择已有画面风格"), 400
    if not LOCK.acquire(blocking=False):
        return jsonify(error="正在处理，请稍后更换风格"), 409
    try:
        path = folder(job)
        value = read_music(path)
        value["visualStyle"] = data["visualStyle"]
        write_json(path / "music.json", value)
        return jsonify(music=value)
    except (ValueError, FileNotFoundError) as error:
        return jsonify(error=str(error)), 400
    finally:
        LOCK.release()


@bp.post("/api/motion/<job>/mix")
def mix(job):
    if not local_mutation():
        return jsonify(error="请从本机工作台操作"), 403
    gain = (request.get_json(silent=True) or {}).get("gain")
    if type(gain) not in {int, float} or not math.isfinite(gain) or not 0 <= gain <= 2:
        return jsonify(error="音乐强度应在 0% 到 200% 之间"), 400
    if not LOCK.acquire(blocking=False):
        return jsonify(error="正在处理，请稍后调整音量"), 409
    try:
        path = folder(job)
        value = read_music(path)
        if not value.get("asset"):
            return jsonify(error="请先选择音乐"), 400
        value["gain"] = gain
        write_json(path / "music.json", value)
        return jsonify(music=value)
    except (ValueError, FileNotFoundError) as error:
        return jsonify(error=str(error)), 400
    finally:
        LOCK.release()


@bp.get("/api/motion/<job>/files/<name>")
def output(job, name):
    try:
        if not re.fullmatch(r"run-[0-9a-f]{12}\.mp4", name):
            return jsonify(error="文件不存在"), 404
        return send_from_directory(folder(job), name)
    except (ValueError, FileNotFoundError) as error:
        return jsonify(error=str(error)), 404
