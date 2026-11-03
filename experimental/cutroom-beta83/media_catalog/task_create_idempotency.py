"""Durable replay protection for the workbench create endpoint."""

from contextlib import contextmanager

import hashlib
import json
import re
import logging


class CreateRequestConflict(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _CreationTransaction:
    """Keep nested creation helpers inside the request's single commit."""

    creation_transaction_managed = True

    def __init__(self, db, request_id=None, input_digest=None):
        self._db = db
        self.request_id = request_id
        self.input_digest = input_digest
        self._commit_callbacks = []
        self._rollback_callbacks = []
        self._savepoint_sequence = 0

    def on_commit(self, callback):
        self._commit_callbacks.append(callback)

    def on_rollback(self, callback):
        self._rollback_callbacks.append(callback)

    def finish(self, committed):
        for callback in (self._commit_callbacks if committed else self._rollback_callbacks):
            try:
                callback()
            except Exception:
                # Durable journals remain for startup/retry recovery. A cleanup
                # failure must not turn an already committed task into a retry.
                logging.getLogger(__name__).exception("Task creation cleanup deferred")

    def __getattr__(self, name):
        return getattr(self._db, name)

    @contextmanager
    def transaction(self, **_kwargs):
        connection = self._db.connection
        if not connection.in_transaction:
            raise RuntimeError("CREATE_TRANSACTION_REQUIRED")
        self._savepoint_sequence += 1
        name = "creation_" + str(self._savepoint_sequence)
        connection.execute("SAVEPOINT " + name)
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK TO SAVEPOINT " + name)
            connection.execute("RELEASE SAVEPOINT " + name)
            raise
        else:
            connection.execute("RELEASE SAVEPOINT " + name)


def create_once(db, request: dict, creator, *, recover=None):
    request_id = request.get("requestId")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
        raise CreateRequestConflict("CREATE_REQUEST_ID_INVALID", "创建请求编号无效。")
    input_payload = {key: value for key, value in request.items() if key != "requestId"}
    input_digest = hashlib.sha256(json.dumps(
        input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    # Recovery can finish legacy filesystem compensation. Commit it before
    # validating/creating a different task, which may itself fail.
    if recover is not None:
        recover(db)
    transaction = _CreationTransaction(db, request_id, input_digest)
    try:
        with db.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT input_digest,status,response_json FROM task_create_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["input_digest"] != input_digest:
                    raise CreateRequestConflict(
                        "CREATE_IDEMPOTENCY_CONFLICT", "创建请求编号已用于另一组输入。"
                    )
                if existing["status"] == "completed":
                    return json.loads(existing["response_json"])
                raise CreateRequestConflict(
                    "CREATE_RECOVERY_REQUIRED", "旧版创建请求未能确认完成。请先在任务列表核对是否已创建；不要重复提交此请求。"
                )
            connection.execute(
                "INSERT INTO task_create_requests(request_id,input_digest,status) VALUES(?,?,'pending')",
                (request_id, input_digest),
            )
            # A process exit rolls back both the task and its replay receipt. The
            # second connection waits for this transaction, then replays one result.
            # Creation performs local validation/files only, never a paid model call.
            result = creator(input_payload, transaction)
            response = json.dumps(result, ensure_ascii=False)
            connection.execute(
                "UPDATE task_create_requests SET status='completed',response_json=?,completed_at=datetime('now') WHERE request_id=? AND status='pending'",
                (response, request_id),
            )
    except BaseException:
        transaction.finish(False)
        raise
    transaction.finish(True)
    return result


def recover_task_creations(db, project_root):
    """Serialize startup/retry recovery against any in-flight creator."""
    from .ad_creation_recovery import recover_incomplete_ad_tasks
    from .digital_workflow import recover_incomplete_digital_tasks
    def recover(connection_db):
        return {"ad": recover_incomplete_ad_tasks(connection_db, project_root),
                "digital": recover_incomplete_digital_tasks(connection_db, project_root)}
    if getattr(db, "creation_transaction_managed", False):
        return recover(db)
    with db.transaction(immediate=True):
        return recover(_CreationTransaction(db))
