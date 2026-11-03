"""Read-only, task-scoped media preflight; native Jianying evidence stays separate."""
import copy
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess

from .image_probe import probe_image
from .jianying_export import verify_jianying_assets
from .probe import probe_media
from .provenance import canonical_digest
from .rough_media import decode_video_frame
from .timeline_contract import TimeWindow, seconds_to_us, validate_source_window

DECODE_POLICY_VERSION = "ffmpeg-strict-samples-v1"


class MediaError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class AtrMediaRunner:
    """Adapt the installed synchronous runtime to existing subprocess-style APIs.

    Latin-1 transports bounded binary samples reversibly inside ATR's JSON.
    The runtime owns the process tree, time limit and bounded pipe capture.
    """
    def __init__(self, cwd, atr_path=None):
        self.cwd = str(Path(cwd).resolve())
        self.path = Path(atr_path) if atr_path else Path(os.environ.get("LOCALAPPDATA", "")) / "PeterAgentRuntime/bin/atr.exe"
        if not self.path.is_absolute() or not self.path.is_file():
            raise MediaError("JY_MEDIA_RUNTIME_MISSING", "需要已安装的ATR受控执行入口")
        self.executions = []

    def __call__(self, command, **options):
        command = list(map(str, command))
        binary = not options.get("text", False)
        if binary and "image2pipe" in command:
            command = command[:-1] + ["-vf", "scale=w='min(640,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease,format=gray"] + command[-1:]
        if binary:
            command = [command[0], "-nostdin", "-threads", "1", *command[1:]]
        arguments = [str(self.path), "run-process", "--exe", command[0]]
        for argument in command[1:]:
            arguments.extend(["--arg", argument])
        arguments.extend(["--cwd", self.cwd, "--timeout-ms", "15000", "--wait-ms", "0",
                          "--max-output-bytes", "524288" if binary else "131072",
                          "--stdout-encoding", "iso-8859-1" if binary else "utf-8",
                          "--caller-agent", "codex", "--json"])
        completed = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=25)
        try:
            envelope = json.loads(completed.stdout)
        except (ValueError, TypeError) as error:
            raise MediaError("JY_MEDIA_RUNTIME_FAILED", "ATR未返回有效终态") from error
        result = envelope.get("result") or {}
        self.executions.append({"command": command, "status": envelope.get("status"),
                                "operation_id": envelope.get("operation_id"),
                                "trace_path": envelope.get("trace_path"),
                                "exit_code": result.get("exit_code"),
                                "stdout_truncated": result.get("stdout_truncated"),
                                "stderr_truncated": result.get("stderr_truncated")})
        if result.get("stdout_truncated") or result.get("stderr_truncated"):
            raise MediaError("JY_MEDIA_OUTPUT_LIMIT", "媒体探测输出达到上限")
        if envelope.get("status") != "success" or completed.returncode != 0:
            code = (envelope.get("error") or {}).get("code", "unknown")
            failure = "JY_MEDIA_TIMEOUT" if code == "process.timeout" else "JY_MEDIA_PROCESS_FAILED"
            raise MediaError(failure, f"受控媒体进程失败：{code}")
        stdout, stderr = result.get("stdout", ""), result.get("stderr", "")
        if binary:
            stdout, stderr = stdout.encode("latin-1"), stderr.encode("latin-1")
        return subprocess.CompletedProcess(command, result.get("exit_code", 1), stdout, stderr)


def _duration(stream):
    measured = None
    ticks, base = stream.get("duration_ts"), stream.get("time_base")
    if type(ticks) is int and ticks > 0 and isinstance(base, str):
        try:
            fraction = Fraction(base)
            if fraction > 0:
                measured = ticks * fraction * 1_000_000
        except (ValueError, ZeroDivisionError):
            pass
    display = None
    try:
        display = seconds_to_us(stream.get("duration"))
        if display <= 0:
            display = None
    except ValueError:
        pass
    if measured is None and display is None:
        raise MediaError("JY_MEDIA_DURATION_UNKNOWN", "所选媒体流实际时长未知")
    if measured is not None and display is not None and abs(measured - display) > 1:
        raise MediaError("JY_MEDIA_DURATION_CONFLICT", "所选媒体流时长依据冲突")
    duration = measured.numerator // measured.denominator if measured is not None else display
    if duration <= 0:
        raise MediaError("JY_MEDIA_DURATION_UNKNOWN", "所选媒体流时长不足一微秒")
    return duration, "duration_ts*time_base" if measured is not None else "stream.duration"


