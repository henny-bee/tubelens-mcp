from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

from youtube_mcp.config import Settings
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.job import JobKind, JobRecord, JobStatus
from youtube_mcp.storage.db import Database


def compute_job_key(
    *,
    video_id: str,
    kind: JobKind,
    language: str = "",
    model: str = "",
    options: dict[str, Any] | None = None,
) -> str:
    opts_serialized = json.dumps(options or {}, sort_keys=True)
    raw = f"{video_id}|{kind.value}|{language}|{model}|{opts_serialized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class JobRepository:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            principal_id=str(row["principal_id"]),
            kind=JobKind(str(row["kind"])),
            job_key=str(row["job_key"]),
            video_id=str(row["video_id"]),
            options=json.loads(str(row["options_json"] or "{}")),
            status=JobStatus(str(row["status"])),
            progress=float(row["progress"] or 0.0),
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=float(row["created_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            completed_at=float(row["completed_at"]) if row["completed_at"] is not None else None,
            attempts=int(row["attempts"] or 0),
            result_ref=row["result_ref"],
            expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
        )

    def get(self, job_id: str) -> JobRecord:
        row = self._db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise AppError(
                ErrorCode.JOB_NOT_FOUND,
                f"Job '{job_id}' does not exist.",
                details={"job_id": job_id},
            )
        return self._row_to_record(row)

    def find_by_key(self, job_key: str) -> JobRecord | None:
        row = self._db.fetch_one("SELECT * FROM jobs WHERE job_key = ?", (job_key,))
        return self._row_to_record(row) if row else None

    def active_queue_count(self) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM jobs WHERE status IN (?, ?)",
            (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
        )
        return int(row["cnt"]) if row else 0

    def enqueue_or_get(
        self,
        *,
        principal_id: str,
        kind: JobKind,
        video_id: str,
        job_key: str,
        options: dict[str, Any] | None = None,
    ) -> tuple[JobRecord, bool]:
        now = time.time()
        ttl = (
            self._settings.asr_cache_ttl_seconds
            if kind == JobKind.ASR_TRANSCRIBE
            else self._settings.artifact_ttl_seconds
        )
        expires_at = now + ttl
        opts_json = json.dumps(options or {}, sort_keys=True)

        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE job_key = ?",
                (job_key,),
            ).fetchone()

            if existing is not None:
                rec = self._row_to_record(existing)
                if rec.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    return rec, False
                if rec.status == JobStatus.SUCCEEDED and (
                    rec.expires_at is None or rec.expires_at > now
                ):
                    return rec, False

                cnt_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM jobs WHERE status IN (?, ?)",
                    (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
                ).fetchone()
                if int(cnt_row["cnt"]) >= self._settings.max_queue_size:
                    raise AppError(
                        ErrorCode.RESOURCE_LIMIT,
                        f"Job queue is full (max {self._settings.max_queue_size} jobs).",
                        retryable=True,
                    )

                conn.execute(
                    """
                    UPDATE jobs
                    SET principal_id = ?,
                        options_json = ?,
                        status = ?,
                        progress = 0.0,
                        error_code = NULL,
                        error_message = NULL,
                        created_at = ?,
                        started_at = NULL,
                        completed_at = NULL,
                        attempts = 0,
                        result_ref = NULL,
                        expires_at = ?
                    WHERE id = ?
                    """,
                    (
                        principal_id,
                        opts_json,
                        JobStatus.QUEUED.value,
                        now,
                        expires_at,
                        rec.id,
                    ),
                )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (rec.id,)).fetchone()
                return self._row_to_record(updated), True

            cnt_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM jobs WHERE status IN (?, ?)",
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchone()
            if int(cnt_row["cnt"]) >= self._settings.max_queue_size:
                raise AppError(
                    ErrorCode.RESOURCE_LIMIT,
                    f"Job queue is full (max {self._settings.max_queue_size} jobs).",
                    retryable=True,
                )

            job_id = f"job_{uuid.uuid4().hex[:20]}"
            conn.execute(
                """
                INSERT INTO jobs (
                    id, principal_id, kind, job_key, video_id, options_json,
                    status, progress, error_code, error_message,
                    created_at, started_at, completed_at, attempts, result_ref, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, NULL, NULL, ?, NULL, NULL, 0, NULL, ?)
                """,
                (
                    job_id,
                    principal_id,
                    kind.value,
                    job_key,
                    video_id,
                    opts_json,
                    JobStatus.QUEUED.value,
                    now,
                    expires_at,
                ),
            )
            created = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_record(created), True

    def claim_next_queued(self) -> JobRecord | None:
        now = time.time()
        with self._db.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None

            job_id = str(row["id"])
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    started_at = ?,
                    progress = 5.0,
                    attempts = attempts + 1
                WHERE id = ? AND status = ?
                """,
                (JobStatus.RUNNING.value, now, job_id, JobStatus.QUEUED.value),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_record(claimed) if claimed else None

    def update_progress(self, job_id: str, progress: float) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET progress = ? WHERE id = ? AND status = ?",
                (max(0.0, min(100.0, progress)), job_id, JobStatus.RUNNING.value),
            )

    def mark_succeeded(self, job_id: str, result_ref: str) -> JobRecord:
        now = time.time()
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    progress = 100.0,
                    completed_at = ?,
                    result_ref = ?,
                    error_code = NULL,
                    error_message = NULL
                WHERE id = ?
                """,
                (JobStatus.SUCCEEDED.value, now, result_ref, job_id),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_record(row)

    def mark_failed(self, job_id: str, error_code: str, error_message: str) -> JobRecord:
        now = time.time()
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    completed_at = ?,
                    error_code = ?,
                    error_message = ?
                WHERE id = ? AND status != ?
                """,
                (
                    JobStatus.FAILED.value,
                    now,
                    error_code,
                    error_message[:500],
                    job_id,
                    JobStatus.CANCELLED.value,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_record(row)

    def mark_cancelled(self, job_id: str) -> tuple[JobRecord, bool]:
        now = time.time()
        with self._db.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise AppError(
                    ErrorCode.JOB_NOT_FOUND,
                    f"Job '{job_id}' does not exist.",
                    details={"job_id": job_id},
                )
            current = self._row_to_record(row)
            if current.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
                return current, False

            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    completed_at = ?,
                    error_code = ?,
                    error_message = 'Cancelled by operator or client request.'
                WHERE id = ?
                """,
                (JobStatus.CANCELLED.value, now, ErrorCode.JOB_CANCELLED.value, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_record(updated), True

    def reconcile_on_startup(self, *, auto_requeue_idempotent: bool = True) -> dict[str, int]:
        now = time.time()
        requeued = 0
        interrupted = 0
        with self._db.transaction() as conn:
            running_rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ?",
                (JobStatus.RUNNING.value,),
            ).fetchall()

            for row in running_rows:
                job_id = str(row["id"])
                kind = str(row["kind"])
                attempts = int(row["attempts"] or 1)
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        error_code = ?,
                        error_message = 'Process restarted while job was running.'
                    WHERE id = ?
                    """,
                    (JobStatus.INTERRUPTED.value, ErrorCode.INTERNAL_ERROR.value, job_id),
                )
                interrupted += 1

                if (
                    auto_requeue_idempotent
                    and kind == JobKind.ASR_TRANSCRIBE.value
                    and attempts < self._settings.max_job_attempts
                ):
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?,
                            progress = 0.0,
                            started_at = NULL,
                            error_code = NULL,
                            error_message = NULL
                        WHERE id = ?
                        """,
                        (JobStatus.QUEUED.value, job_id),
                    )
                    requeued += 1
                    interrupted -= 1
                else:
                    conn.execute(
                        "UPDATE jobs SET completed_at = ? WHERE id = ?",
                        (now, job_id),
                    )
        return {"interrupted": interrupted, "requeued": requeued}

    def purge_expired(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                DELETE FROM jobs
                WHERE expires_at IS NOT NULL
                  AND expires_at <= ?
                  AND status IN (?, ?, ?, ?)
                """,
                (
                    ts,
                    JobStatus.SUCCEEDED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.INTERRUPTED.value,
                ),
            )
            return cur.rowcount or 0
