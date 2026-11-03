"""Persistent job state, heartbeat recovery, and the single-heavy-job gate."""


class HeavyJobBusy(RuntimeError):
    """Raised when a second resource-heavy task is requested."""


def bounded_error(error: BaseException | str, limit: int = 2000) -> str:
    if isinstance(error, str):
        summary = error
    else:
        summary = f"{type(error).__name__}: {error}"
    return summary[:limit]


class JobManager:
    PAID_OR_GENERATIVE_TYPES = {
        "edit_intent",
        "ai_storyboard",
        "recommend_storyboard",
        "generate_broll",
        "seedance",
        "tts",
        "visual_analysis",
        "delivery_copy_suggestion",
        "content_review",
        "audio_review",
        "creative_visual_advice",
        "packaging_suggestion",
    }
    HEAVY_TYPES = {
        "scan",
        "probe",
        "proxy",
        "render",
        "preview",
        "generate_preview",
        "audio",
        "segments",
        "privacy",
        "transcribe",
        "recover_storyboard",
        "sync_sources",
        "initial_source_check",
        "source_continue",
        "render_and_qa",
        "build_publish_package",
        "packaging_preview",
        "packaging_catalog_check",
        "packaging_audio_analysis",
        "scene_detection",
    } | PAID_OR_GENERATIVE_TYPES

    def __init__(
        self,
        db,
        job_id: str,
        job_type: str,
        library_id: str | None = None,
    ):
        self.db = db
        self.job_id = job_id
        self.job_type = job_type
        self.library_id = library_id

    def start(self) -> None:
        if self.job_type in self.HEAVY_TYPES:
            raise HeavyJobBusy(
                "重任务必须通过持久队列启动，不能绕过队首、租约和重试门禁。"
            )
        connection = self.db.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            same_job = connection.execute(
                "SELECT status, cursor, retry_policy FROM jobs WHERE job_id=?",
                (self.job_id,),
            ).fetchone()
            if same_job and same_job["retry_policy"] == "never":
                raise HeavyJobBusy(
                    "该任务禁止重试，不能通过旧入口重新启动。"
                )
            if same_job and same_job["status"] == "running":
                raise HeavyJobBusy(
                    "这个任务已经在运行。请等待完成，或在确认旧任务已停止后重试。"
                )
            resume_cursor = (
                same_job["cursor"]
                if same_job
                and same_job["status"] in {"failed", "interrupted", "stale"}
                else None
            )
            connection.execute(
                """
                INSERT INTO jobs(
                  job_id, job_type, library_id, status, cursor,
                  heartbeat_at, created_at, updated_at
                ) VALUES(
                  ?, ?, ?, 'running', ?, datetime('now'), datetime('now'), datetime('now')
                )
                ON CONFLICT(job_id) DO UPDATE SET
                  job_type=excluded.job_type,
                  library_id=excluded.library_id,
                  status='running',
                  cursor=excluded.cursor,
                  heartbeat_at=datetime('now'),
                  updated_at=datetime('now'),
                  error_summary=NULL
                """,
                (self.job_id, self.job_type, self.library_id, resume_cursor),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def heartbeat(self, cursor: str | None = None) -> None:
        with self.db.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET cursor=?, heartbeat_at=datetime('now'), updated_at=datetime('now')
                WHERE job_id=? AND status='running'
                """,
                (cursor, self.job_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"任务当前不可更新进度：{self.job_id}")

    def resume_cursor(self) -> str | None:
        row = self.db.connection.execute(
            "SELECT cursor FROM jobs WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        return None if row is None else row["cursor"]

    def complete(self) -> None:
        self._finish("completed", None)

    def fail(self, error: BaseException | str) -> None:
        self._finish("failed", bounded_error(error))

    def interrupt(self, error: BaseException | str = "任务被中断") -> None:
        self._finish("interrupted", bounded_error(error))

    def recover_interrupted(self, max_age_seconds: int) -> int:
        if self.job_type in self.HEAVY_TYPES:
            raise HeavyJobBusy("重任务恢复必须通过持久队列租约执行。")
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds 不能小于 0")
        modifier = f"-{int(max_age_seconds)} seconds"
        with self.db.transaction() as connection:
            return connection.execute(
                """
                UPDATE jobs
                SET status='interrupted',
                    error_summary='任务意外停止，可从上次进度继续',
                    updated_at=datetime('now')
                WHERE status='running'
                  AND job_id=?
                  AND heartbeat_at < datetime('now', ?)
                """,
                (self.job_id, modifier),
            ).rowcount

    def _finish(self, status: str, error_summary: str | None) -> None:
        with self.db.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET status=?, error_summary=?, heartbeat_at=datetime('now'),
                    updated_at=datetime('now')
                WHERE job_id=? AND status='running'
                """,
                (status, error_summary, self.job_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"任务当前不可结束：{self.job_id}")