def _origin(stream):
    value = stream.get("start_time")
    if value is not None:
        try:
            origin = Decimal(str(value))
            if not origin.is_finite() or origin != 0:
                raise MediaError("JY_MEDIA_TIME_ORIGIN_UNSUPPORTED", "所选流起始偏移尚未支持")
        except InvalidOperation as error:
            raise MediaError("JY_MEDIA_TIME_ORIGIN_UNSUPPORTED", "所选流起始偏移无法识别") from error
    if "start_pts" in stream and stream["start_pts"] not in (None, 0):
        raise MediaError("JY_MEDIA_TIME_ORIGIN_UNSUPPORTED", "所选流非零起始PTS尚未支持")


def _roles(recipe):
    for index, shot in enumerate(recipe["shots"]):
        yield {"role": f"shot:{index}", "src": shot["src"],
               "kind": "image" if shot["media_kind"] == "image" else "video",
               "start": shot.get("source_start_us"), "end": shot.get("source_end_us")}
        alternate = (shot.get("render_value") or {}).get("src")
        if alternate and str(Path(alternate).resolve()) != str(Path(shot["src"]).resolve()):
            yield {"role": f"shot-render:{index}", "src": alternate, "kind": "unsupported"}
        value = shot.get("render_value") or {}
        if value.get("audio") in {"keep", "duck", "clean"}:
            # Match render-full-audio.ts, including its optional absent-track skip.
            start = seconds_to_us(value.get("sourceStart", 0))
            duration = seconds_to_us(value["end"]) - seconds_to_us(value["start"])
            if start < 0 or duration <= 0:
                raise MediaError("JY_MEDIA_ROLE_INVALID", "同期声取用区间无效")
            yield {"role": f"shot-audio:{index}", "src": value.get("src", shot["src"]),
                   "kind": "audio", "start": start, "end": start + duration, "optional_audio": True}
    audio = recipe["main_audio"]
    yield {"role": "main_audio", "src": audio["src"], "kind": "audio",
           "start": audio["source_start_us"], "end": audio["source_end_us"]}
    if (recipe.get("task_snapshot") or {}).get("task_type") == "digital_human":
        yield {"role": "presenter_base", "src": audio["src"], "kind": "video",
               "start": audio["source_start_us"], "end": audio["source_end_us"]}
    for index, layer in enumerate(recipe.get("packaging_instances", [])):
        value = layer.get("render_value") or {}
        if not value.get("src"):
            continue
        if layer.get("kind") == "bgm":
            yield {"role": f"bgm:{index}", "src": value["src"], "kind": "audio",
                   "start": 0, "end": None, "loop_to_us": layer["end_us"] - layer["start_us"]}
        elif layer.get("kind") == "sfx":
            yield {"role": f"sfx:{index}", "src": value["src"], "kind": "audio",
                   "start": None, "end": None, "sfx_value": value,
                   "available_output_us": recipe["duration_us"] - layer["start_us"]}
        elif Path(value["src"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            yield {"role": f"packaging:{index}", "src": value["src"], "kind": "image"}
        else:
            # No implicit effect trim/duration policy is invented in this batch.
            yield {"role": f"packaging:{index}", "src": value["src"], "kind": "unsupported"}


def _decode(role, source, window, ffmpeg, runner):
    def strict_sample(command, **options):
        # Keep this policy local: shared rough_media callers retain their contract.
        # FFmpeg otherwise recovers from damaged packets and can exit 0 with bytes.
        command = [str(command[0]), "-xerror", "-err_detect", "explode", *command[1:]]
        options["timeout"] = min(options.get("timeout", 15), 15)
        try:
            return runner(command, **options)
        except MediaError as error:
            if error.code == "JY_MEDIA_PROCESS_FAILED":
                raise RuntimeError(f"严格样本解码失败：{error}") from error
            # A runtime/limit/timeout failure is not a media-corruption verdict.
            raise

    if role["kind"] in {"image", "video"}:
        timestamps = [0] if role["kind"] == "image" else sorted({window.start_us, window.end_us - min(50_000, max(1, window.duration_us // 2))})
        for timestamp in timestamps:
            decode_video_frame(source, ffmpeg, timestamp / 1_000_000, runner=strict_sample)
        return [{"at_us": value, "kind": "video_frame"} for value in timestamps]
    duration = min(250_000, window.duration_us)
    timestamps = sorted({window.start_us, window.end_us - duration})
    for timestamp in timestamps:
        completed = strict_sample([str(ffmpeg), "-v", "error", "-i", str(source),
                            "-ss", f"{timestamp / 1_000_000:.6f}", "-t", f"{duration / 1_000_000:.6f}",
                            "-map", "0:a:0", "-ar", "8000", "-ac", "1", "-f", "s16le", "-"],
                           capture_output=True, text=False, timeout=15, check=False)
        if completed.returncode != 0 or not completed.stdout:
            raise RuntimeError("未解码出所选音轨样本")
    return [{"at_us": value, "duration_us": duration, "kind": "audio_sample"} for value in timestamps]


def inspect_jianying_media(recipe, asset_manifest, trusted_roots, *,
                          ffprobe_path, ffmpeg_path, relinks=None, process_runner=None):
    """Inspect exact selected streams and limited decode samples, without writes.

    passed means only the reported windows/samples were checked. No native
    format compatibility or full-file corruption claim follows from this result.
    Custom runners are labeled explicitly and do not earn real-execution evidence.
    """
    report = {"schema": "cutroom.jianying-media/v1", "status": "blocked",
              "decode_policy_version": DECODE_POLICY_VERSION,
              "metadata_verified": False, "decode_samples_verified": False,
              "native_project_verified": False, "approval_transferred": False,
              "execution_origin": "injected_runner" if process_runner is not None else "atr",
              "checks": [], "blockers": [], "warnings": []}

    def block(error, role="preflight"):
        code = getattr(error, "code", None) or ("JY_MEDIA_TIMEOUT" if isinstance(error, subprocess.TimeoutExpired) else
                                               str(error).split(":", 1)[0] if isinstance(error, ValueError) else "JY_MEDIA_PROBE_FAILED")
        report["blockers"].append({"code": code, "role": role, "message": str(error)[:500]})

    try:
        requested_recipe, requested_manifest = recipe, asset_manifest
        recipe, asset_manifest = copy.deepcopy(recipe), copy.deepcopy(asset_manifest)
        manifest_digest = canonical_digest(asset_manifest)
        trusted_roots = list(trusted_roots)
        relinks = copy.deepcopy(relinks)
        identity = verify_jianying_assets(recipe, asset_manifest, trusted_roots, relinks=relinks)
        report.update(recipe_digest=identity["recipe_digest"], resolution_digest=identity["resolution_digest"],
                      assets=identity["assets"])
        ffprobe, ffmpeg = Path(ffprobe_path), Path(ffmpeg_path)
        if any(not p.is_absolute() or not p.is_file() for p in (ffprobe, ffmpeg)):
            raise MediaError("JY_MEDIA_TOOL_MISSING", "需要调用方提供实际媒体工具绝对路径")
        runner = process_runner if process_runner is not None else AtrMediaRunner(Path.cwd())
        try:
            roles = list(_roles(recipe))
        except (KeyError, TypeError, ValueError) as error:
            raise MediaError("JY_MEDIA_ROLE_INVALID", f"媒体角色或取用区间无效：{error}") from error
    except (ValueError, OSError, TypeError, KeyError) as error:
        block(error)
        return report
    paths = {entry["source_path"]: Path(entry["resolved_path"]) for entry in identity["assets"]}
    probes = {}
    for role in roles:
        check = {"role": role["role"], "metadata_verified": False, "decode_samples_verified": False}
        report["checks"].append(check)
        try:
            source = paths[str(Path(role["src"]).resolve())]
            check.update(source_path=str(Path(role["src"]).resolve()), resolved_path=str(source))
            if role["kind"] == "unsupported":
                raise MediaError("JY_MEDIA_ROLE_UNSUPPORTED", "此包装源的取用策略需要公共消费合同")
            if role["kind"] == "image":
                info = probe_image(source)
                if info["width"] <= 0 or info["height"] <= 0:
                    raise MediaError("JY_MEDIA_IMAGE_INVALID", "图片尺寸无效")
                check["image"] = info
                window = None
            else:
                if source not in probes:
                    probes[source] = probe_media(source, ffprobe, runner=runner)
                streams = probes[source].get("streams", [])
                if not isinstance(streams, list):
                    raise MediaError("JY_MEDIA_METADATA_INVALID", "媒体探测缺少有效streams数组")
                selected = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == role["kind"]]
                if not selected:
                    if role.get("optional_audio"):
                        check.update(metadata_verified=True, decode_samples_verified=False,
                                     skipped=True, skip_reason="audio_track_absent", decode_samples=[])
                        report["warnings"].append({"code": "JY_AMBIENT_AUDIO_ABSENT", "role": role["role"],
                                                   "message": "素材无同期声音轨，沿现有渲染策略跳过"})
                        continue
                    raise MediaError("JY_MEDIA_STREAM_MISSING", "素材缺少所需媒体流")
                stream = selected[0]
                _origin(stream)
                duration, basis = _duration(stream)
                end = role["end"] if role["end"] is not None else duration
                start = role["start"]
                if "sfx_value" in role:
                    value = role["sfx_value"]
                    start = seconds_to_us(value.get("sourceStart", 0))
                    requested_duration = role["available_output_us"]
                    if value.get("end") is not None:
                        requested_duration = seconds_to_us(value["end"]) - seconds_to_us(value.get("start", 0))
                    if requested_duration <= 0:
                        raise MediaError("JY_MEDIA_SFX_WINDOW_INVALID", "音效取用区间无效")
                    requested_end = start + min(requested_duration, role["available_output_us"])
                    end = min(duration, requested_end)
                    check["requested_source_end_us"] = requested_end
                    if requested_end > duration:
                        report["warnings"].append({"code": "JY_SFX_SOURCE_SHORT", "role": role["role"],
                                                   "requested_source_end_us": requested_end, "actual_source_end_us": end})
                window = TimeWindow(start, end)
                validate_source_window(window, media_kind=role["kind"], duration_us=duration)
                check.update(stream_index=stream.get("index"), duration_us=duration,
                             duration_basis=basis, source_start_us=window.start_us,
                             source_end_us=window.end_us, codec=stream.get("codec_name"))
                if role.get("loop_to_us", 0) > duration:
                    report["warnings"].append({"code": "JY_BGM_LOOP_REQUIRED", "role": role["role"],
                                               "source_duration_us": duration, "output_duration_us": role["loop_to_us"]})
            check["metadata_verified"] = True
            try:
                check["decode_samples"] = _decode(role, source, window, ffmpeg, runner)
            except RuntimeError as error:
                raise MediaError("JY_MEDIA_DECODE_FAILED", str(error)) from error
            check["decode_samples_verified"] = True
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            block(error, role["role"])
    report["metadata_verified"] = bool(report["checks"]) and all(c["metadata_verified"] for c in report["checks"])
    sampled_checks = [c for c in report["checks"] if not c.get("skipped")]
    report["decode_samples_verified"] = bool(sampled_checks) and all(c["decode_samples_verified"] for c in sampled_checks)
    try:
        if canonical_digest(requested_recipe) != report["recipe_digest"]:
            raise MediaError("JY_RECIPE_CHANGED", "预检期间配方发生变化")
        if canonical_digest(requested_manifest) != manifest_digest:
            raise MediaError("JY_MANIFEST_CHANGED", "预检期间调用方清单发生变化")
        verify_jianying_assets(recipe, asset_manifest, trusted_roots, relinks=relinks)
    except (ValueError, OSError) as error:
        block(error, "postflight")
        report["metadata_verified"] = report["decode_samples_verified"] = False
    if isinstance(runner, AtrMediaRunner):
        report["processes"] = runner.executions
    if not report["blockers"]:
        report["status"] = "passed"
    return report
