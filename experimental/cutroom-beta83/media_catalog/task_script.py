"""First script draft persistence; confirmed scripts use the rework workflow."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from .provenance import canonical_digest, record_gate
from .tasks import get_task
from .workflow import project_gates, rollback_to_stage_connection


class ScriptDraftError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message



def transcript_has_current_downstream(db, task_id: str) -> bool:
    """Historical renders survive a proven return to transcript review.

    The storyboard stage keeps the rollback reason when transcription makes it
    ready again. Completing a new storyboard replaces that receipt. Missing or
    unrelated receipts remain fail-closed; scene cards always count as current.
    """
    connection = db.connection
    if connection.execute("SELECT 1 FROM scene_cards WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
        return True
    if not any(connection.execute(f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1", (task_id,)).fetchone()
               for table in ("candidate_versions", "render_snapshots")):
        return False
    boundary = connection.execute(
        "SELECT status,detail_json FROM task_stage_states WHERE task_id=? AND stage_key='ai_storyboard'",
        (task_id,),
    ).fetchone()
    if boundary is None or boundary['status'] not in {'invalidated', 'ready'}:
        return True
    try:
        detail = json.loads(boundary['detail_json'] or '{}')
        reason = detail.get('reason') if isinstance(detail, dict) else None
    except (ValueError, TypeError):
        return True
    if isinstance(reason, str) and reason in {'script_revision:transcript_review', 'transcript_recovery'}:
        return False
    if isinstance(reason, str) and reason.startswith('rework:'):
        rework = connection.execute(
            "SELECT impact_json FROM rework_requests WHERE task_id=? AND rework_id=? "
            "AND target_stage='brief_lock' AND status='applied' AND result_candidate_id IS NULL",
            (task_id, reason[len('rework:'):]),
        ).fetchone()
        if rework is not None:
            try:
                impact = json.loads(rework['impact_json'] or '{}')
                return not (isinstance(impact, dict) and
                            (impact.get('script_revised') is True or impact.get('source_revised') is True))
            except (ValueError, TypeError):
                pass
    return True


def read_task_script(project_root: Path, task: dict) -> str | None:
    """Project the saved script for review without accepting arbitrary file paths."""
    task_id = str(task.get("task_id") or "")
    brief = task.get("brief") or {}
    script = brief.get("final_script") or {}
    pending = not script and brief.get("script_draft_sha256") == hashlib.sha256(b"").hexdigest()
    if (
        not task_id
        or Path(task_id).name != task_id
        or (not pending and script.get("relative_path") != "inputs/final-script.txt")
    ):
        return None
    tasks_root = Path(project_root) / "runtime" / "workbench" / "tasks"
    task_root = tasks_root / task_id
    path = task_root / "inputs" / "final-script.txt"
    try:
        resolved_root = task_root.resolve(strict=True)
        if not resolved_root.is_relative_to(tasks_root.resolve(strict=True)):
            return None
        if pending:
            inputs = path.parent.resolve(strict=True)
            if not inputs.is_relative_to(resolved_root) or path.exists() or path.is_symlink():
                return None
            return ""
        if not path.resolve(strict=True).is_relative_to(resolved_root):
            return None
        if path.stat().st_size > 200_000:
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None


def save_script_draft(db, project_root: Path, task_id: str, text: str, expected: str):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 200_000:
        raise ScriptDraftError(400, "INVALID_REQUEST", "文案必须是有效文本且不超过 200KB。")
    if not text.strip():
        text = ""
    connection = db.connection
    if connection.in_transaction:
        raise RuntimeError("Script save requires its own transaction")
    connection.execute("BEGIN IMMEDIATE")
    replaced = False
    had_file = False
    temporary = None
    archive = artifacts = None
    moved_artifacts = False
    created_artifacts = False
    try:
        current = get_task(db, task_id)
        if project_gates(db, task_id).get("brief_locked"):
            raise ScriptDraftError(409, "BRIEF_ALREADY_LOCKED", "已确认文案请通过返工流程修改。")
        if connection.execute(
            "SELECT 1 FROM jobs WHERE task_id=? AND job_type!='initial_source_check' AND status IN ('queued','running','pending') LIMIT 1",
            (task_id,),
        ).fetchone():
            raise ScriptDraftError(409, "TASK_BUSY", "任务正在处理，请等待作业结束后修改。")
        brief = dict(current.get("brief") or {})
        current_digest = (brief.get("final_script") or {}).get("sha256")
        if current_digest is None:
            current_digest = brief.get("script_draft_sha256")
        if current_digest != expected:
            raise ScriptDraftError(409, "SCRIPT_CHANGED", "文案已更新，请重新载入后核对。")
        previous = read_task_script(project_root, current)
        if previous is None:
            raise ScriptDraftError(409, "SCRIPT_UNAVAILABLE", "文案文件不可访问，请恢复任务输入后重试。")
        if hashlib.sha256(previous.encode("utf-8")).hexdigest() != expected:
            raise ScriptDraftError(409, "SCRIPT_CHANGED", "文案文件已变化，请先恢复一致的任务输入。")
        if text != previous:
            path = project_root / "runtime" / "workbench" / "tasks" / task_id / "inputs" / "final-script.txt"
            rework = connection.execute(
                """SELECT rework_id,impact_json FROM rework_requests WHERE task_id=? AND target_stage='brief_lock'
                AND status='applied' AND result_candidate_id IS NULL ORDER BY rowid DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            rework_impact = json.loads(rework["impact_json"] or "{}") if rework is not None else {}
            if rework is not None and not rework_impact.get("script_revised"):
                task_root = path.parent.parent.resolve()
                tasks_root = (Path(project_root) / "runtime" / "workbench" / "tasks").resolve()
                artifacts = task_root / "artifacts"
                if (task_root == tasks_root or not task_root.is_relative_to(tasks_root)
                        or artifacts.is_symlink() or (artifacts.exists() and not artifacts.is_dir())):
                    raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务产物目录不可用。")
                history = task_root / "history" / "script-revisions"
                if not history.resolve().is_relative_to(task_root):
                    raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务历史目录不可用。")
                archive = history / uuid.uuid4().hex
                history.mkdir(parents=True, exist_ok=True)
                archive.mkdir()
                cards = [dict(row) for row in connection.execute(
                    "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id", (task_id,),
                )]
                feedback = [dict(row) for row in connection.execute(
                    "SELECT * FROM storyboard_feedback WHERE task_id=? ORDER BY rowid", (task_id,),
                )]
                (archive / "previous-script.txt").write_text(previous, encoding="utf-8")
                (archive / "scene-cards.json").write_text(
                    json.dumps({"cards": cards, "feedback": feedback, "rework_id": rework["rework_id"]},
                               ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8",
                )
                if artifacts.is_dir():
                    os.replace(artifacts, archive / "artifacts")
                    moved_artifacts = True
                artifacts.mkdir()
                created_artifacts = True
                connection.execute("DELETE FROM scene_cards WHERE task_id=?", (task_id,))
                connection.execute(
                    "UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?", (task_id,),
                )
                impact = rework_impact
                impact["script_revised"] = True
                connection.execute(
                    "UPDATE rework_requests SET impact_json=? WHERE rework_id=?",
                    (json.dumps(impact, ensure_ascii=False, sort_keys=True), rework["rework_id"]),
                )
            temporary = path.with_name(f".script-{uuid.uuid4().hex}.tmp")
            had_file = path.exists()
            if text:
                temporary.write_bytes(text.encode("utf-8"))
                os.replace(temporary, path)
                brief.pop("script_draft_sha256", None)
                brief["final_script"] = {
                    "relative_path": "inputs/final-script.txt",
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            else:
                path.unlink()
                brief.pop("final_script", None)
                brief["script_draft_sha256"] = hashlib.sha256(b"").hexdigest()
            replaced = True
            connection.execute(
                "UPDATE tasks SET brief_json=?, updated_at=datetime('now') WHERE task_id=?",
                (json.dumps(brief, ensure_ascii=False), task_id),
            )
        connection.commit()
    except Exception:
        try:
            if replaced:
                # Restore the previous file state, including a genuinely absent
                # script on a newly created task.
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                if had_file:
                    temporary.write_bytes(previous.encode("utf-8"))
                    os.replace(temporary, path)
                else:
                    path.unlink(missing_ok=True)
            if archive is not None:
                if created_artifacts and artifacts.exists():
                    shutil.rmtree(artifacts)
                if moved_artifacts:
                    os.replace(archive / "artifacts", artifacts)
                if archive.exists():
                    shutil.rmtree(archive)
        finally:
            connection.rollback()
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def revise_transcribed_script(
    db, project_root: Path, task_id: str, text: str,
    expected_script_sha256: str, expected_transcript_digest: str,
) -> dict:
    """Reopen an early digital task after listening, retaining its old evidence."""
    from .transcript_reconciliation import project_transcript_state
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 200_000:
        raise ScriptDraftError(400, "INVALID_REQUEST", "更正文案不能为空且不超过 200KB。")
    connection = db.connection
    if connection.in_transaction:
        raise RuntimeError("Script revision requires its own transaction")
    connection.execute("BEGIN IMMEDIATE")
    tasks_root = (Path(project_root) / "runtime" / "workbench" / "tasks").resolve()
    task_root = (tasks_root / task_id).resolve()
    script_path = task_root / "inputs" / "final-script.txt"
    artifacts = task_root / "artifacts"
    history_root = task_root / "history" / "script-revisions"
    archive = history_root / uuid.uuid4().hex
    temporary = script_path.with_name(f".script-{uuid.uuid4().hex}.tmp")
    moved_artifacts = replaced = False
    previous = None
    try:
        task = get_task(db, task_id)
        if task is None:
            raise ScriptDraftError(404, "NOT_FOUND", "任务不存在。")
        if not task_root.is_relative_to(tasks_root) or task_root == tasks_root:
            raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务目录不可用。")
        if task.get("task_type") != "digital_human":
            raise ScriptDraftError(409, "ROUTE_INVALID", "仅数字人口播任务可在口播核对中更正文案。")
        if connection.execute(
            "SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running','pending') LIMIT 1",
            (task_id,),
        ).fetchone():
            raise ScriptDraftError(409, "TASK_BUSY", "任务正在处理，请等待作业结束后修改。")
        if transcript_has_current_downstream(db, task_id):
            raise ScriptDraftError(409, "REWORK_REQUIRED", "已有分镜或成片候选，请使用返工流程修改。")
        previous = read_task_script(project_root, task)
        brief = dict(task.get("brief") or {})
        if previous is None or hashlib.sha256(previous.encode("utf-8")).hexdigest() != expected_script_sha256 \
                or (brief.get("final_script") or {}).get("sha256") != expected_script_sha256:
            raise ScriptDraftError(409, "SCRIPT_CHANGED", "文案已变化，请重新载入后核对。")
        if text == previous:
            raise ScriptDraftError(409, "SCRIPT_UNCHANGED", "更正文案与当前版本相同。")
        state = project_transcript_state(project_root, task, verify_source_content=True)
        if state is None or state["status"] not in {"aligned", "needs_review", "reviewed"} \
                or state["digest"] != expected_transcript_digest:
            raise ScriptDraftError(409, "TRANSCRIPT_REVIEW_STALE", "口播核对依据已变化，请刷新后重听。")
        if not artifacts.is_dir() or artifacts.is_symlink() or script_path.is_symlink():
            raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务输入或转写目录不可用。")
        if not history_root.resolve().is_relative_to(task_root):
            raise ScriptDraftError(409, "TASK_INPUT_INVALID", "任务历史目录不可用。")
        history_root.mkdir(parents=True, exist_ok=True)
        archive.mkdir()
        (archive / "previous-script.txt").write_text(previous, encoding="utf-8")
        (archive / "revision.json").write_text(json.dumps({
            "previous_script_sha256": expected_script_sha256,
            "previous_transcript_digest": expected_transcript_digest,
            "new_script_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(artifacts, archive / "artifacts")
        moved_artifacts = True
        artifacts.mkdir()
        temporary.write_bytes(text.encode("utf-8"))
        os.replace(temporary, script_path)
        replaced = True
        brief["final_script"] = {
            "relative_path": "inputs/final-script.txt",
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        connection.execute(
            "UPDATE tasks SET brief_json=?, status='draft', updated_at=datetime('now') WHERE task_id=?",
            (json.dumps(brief, ensure_ascii=False), task_id),
        )
        rollback_to_stage_connection(
            connection, task_id, "brief_lock", reason="script_revision:transcript_review"
        )
        result = {"task": get_task(db, task_id), "archive": str(archive)}
        connection.commit()
        return result
    except Exception:
        try:
            if replaced and previous is not None:
                temporary.write_bytes(previous.encode("utf-8"))
                os.replace(temporary, script_path)
            if moved_artifacts:
                if artifacts.exists():
                    shutil.rmtree(artifacts)
                os.replace(archive / "artifacts", artifacts)
            if archive.exists():
                shutil.rmtree(archive)
        finally:
            connection.rollback()
        raise
    finally:
        temporary.unlink(missing_ok=True)


def confirm_script(db, project_root: Path, task_id: str, expected: str | None = None) -> dict:
    """Bind approval to current nonempty bytes under the same writer lock as save."""
    with db.transaction(immediate=True) as connection:
        current = get_task(db, task_id)
        if current is None:
            raise ScriptDraftError(404, "NOT_FOUND", "任务不存在。")
        text = read_task_script(project_root, current)
        if not text or not text.strip():
            raise ScriptDraftError(409, "FINAL_SCRIPT_REQUIRED", "请先填写并保存本次最终文案。")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        brief = current["brief"]
        if digest != brief["final_script"].get("sha256") or (expected is not None and expected != digest):
            raise ScriptDraftError(409, "SCRIPT_CHANGED", "文案已变化，请重新核对当前保存版本。")
        if current["task_type"] == "digital_human":
            source = brief.get("digital_human_source") or {}
            # New tasks store a source snapshot. Older imported tasks can lack
            # these fields; their existing approval flow remains readable.
            if all(source.get(key) is not None for key in
                   ("path", "size_bytes", "mtime_ns", "quick_fingerprint")):
                from .digital_workflow import _default_file_snapshot, _source_snapshot_identity
                try:
                    live_source = _default_file_snapshot(Path(source["path"]))
                    same_source = _source_snapshot_identity(live_source) == _source_snapshot_identity(source)
                except (OSError, ValueError, TypeError, KeyError):
                    same_source = False
                if not same_source:
                    raise ScriptDraftError(409, "SOURCE_CHANGED", "口播底片已变化，请更换底片并重新核对文案。")
        gate = connection.execute(
            "SELECT event_id,gate_digest FROM task_gate_events WHERE task_id=? AND gate_type='brief' ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        rework = connection.execute(
            """SELECT impact_json FROM rework_requests WHERE task_id=? AND target_stage='brief_lock'
            AND status='applied' AND result_candidate_id IS NULL ORDER BY rowid DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        prior_gate_id = (json.loads(rework["impact_json"] or "{}")
                         .get("prior_brief_gate_event_id")) if rework else None
        change = json.loads(rework["impact_json"] or "{}") if rework else {}
        if rework is not None and not (change.get("script_revised") or change.get("source_revised")):
            raise ScriptDraftError(409, "REWORK_CHANGE_REQUIRED", "请先保存返工后的文案，再重新确认。")
        newly_confirmed = (current.get("status") == "draft"
                           or not project_gates(db, task_id)["brief_locked"]
                           or gate is None or gate["gate_digest"] != canonical_digest(brief)
                           or (rework is not None and gate["event_id"] == prior_gate_id))
        if newly_confirmed:
            connection.execute("UPDATE tasks SET status='brief_locked', updated_at=datetime('now') WHERE task_id=?", (task_id,))
            record_gate(connection, task_id, "brief", canonical_digest(brief), brief)
        from .voice_script_rework import rebind_confirmed_script_voice
        rebind_confirmed_script_voice(
            db, project_root, task_id,
            previous_gate_id=gate["event_id"] if gate else None,
        )
        return {"task": get_task(db, task_id), "newly_confirmed": newly_confirmed}


def require_transcription_inputs(db, task_id: str, input_digest: str | None, *,
                                 script_sha256: str | None = None,
                                 project_root: Path | None = None) -> str:
    """Recheck the confirmed inputs inside enqueue's lock and at worker entry."""
    task = get_task(db, task_id)
    brief = (task or {}).get("brief") or {}
    current_sha = (brief.get("final_script") or {}).get("sha256")
    valid = (task is not None and task.get("status") == "brief_locked"
             and project_gates(db, task_id)["brief_locked"]
             and (input_digest is None or canonical_digest(brief) == input_digest)
             and (script_sha256 is None or current_sha == script_sha256))
    if valid and project_root is not None:
        text = read_task_script(project_root, task)
        valid = (isinstance(text, str) and bool(text.strip())
                 and hashlib.sha256(text.encode("utf-8")).hexdigest() == current_sha)
    if not valid:
        raise ScriptDraftError(409, "TRANSCRIPTION_INPUT_CHANGED",
            "文案或确认状态已变化，请核对当前文案后重新确认。")
    gate = db.connection.execute(
        "SELECT event_id FROM task_gate_events WHERE task_id=? AND gate_type='brief' "
        "ORDER BY rowid DESC LIMIT 1", (task_id,),
    ).fetchone()
    return gate["event_id"]


def _transcription_outputs_current(project_root: Path, task: dict) -> bool:
    """Reuse verified files; dispatch only states the worker can recover safely."""
    from .transcript_reconciliation import project_transcript_state
    artifacts = Path(project_root) / "runtime/workbench/tasks" / task["task_id"] / "artifacts"
    if (artifacts / ".transcription-promotion.json").exists():
        return False  # The worker verifies and finishes its existing publication.
    names = ("transcript.srt", "transcript.words.json", "transcription.json")
    if not any((artifacts / name).exists() for name in names):
        return False  # Rework archived the former outputs; run local Whisper again.
    try:
        state = project_transcript_state(project_root, task, verify_source_content=True)
        srt = (artifacts / "transcript.srt").read_bytes()
        metadata = json.loads((artifacts / "transcription.json").read_text(encoding="utf-8"))
        if (state is not None and state["status"] in {"aligned", "needs_review", "reviewed"}
                and srt.strip() and metadata.get("srt_sha256") in (
                    None, hashlib.sha256(srt).hexdigest())):
            return True
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    raise ScriptDraftError(409, "TRANSCRIPTION_OUTPUTS_CHANGED",
        "当前转写文件不完整或已变化，原文件已保留。请恢复转写文件后重试；本次未重新生成。")


def transcription_dispatch(db, task: dict, project_root: Path | None = None) -> dict | None:
    """Describe the first transcription request, preserving existing retry controls."""
    if task.get("status") != "brief_locked":
        return None
    task_id = task["task_id"]
    digest = task["brief"]["final_script"]["sha256"]
    input_digest = canonical_digest(task["brief"])
    gates = db.connection.execute(
        "SELECT event_id,gate_digest,created_at FROM task_gate_events WHERE task_id=? AND gate_type='brief' ORDER BY rowid",
        (task_id,),
    ).fetchall()
    single_version_history = bool(gates) and all(gate["gate_digest"] == input_digest for gate in gates)
    rows = db.connection.execute(
        "SELECT j.job_id,j.status,j.created_at,p.payload_json FROM jobs j LEFT JOIN job_payloads p ON p.job_id=j.job_id "
        "WHERE j.task_id=? AND j.job_type='transcribe' ORDER BY j.rowid DESC",
        (task_id,),
    ).fetchall()
    recovery_of = None
    outputs_current = None
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        legacy_same_version = (
            payload.get("input_digest") is None
            and payload.get("script_sha256") in (None, digest)
            and single_version_history
            and row["created_at"] >= gates[0]["created_at"]
        )
        if payload.get("input_digest") == input_digest or legacy_same_version:
            recovery = payload.get("recovery")
            if recovery and (not gates or recovery.get("brief_event_id") != gates[-1]["event_id"]):
                # A destructive repair belongs to its original confirmation, even
                # when later rework returns to identical brief bytes. Keep the old
                # job/archive as history and dispatch a fresh local transcription.
                recovery_of = recovery_of or row["job_id"]
                continue
            if row["status"] == "cancelled":
                # This function is called by an explicit confirmation, not a poll.
                recovery_of = recovery_of or row["job_id"]
                continue
            if row["status"] == "completed" and project_root is not None:
                if outputs_current is None:
                    outputs_current = _transcription_outputs_current(project_root, task)
                if not outputs_current:
                    recovery_of = recovery_of or row["job_id"]
                    continue
            # A legacy job needs approval history and dispatch time evidence.
            # Failed/paused jobs retain their existing explicit resume controls.
            return {"job_id": row["job_id"], "existing": True}
    identity = {"task_id": task_id, "brief": task["brief"]}
    if recovery_of is not None:
        # Keep the historical completion intact and deduplicate its local retry.
        # The worker handles valid current files and interrupted promotion receipts.
        identity["recovery_of"] = recovery_of
    key = canonical_digest(identity)
    return {"job_id": f"transcribe-{key}", "existing": False, "script_sha256": digest, "input_digest": input_digest}
