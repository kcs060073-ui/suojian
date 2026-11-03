"""Durable, job-scoped checkpoints for local publish-package staging."""

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .snapshots import file_content_signature


_TAIL_FILES = {
    "delivery-manifest.json", "material-timecodes.json", "audio-source-map.json",
    "publish-copy.md", "qa-report.md", "publish-check.json",
}
_TAIL_PREFIXES = ("video-base.", "subtitles.", "audio-voice.", "audio-bgm.", "audio-sfx.", "audio-source-")


def _owned_tail_name(name: str) -> bool:
    return name in _TAIL_FILES or name.startswith(_TAIL_PREFIXES)


class PublishStaging:
    """Keep verified staged files across an explicit retry of the same job."""

    def __init__(self, root: Path, task_id: str, input_digest: str,
                 version: int, retry_key: str | None):
        self.persistent = retry_key is not None
        self.state_path: Path | None = None
        self.state: dict | None = None
        if retry_key is None:
            self.path = root / f".{task_id}.part-{uuid.uuid4().hex}"
            self.record_id = f"pub-{uuid.uuid4().hex[:12]}"
            self.path.mkdir(exist_ok=False)
            return
        if not isinstance(retry_key, str) or not retry_key.strip() or len(retry_key) > 200:
            raise ValueError("DELIVERY_RETRY_KEY_INVALID: 交付重试身份无效")
        retry_hash = hashlib.sha256(retry_key.encode("utf-8")).hexdigest()[:24]
        suffix = f"{retry_hash}-{input_digest[:16]}"
        self.path = root / f".{task_id}.part-{suffix}"
        self.state_path = root / f".{task_id}.part-state-{suffix}.json"
        if self.state_path.exists():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise RuntimeError("DELIVERY_STAGE_CHANGED: 交付重试检查点无法读取") from error
            if (
                not isinstance(state, dict)
                or state.get("schema") != "cutroom.publish-stage/v1"
                or state.get("task_id") != task_id
                or state.get("input_digest") != input_digest
                or state.get("retry_hash") != retry_hash
                or state.get("version") != version
                or not re.fullmatch(r"pub-[a-f0-9]{12}", str(state.get("record_id") or ""))
                or not isinstance(state.get("files"), dict)
            ):
                raise RuntimeError("DELIVERY_STAGE_CHANGED: 交付重试检查点身份已变化")
            self.state = state
        else:
            if self.path.exists() or self.path.is_symlink():
                raise RuntimeError("DELIVERY_STAGE_CHANGED: 临时目录没有可信检查点")
            self.state = {
                "schema": "cutroom.publish-stage/v1",
                "task_id": task_id,
                "input_digest": input_digest,
                "retry_hash": retry_hash,
                "version": version,
                "record_id": f"pub-{uuid.uuid4().hex[:12]}",
                "files": {},
                "groups": {},
                "tail": None,
            }
            try:
                with self.state_path.open("x", encoding="utf-8") as target:
                    target.write(json.dumps(self.state, ensure_ascii=False) + "\n")
                    target.flush()
                    os.fsync(target.fileno())
            except FileExistsError as error:
                raise RuntimeError("DELIVERY_STAGE_BUSY: 同一交付任务已有并行检查点") from error
        self.record_id = self.state["record_id"]
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_dir()):
            raise RuntimeError("DELIVERY_STAGE_CHANGED: 临时交付目录类型已变化")
        if not self.path.exists():
            self.path.mkdir(exist_ok=False)
            self.state["files"] = {}
            self.state["groups"] = {}
            self.state["tail"] = None
            self._save()

    def _save(self) -> None:
        if self.state_path is None:
            return
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self.state, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def file_valid(self, name: str) -> bool:
        if self.state is None:
            return False
        expected = self.state["files"].get(name)
        path = self.path / name
        if not isinstance(expected, dict) or path.is_symlink() or not path.is_file():
            return False
        try:
            return file_content_signature(path) == expected
        except OSError:
            return False

    def record_file(self, name: str) -> None:
        if self.state is None:
            return
        self.state["files"][name] = file_content_signature(self.path / name)
        self.state["groups"] = {}
        self.state["tail"] = None
        self._save()

    def valid_group(self, group_name: str) -> list[str] | None:
        if self.state is None:
            return None
        group = (self.state.get("groups") or {}).get(group_name)
        if not isinstance(group, dict):
            return None
        files = group.get("files")
        signatures = group.get("signatures")
        if not isinstance(files, list) or not isinstance(signatures, dict):
            return None
        for name in files:
            if not isinstance(name, str) or not _owned_tail_name(name):
                return None
            path = self.path / name
            if path.is_symlink() or not path.is_file():
                return None
            try:
                if file_content_signature(path) != signatures.get(name):
                    return None
            except OSError:
                return None
        return list(files)

    def record_group(self, group_name: str, files: list[str]) -> None:
        if self.state is None:
            return
        self.state.setdefault("groups", {})[group_name] = {
            "files": list(files),
            "signatures": {
                name: file_content_signature(self.path / name) for name in files
            },
        }
        self.state["tail"] = None
        self._save()

    def valid_tail(self) -> list[str] | None:
        if self.state is None or not isinstance(self.state.get("tail"), dict):
            return None
        tail = self.state["tail"]
        files = tail.get("files")
        signatures = tail.get("signatures")
        if not isinstance(files, list) or not files or not isinstance(signatures, dict):
            return None
        if {path.name for path in self.path.iterdir()} != set(files):
            return None
        for name in files:
            if not isinstance(name, str) or Path(name).name != name:
                return None
            path = self.path / name
            if path.is_symlink() or not path.is_file():
                return None
            try:
                if file_content_signature(path) != signatures.get(name):
                    return None
            except OSError:
                return None
        return list(files)

    def valid_component_files(self) -> set[str]:
        if self.state is None:
            return set()
        return {
            name for name in self.state["files"]
            if name.startswith(_TAIL_PREFIXES) and self.file_valid(name)
        }

    def clear_tail(self, core_names: set[str]) -> None:
        removable = []
        for path in self.path.iterdir():
            if path.name in core_names:
                continue
            if not _owned_tail_name(path.name) or not (path.is_symlink() or path.is_file()):
                raise RuntimeError("DELIVERY_STAGE_CHANGED: 临时目录包含非交付产物")
            removable.append(path)
        for path in removable:
            path.unlink()
        if self.state is not None:
            self.state["tail"] = None
            self._save()

    def record_tail(self, files: list[str]) -> None:
        if self.state is None:
            return
        self.state["tail"] = {
            "files": list(files),
            "signatures": {
                name: file_content_signature(self.path / name) for name in files
            },
        }
        self._save()

    def finish(self) -> None:
        if self.state_path is not None:
            try:
                self.state_path.unlink(missing_ok=True)
            except OSError:
                pass
