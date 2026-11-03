"""Resolve the complete ad voice only while it belongs to the current locked copy."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import wave

from .ad_artifact_binding import ad_artifact_hashes_match
from .task_script import ScriptDraftError, read_task_script
from .tools import ToolNotFound, default_ffprobe_search_roots, find_ffprobe
from .workflow import project_brief_locked


def _legacy_current_ad_voice(db, project_root: Path, task: dict) -> Path | None:
    if task.get("task_type") != "ad_mix":
        return None
    task_id = task["task_id"]
    if not isinstance(task_id, str) or task_id in {".", ".."} or "/" in task_id or "\\" in task_id:
        return None
    if not project_brief_locked(db, task_id):
        return None
    brief = task.get("brief")
    if not isinstance(brief, dict):
        return None
    script_record = brief.get("final_script")
    if not isinstance(script_record, dict):
        return None
    script_sha = script_record.get("sha256")
    if not isinstance(script_sha, str) or not script_sha:
        return None
    try:
        script = read_task_script(project_root, task)
    except (OSError, ScriptDraftError):
        return None
    if script is None:
        return None
    if hashlib.sha256(script.encode("utf-8")).hexdigest() != script_sha:
        return None
    tasks_root = (Path(project_root) / "runtime" / "workbench" / "tasks").resolve()
    task_root = tasks_root / task_id
    artifacts = task_root / "artifacts"
    ad_dir = artifacts / "ad"
    if task_root.is_symlink() or artifacts.is_symlink() or ad_dir.is_symlink():
        return None
    if not ad_dir.resolve().is_relative_to(task_root):
        return None
    voice = ad_dir / "voice.mp3"
    metadata = ad_dir / "recommendation.json"
    try:
        if voice.is_symlink() or not voice.is_file() or voice.stat().st_size <= 0:
            return None
        if metadata.is_symlink() or not metadata.is_file():
            return None
    except OSError:
        return None
    try:
        binding = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(binding, dict) or binding.get("script_sha256") != script_sha:
        return None
    if not ad_artifact_hashes_match(ad_dir, binding):
        return None
    return voice


@dataclass(frozen=True)
class CurrentAdVoiceSource:
    """One server decision for preview and duration; never a TTS provenance claim."""
    path: Path | None
    source_kind: str
    status: str
    reason: str
    revision: int | None = None
    voice_id: str | None = None
    voice_label: str | None = None

    def public_payload(self):
        # Internal paths and guessed historical voice identities are not public.
        result = dict(sourceKind=self.source_kind,status=self.status,reason=self.reason,
                      revision=self.revision,originalTtsInput='unverified')
        if self.voice_id:
            result.update(voiceId=self.voice_id, voiceLabel=self.voice_label)
        return result


def current_ad_voice_source(db, project_root: Path, task: dict, *, projection_only=False) -> CurrentAdVoiceSource:
    """Keep legacy checks only until a formal voice version takes ownership.

    Editor initialization and saved/cancelled drafts do not activate audio.
    After activation, an unavailable or unverified managed source MUST NOT
    silently return the old ad/voice.mp3. Managed bundle verification is the
    next adapter; its absence is reported, not treated as playback permission.
    This function performs no writes, migrations, generation or gate changes.
    """
    from .voice_ownership import read_voice_ownership
    if not isinstance(task,dict) or task.get('task_type')!='ad_mix':
        return CurrentAdVoiceSource(None,'none','not_applicable','DIGITAL_ORIGINAL_VOICE_PRESERVED')
    task_id=task.get('task_id')
    if (not isinstance(task_id,str) or not task_id or task_id in ('.','..')
            or '/' in task_id or '\\' in task_id):
        return CurrentAdVoiceSource(None,'none','blocked','TASK_INVALID')
    ownership=read_voice_ownership(db,task_id)
    if ownership.branch=='legacy':
        path=_legacy_current_ad_voice(db,project_root,task)
        return CurrentAdVoiceSource(path,'legacy','available' if path else 'unavailable',
                                    ownership.reason if path else 'LEGACY_VOICE_UNAVAILABLE',ownership.revision)
    if ownership.reason=='CURRENT_CONTEXT_INVALID':
        from .voice_task_context import resolve_voice_task_context
        try:
            context=resolve_voice_task_context(db,project_root,task_id)
        except (OSError,ValueError):
            return CurrentAdVoiceSource(None,'managed','blocked','VOICE_CONTEXT_UNAVAILABLE',ownership.revision)
        ownership=read_voice_ownership(db,task_id,context_digest=context.context_digest)
    if ownership.branch=='managed':
        from .voice_bundle_service import read_registered_voice,_service
        try:
            head=_service(db,project_root,task_id).current(task_id)
            reader=read_registered_voice
            if projection_only:
                from .voice_bundle_service import read_registered_voice_projection
                reader=read_registered_voice_projection
            checked=reader(db,project_root,task_id,head['audio'],head['config'],head['context_digest'])
        except (OSError,ValueError,KeyError):
            return CurrentAdVoiceSource(None,'managed','pending','VOICE_BUNDLE_VERIFICATION_REQUIRED',ownership.revision)
        voice_id = head['config']['voice']['voice']
        label = None
        # A stale/missing catalog label must not block a verified audio package.
        from .model_profiles import get_task_profile_binding, resolve_task_tts_voice
        try:
            binding = get_task_profile_binding(db, task_id, 'tts')
            if binding is not None:
                label = resolve_task_tts_voice(db, binding, voice_id).get('displayName')
        except (ValueError, KeyError):
            pass
        return CurrentAdVoiceSource(checked['path'],'managed','available','VOICE_REGISTERED_BUNDLE_VERIFIED',
                                    ownership.revision, voice_id, label)
    return CurrentAdVoiceSource(None,'unknown','blocked',ownership.reason,ownership.revision)


def current_ad_voice(db, project_root: Path, task: dict) -> Path | None:
    """Compatibility path API: all existing consumers share the same ownership."""
    return current_ad_voice_source(db,project_root,task).path


_DURATION_CACHE: OrderedDict[tuple[str, str], float] = OrderedDict()
_DURATION_CACHE_LOCK = Lock()
_DURATION_CACHE_LIMIT = 64


def _duration_content_key(voice: Path) -> tuple[str, str]:
    digest = hashlib.sha256()
    with voice.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    current = voice.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key)
           or getattr(after, key) != getattr(current, key) for key in fields):
        raise OSError("Audio changed while measuring its identity")
    return str(voice.resolve()), digest.hexdigest()


def measured_voice_duration(voice: Path | None) -> float | None:
    """Read playback length from the current complete audio; unknown stays unknown."""
    if voice is None:
        return None
    # Registered voice packages are PCM WAV. Count readable frames directly,
    # keeping verification local and avoiding a subprocess on every detail read.
    # Compressed or unsupported WAVE formats still use the established probe.
    try:
        with wave.open(str(voice), "rb") as pcm:
            rate = pcm.getframerate()
            frames = pcm.getnframes()
            frame_bytes = pcm.getnchannels() * pcm.getsampwidth()
            if rate <= 0 or frames <= 0 or frame_bytes <= 0:
                return None
            remaining = frames
            while remaining:
                count = min(remaining, 65536)
                if len(pcm.readframes(count)) != count * frame_bytes:
                    return None
                remaining -= count
            seconds = frames / rate
            return seconds if math.isfinite(seconds) and seconds > 0 else None
    except (wave.Error, EOFError, RuntimeError):
        pass
    except OSError:
        return None
    try:
        # Reuse only a successful measurement of identical complete bytes. A
        # filename/mtime hit is insufficient, and failures never enter the cache.
        key = _duration_content_key(voice)
        with _DURATION_CACHE_LOCK:
            cached = _DURATION_CACHE.get(key)
            if cached is not None:
                _DURATION_CACHE.move_to_end(key)
                return cached
        ffprobe = find_ffprobe(os.environ, search_roots=default_ffprobe_search_roots())
        result = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(voice)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            return None
        seconds = float(result.stdout.strip())
        if not math.isfinite(seconds) or seconds <= 0 or _duration_content_key(voice) != key:
            return None
    except (ToolNotFound, OSError, ValueError, subprocess.TimeoutExpired):
        return None
    with _DURATION_CACHE_LOCK:
        _DURATION_CACHE[key] = seconds
        _DURATION_CACHE.move_to_end(key)
        while len(_DURATION_CACHE) > _DURATION_CACHE_LIMIT:
            _DURATION_CACHE.popitem(last=False)
    return seconds
