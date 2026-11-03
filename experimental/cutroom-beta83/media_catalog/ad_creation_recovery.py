"""Recover only owned ad-input directories whose atomic creation rolled back."""
import json
import os
import re
import uuid
from pathlib import Path

from .digital_workflow import _assert_no_reparse_path, _ensure_directory_chain_no_reparse, _remove_tree

MARKER = ".atomic-ad-creation.json"


def register_creation(db, root, task_id, staging):
    if not getattr(db, "creation_transaction_managed", False):
        return
    workbench = Path(root).resolve() / "runtime" / "workbench"
    recovery = workbench / "recovery"
    _ensure_directory_chain_no_reparse(recovery, Path(root).resolve())
    _assert_no_reparse_path(recovery, Path(root).resolve())
    _assert_no_reparse_path(staging, workbench)
    state = {"version": 1, "task_id": task_id, "request_id": db.request_id,
             "input_digest": db.input_digest, "owner_token": uuid.uuid4().hex}
    journal = recovery / ("ad-atomic-" + task_id + ".json")
    payload = json.dumps(state, sort_keys=True)
    # Exclusive files and a matching directory marker prevent adopting old paths.
    with journal.open("x", encoding="utf-8") as stream:
        stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    with (staging / MARKER).open("x", encoding="utf-8") as stream:
        stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    original_db = db._db
    def finish():
        with original_db.transaction(immediate=True):
            if journal.exists():
                _recover_one(original_db, workbench, journal)
    db.on_commit(finish)
    db.on_rollback(finish)


def _recover_one(db, workbench, journal):
    _assert_no_reparse_path(journal, workbench.parent.parent)
    state = json.loads(journal.read_text(encoding="utf-8"))
    if set(state) != {"version", "task_id", "request_id", "input_digest", "owner_token"} or state["version"] != 1:
        raise ValueError("广告创建恢复记录无效")
    for key, pattern in (("task_id", r"task_[0-9a-f]{32}"), ("request_id", r"[A-Za-z0-9_-]{1,100}"),
                         ("input_digest", r"[0-9a-f]{64}"), ("owner_token", r"[0-9a-f]{32}")):
        if not isinstance(state[key], str) or not re.fullmatch(pattern, state[key]):
            raise ValueError("广告创建恢复凭证无效")
    task_id = state["task_id"]
    if journal.name != "ad-atomic-" + task_id + ".json":
        raise ValueError("广告创建恢复路径不匹配")
    task = db.connection.execute("SELECT task_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    receipt = db.connection.execute("SELECT input_digest,status,response_json FROM task_create_requests WHERE request_id=?", (state["request_id"],)).fetchone()
    committed = False
    if task is not None or receipt is not None:
        committed = bool(task is not None and receipt is not None and receipt["status"] == "completed"
                         and receipt["input_digest"] == state["input_digest"]
                         and json.loads(receipt["response_json"]).get("task_id") == task_id)
        if not committed:
            raise ValueError("广告任务与创建回执不一致，保留现场")
    final = workbench / "tasks" / task_id
    staging = workbench / "tasks" / ("." + task_id + ".staging")
    owned = []
    for directory in (staging, final):
        if not os.path.lexists(directory):
            continue
        _assert_no_reparse_path(directory, workbench)
        marker = directory / MARKER
        if committed and directory == final and not marker.exists():
            # A prior committed cleanup may have removed the marker before exit.
            continue
        _assert_no_reparse_path(marker, workbench)
        if json.loads(marker.read_text(encoding="utf-8")) != state:
            raise ValueError("广告创建目录所有权不匹配，保留现场")
        owned.append(directory)
    if committed and staging in owned:
        raise ValueError("广告创建完成但临时目录仍存在，保留现场")
    for directory in owned:
        if committed:
            (directory / MARKER).unlink()
        else:
            _remove_tree(directory, trusted_root=workbench.parent.parent)
    journal.unlink()


def recover_incomplete_ad_tasks(db, project_root):
    workbench = Path(project_root).resolve() / "runtime" / "workbench"
    recovery = workbench / "recovery"
    result = {"processed": 0, "failed": []}
    if not recovery.exists():
        return result
    _assert_no_reparse_path(recovery, Path(project_root).resolve())
    for journal in sorted(recovery.glob("ad-atomic-task_*.json")):
        result["processed"] += 1
        try:
            _recover_one(db, workbench, journal)
        except Exception as error:
            result["failed"].append({"journal": journal.name, "error": str(error)})
    return result
