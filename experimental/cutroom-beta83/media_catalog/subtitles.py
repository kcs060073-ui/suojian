"""Immutable task subtitle compilation and provenance validation."""

import hashlib
from .runtime_artifacts import compiled_signature, node_script_command
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import uuid


_SUBTITLE_RUNTIME_FILES = (
    ("subtitle:make_ass", Path("scripts/make-ass.ts")),
    ("subtitle:ass_source", Path("src/subtitle-ass.ts")),
    ("subtitle:alignment_source", Path("src/subtitle-alignment.ts")),
    ("subtitle:compiler", Path("media_catalog/subtitles.py")),
    ("subtitle:speech_alignment", Path("media_catalog/speech_alignment.py")),
    ("subtitle:simplifier", Path("scripts/simplify-speech.cjs")),
    ("subtitle:preset_manifest", Path("assets/manifests/subtitle-presets.json")),
)
_PROXY_KEYS = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
}
_WINDOWS_SAFE_SUBTITLE_PATH_LENGTH = 240


class SubtitleSemanticsError(ValueError):
    """Raised when a subtitle semantics source is missing or malformed."""


def validate_semantics_file(path: Path) -> Path:
    """Validate and resolve the exact two-array subtitle semantics contract."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise SubtitleSemanticsError(f"semantics 文件不存在：{resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SubtitleSemanticsError(f"semantics JSON 无效：{error}") from error
    if not isinstance(payload, dict) or set(payload) != {"keywords", "punchLines"}:
        raise SubtitleSemanticsError(
            "semantics 必须是且只能包含 keywords 与 punchLines 的 JSON object"
        )
    for key in ("keywords", "punchLines"):
        values = payload[key]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise SubtitleSemanticsError(f"semantics.{key} 必须是非空字符串数组")
    return resolved


def _blocked(code: str, message: str) -> None:
    from .render_mix_adapter import RenderMixJobError

    raise RenderMixJobError(code, message)


def file_signature(path: Path) -> dict:
    path = Path(path).resolve()
    compiled = compiled_signature(path)
    if compiled is not None:
        return {"path": str(path), **compiled}
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _required_file(path: Path, label: str) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        _blocked("SUBTITLE_SOURCE_MISSING", f"字幕依赖不存在：{label}/{resolved}")
    return resolved


def _preset(project_root: Path, preset_id: str) -> tuple[dict, Path, Path]:
    manifest_path = _required_file(
        Path(project_root) / "assets" / "manifests" / "subtitle-presets.json",
        "preset manifest",
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _blocked("ASS_PRESET_MISMATCH", f"字幕 preset manifest 无效：{error}")
    preset = next(
        (item for item in manifest.get("items", []) if item.get("id") == preset_id),
        None,
    )
    if not isinstance(preset, dict) or preset.get("license") != "commercial-approved":
        _blocked("ASS_PRESET_MISMATCH", f"已确认字幕 preset 不可用：{preset_id}")
    font_name = preset.get("font")
    if not isinstance(font_name, str) or not font_name:
        _blocked("ASS_PRESET_MISMATCH", f"字幕 preset 缺少字体：{preset_id}")
    font_path = _required_file(Path(project_root) / "assets" / "fonts" / font_name, "font")
    return preset, manifest_path, font_path


def _runtime_signatures(project_root: Path, manifest_path: Path, font_path: Path) -> list[dict]:
    result = []
    for kind, relative in _SUBTITLE_RUNTIME_FILES:
        path = manifest_path if kind == "subtitle:preset_manifest" else _required_file(
            Path(project_root) / relative, kind
        )
        result.append({"kind": kind, **file_signature(path)})
    return result


def _find_ts_node(project_root: Path) -> Path:
    candidates = [Path(project_root) / "node_modules" / "ts-node" / "dist" / "bin.js"]
    root = Path(project_root).resolve()
    if root.parent.name == ".worktrees":
        candidates.append(root.parent.parent / "node_modules" / "ts-node" / "dist" / "bin.js")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    _blocked("SUBTITLE_TOOL_MISSING", "缺少本地 node_modules/ts-node/dist/bin.js")


def _controlled_env() -> dict:
    env = {
        key: value for key, value in os.environ.items()
        if key not in _PROXY_KEYS and not key.upper().startswith("TS_NODE")
    }
    env["LANG"] = "C.UTF-8"
    return env


def _default_runner(command: list[str], **kwargs):
    return subprocess.run(command, **kwargs)


def _load_metadata(path: Path, *, code: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _blocked(code, f"字幕 metadata 无效：{error}")
    if not isinstance(value, dict):
        _blocked(code, "字幕 metadata 必须是 JSON object")
    return value


def _validate_metadata(metadata: dict, preset_id: str, ass_path: Path, *, external: bool) -> None:
    mismatch_code = "ASS_PRESET_MISMATCH" if external else "ASS_CONTENT_MISMATCH"
    if metadata.get("schema") != 1 or metadata.get("preset") != preset_id:
        _blocked(mismatch_code, f"ASS 与已确认 preset 不一致：{preset_id}")
    coverage = metadata.get("coverage")
    if (
        not isinstance(coverage, (int, float))
        or isinstance(coverage, bool)
        or not math.isfinite(coverage)
        or not 0.80 <= coverage <= 1
    ):
        _blocked(
            "ASS_PRESET_MISMATCH",
            "字幕 alignment coverage 必须在 0.80 到 1.00 之间",
        )
    if not isinstance(metadata.get("cues"), list):
        _blocked(mismatch_code, "字幕 metadata 缺少有效 cues")
    if metadata.get("assSha256") != file_signature(ass_path)["sha256"]:
        _blocked(mismatch_code, "ASS 字节与 metadata 哈希不一致")


def _source_signature(metadata: dict, key: str, *, external: bool) -> dict:
    value = metadata.get(key)
    code = "ASS_PRESET_MISMATCH" if external else "ASS_CONTENT_MISMATCH"
    if not isinstance(value, dict) or not value.get("path") or not value.get("sha256"):
        _blocked(code, f"字幕 metadata 缺少 {key} 来源签名")
    path = Path(value["path"])
    if not path.is_file() or file_signature(path)["sha256"] != value["sha256"]:
        _blocked(code, f"字幕 {key} 来源已变化")
    return file_signature(path)


def _binding(
    project_root: Path,
    preset_id: str,
    ass_path: Path,
    metadata_path: Path,
    *,
    script_signature: dict | None = None,
    words_signature: dict | None = None,
    semantics_signature: dict | None = None,
    external: bool,
    frozen_render_font: dict | None = None,
) -> dict:
    if frozen_render_font is None:
        _, manifest_path, font_path = _preset(project_root, preset_id)
    # Delivery keeps the historical font identity without loading a new
    # installation's renderer/font. Packaging file references stay validated.
    metadata = _load_metadata(
        metadata_path,
        code="ASS_PRESET_MISMATCH" if external else "ASS_CONTENT_MISMATCH",
    )
    _validate_metadata(metadata, preset_id, ass_path, external=external)
    script = script_signature or _source_signature(metadata, "script", external=external)
    words = words_signature or _source_signature(metadata, "words", external=external)
    semantics = semantics_signature or _source_signature(
        metadata, "semantics", external=external
    )
    if (
        metadata.get("script") != script
        or metadata.get("words") != words
        or metadata.get("semantics") != semantics
    ):
        _blocked(
            "ASS_PRESET_MISMATCH" if external else "ASS_CONTENT_MISMATCH",
            "字幕来源签名与 metadata 不一致",
        )
    derived = {}
    if metadata.get("alignment_review_digest"):
        derived["alignment_review_digest"] = metadata["alignment_review_digest"]
    if metadata.get("alignment_review_digest") or metadata.get("alignment_words"):
        derived["alignment_words"] = _source_signature(metadata, "alignment_words", external=external)
    return {
        **derived,
        "preset_id": preset_id,
        "alignment_coverage": float(metadata["coverage"]),
        "script": script,
        "words": words,
        "semantics": semantics,
        "ass": file_signature(ass_path),
        "metadata": file_signature(metadata_path),
        "font": file_signature(font_path) if frozen_render_font is None else dict(frozen_render_font),
        "runtime_files": (
            _runtime_signatures(project_root, manifest_path, font_path)
            if frozen_render_font is None else []
        ),
    }


def _input_digest(
    preset_id: str, script: dict, words: dict, semantics: dict,
    runtime_files: list[dict], highlight_enabled: bool = True, alignment_review=None,
) -> str:
    state = {
        "preset_id": preset_id,
        "speed": 1.25,
        "lead": 0.08,
        "script": script,
        "words": words,
        "semantics": semantics,
        "runtime": runtime_files,
    }
    if alignment_review:
        state["alignment_review"] = alignment_review
    if not highlight_enabled:
        state["highlight_enabled"] = False
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _subtitle_job_dir(
    project_root: Path, work_dir: Path, task_id: str, digest: str
) -> Path:
    preferred = (Path(work_dir).resolve() / "runtime" / "jobs" / task_id).resolve()
    longest_name = f".subtitles.{digest}.{'0' * 32}.metadata.tmp"
    if (
        os.name == "nt"
        and len(str(preferred / longest_name)) > _WINDOWS_SAFE_SUBTITLE_PATH_LENGTH
    ):
        return (Path(project_root).resolve() / "runtime" / "subtitles").resolve()
    return preferred


def compile_task_ass(
    project_root: Path,
    work_dir: Path,
    task_id: str,
    preset_id: str,
    script_path: Path,
    words_path: Path,
    semantics_path: Path,
    *,
    runner=None,
    highlight_enabled: bool = True,
    alignment_review: dict | None = None,
) -> dict:
    """Return immutable ASS/metadata paths and verified signatures."""
    if not task_id or task_id in (".", "..") or "/" in task_id or "\\" in task_id:
        _blocked("INVALID_TASK_ID", "任务 ID 不能包含路径分隔符")
    if not isinstance(highlight_enabled, bool):
        _blocked("ASS_HIGHLIGHT_INVALID", "重点词开关必须是布尔值")
    project_root = Path(project_root).resolve()
    script_path = _required_file(script_path, "script")
    words_path = _required_file(words_path, "words")
    try:
        semantics_path = validate_semantics_file(semantics_path)
    except SubtitleSemanticsError as error:
        _blocked("ASS_SOURCE_INVALID", str(error))
    script = file_signature(script_path)
    words = file_signature(words_path)
    semantics = file_signature(semantics_path)
    _, manifest_path, font_path = _preset(project_root, preset_id)
    runtime_files = _runtime_signatures(project_root, manifest_path, font_path)
    digest = _input_digest(
        preset_id,
        script,
        words,
        semantics,
        runtime_files + [{"kind": "subtitle:font", **file_signature(font_path)}],
        highlight_enabled, alignment_review,
    )
    job_dir = _subtitle_job_dir(project_root, work_dir, task_id, digest)
    ass_path = job_dir / f"subtitles.{digest}.ass"
    metadata_path = job_dir / f"subtitles.{digest}.metadata.json"

    if ass_path.is_file() and metadata_path.is_file():
        return _binding(
            project_root, preset_id, ass_path, metadata_path,
            script_signature=script, words_signature=words,
            semantics_signature=semantics, external=False,
        )

    job_dir.mkdir(parents=True, exist_ok=True)
    alignment_words_path = words_path
    review_digest = None
    if alignment_review and alignment_review.get("status") == "reviewed":
        from .speech_alignment import reviewed_words
        review_digest = alignment_review.get("digest")
        original = json.loads(words_path.read_text(encoding="utf-8"))
        corrected = reviewed_words(project_root, script_path.read_text(encoding="utf-8"),
                                   original, alignment_review)
        alignment_words_path = job_dir / f"words.{digest}.json"
        payload = (json.dumps(corrected, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with alignment_words_path.open("xb") as output:
                output.write(payload)
        except FileExistsError:
            if alignment_words_path.read_bytes() != payload:
                _blocked("ASS_CONTENT_MISMATCH", "同版本核对词时内容不一致")
    token = uuid.uuid4().hex
    temp_ass = job_dir / f".subtitles.{digest}.{token}.ass.tmp"
    temp_metadata = job_dir / f".subtitles.{digest}.{token}.metadata.tmp"
    node_value = shutil.which("node")
    if not node_value:
        _blocked("SUBTITLE_TOOL_MISSING", "缺少 node 可执行文件")
    try:
        script_command = node_script_command(Path(node_value).resolve(), project_root, "scripts/make-ass.ts")
    except (OSError, RuntimeError) as error:
        _blocked("SUBTITLE_TOOL_MISSING", str(error))
    if len(script_command) == 3:
        script_command[1] = str(_find_ts_node(project_root))
    command = [
        *script_command,
        "--script", str(script_path),
        "--words", str(alignment_words_path),
        "--semantics", str(semantics_path),
        "--output", str(temp_ass),
        "--metadata", str(temp_metadata),
        "--preset", preset_id,
        "--speed", "1.25",
        "--lead", "0.08",
    ]
    if not highlight_enabled:
        command.extend(("--highlight", "0"))
    process_runner = runner or _default_runner
    try:
        result = process_runner(
            command,
            cwd=str(project_root),
            env=_controlled_env(),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").strip()[-1000:]
            _blocked("ASS_GENERATION_FAILED", detail or "make-ass.ts 执行失败")
        if not temp_ass.is_file() or not temp_metadata.is_file():
            _blocked("ASS_GENERATION_FAILED", "make-ass.ts 未生成 ASS/metadata")
        metadata = _load_metadata(temp_metadata, code="ASS_CONTENT_MISMATCH")
        _validate_metadata(metadata, preset_id, temp_ass, external=False)
        metadata["script"] = script
        metadata["words"] = words
        metadata["semantics"] = semantics
        if review_digest:
            metadata["alignment_review_digest"] = review_digest
            metadata["alignment_words"] = file_signature(alignment_words_path)
        normalized_metadata = (
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        temp_metadata.write_bytes(normalized_metadata)

        for source, target in ((temp_ass, ass_path), (temp_metadata, metadata_path)):
            expected = source.read_bytes()
            try:
                with target.open("xb") as destination:
                    destination.write(expected)
            except FileExistsError:
                if target.read_bytes() != expected:
                    _blocked("ASS_CONTENT_MISMATCH", f"同摘要字幕内容不一致：{target}")
        return _binding(
            project_root, preset_id, ass_path, metadata_path,
            script_signature=script, words_signature=words,
            semantics_signature=semantics, external=False,
        )
    finally:
        temp_ass.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)


def bind_external_ass(project_root: Path, preset_id: str, ass_path: Path) -> dict:
    ass_path = Path(ass_path).resolve()
    if not ass_path.is_file():
        _blocked("ASS_FILE_MISSING", f"字幕文件不存在：{ass_path}")
    metadata_path = ass_path.with_suffix(".metadata.json")
    if not metadata_path.is_file():
        _blocked("ASS_PRESET_MISMATCH", f"外部 ASS 缺少同名 metadata sidecar：{metadata_path}")
    return _binding(project_root, preset_id, ass_path, metadata_path, external=True)


def verify_subtitle_binding(project_root: Path, stored: dict, *, delivery_only: bool = False) -> dict | None:
    """Rehash a stored binding without invoking make-ass or changing bytes."""
    try:
        preset_id = stored["preset_id"]
        ass_path = Path(stored["ass"]["path"])
        metadata_path = Path(stored["metadata"]["path"])
        current = _binding(
            project_root, preset_id, ass_path, metadata_path, external=True,
            frozen_render_font=stored["font"] if delivery_only else None,
        )
    except (KeyError, TypeError, OSError, ValueError, RuntimeError):
        return None
    comparable = dict(current)
    comparable.pop("runtime_files", None)
    expected = dict(stored)
    expected.pop("runtime_files", None)
    return current if comparable == expected else None
