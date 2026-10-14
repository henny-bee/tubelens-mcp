from __future__ import annotations

from typing import Any

from youtube_mcp.config import Settings
from youtube_mcp.jobs.cleanup import CleanupManager
from youtube_mcp.jobs.persistence import JobRepository, compute_job_key
from youtube_mcp.jobs.scheduler import JobScheduler
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.job import (
    JobCancelResponse,
    JobKind,
    JobRecord,
    JobStatusResponse,
)
from youtube_mcp.security.audit import AuditLogger
from youtube_mcp.workers.subprocess_runner import SubprocessAsrRunner


class JobManager:
    def __init__(
        self,
        *,
        settings: Settings,
        job_repo: JobRepository,
        scheduler: JobScheduler,
        asr_runner: SubprocessAsrRunner,
        cleanup: CleanupManager,
        audit: AuditLogger,
    ) -> None:
        self._settings = settings
        self._job_repo = job_repo
        self._scheduler = scheduler
        self._asr_runner = asr_runner
        self._cleanup = cleanup
        self._audit = audit

    def submit_asr_job(
        self,
        *,
        principal_id: str,
        video_id: str,
        language: str = "",
        known_duration_seconds: int | None = None,
    ) -> JobRecord:
        if not self._settings.whisper_enabled:
            raise AppError(
                ErrorCode.CAPTION_UNAVAILABLE,
                "Captions are unavailable and Whisper ASR fallback is disabled.",
                details={"video_id": video_id},
            )
        if (
            known_duration_seconds is not None
            and known_duration_seconds > self._settings.max_asr_duration_seconds
        ):
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                f"Video duration ({known_duration_seconds}s) exceeds maximum ASR limit ({self._settings.max_asr_duration_seconds}s).",
                details={"video_id": video_id, "duration_seconds": known_duration_seconds},
            )

        options: dict[str, Any] = {
            "language": language,
            "model": self._settings.whisper_model,
            "compute_type": self._settings.whisper_compute_type,
        }
        job_key = compute_job_key(
            video_id=video_id,
            kind=JobKind.ASR_TRANSCRIBE,
            language=language,
            model=self._settings.whisper_model,
            options=options,
        )
        record, created = self._job_repo.enqueue_or_get(
            principal_id=principal_id,
            kind=JobKind.ASR_TRANSCRIBE,
            video_id=video_id,
            job_key=job_key,
            options=options,
        )
        self._audit.record(
            principal_id=principal_id,
            action="submit_asr_job",
            object_id=record.id,
            status="enqueued" if created else "deduplicated",
        )
        if created:
            self._scheduler.notify()
        return record

    def submit_media_job(
        self,
        *,
        principal_id: str,
        video_id: str,
        kind: str,
        format_preset: str,
        confirm_authorized: bool,
        known_duration_seconds: int | None = None,
    ) -> JobRecord:
        if not self._settings.enable_media_download:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Media download tool is disabled by operator policy (ENABLE_MEDIA_DOWNLOAD=false).",
                details={"video_id": video_id},
            )
        if not confirm_authorized:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Affirmative rights declaration (confirm_authorized=true) is required.",
                details={"video_id": video_id},
            )
        if kind not in ("audio", "video"):
            raise AppError(ErrorCode.INVALID_ID, "kind must be 'audio' or 'video'.")
        if format_preset not in ("mp3", "m4a", "wav", "webm", "mp4"):
            raise AppError(
                ErrorCode.INVALID_ID,
                "format_preset must be one of: mp3, m4a, wav, webm, mp4.",
            )
        if (
            known_duration_seconds is not None
            and known_duration_seconds > self._settings.max_asr_duration_seconds
        ):
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                f"Video duration ({known_duration_seconds}s) exceeds maximum allowed limit ({self._settings.max_asr_duration_seconds}s).",
                details={"video_id": video_id, "duration_seconds": known_duration_seconds},
            )

        options = {"kind": kind, "format_preset": format_preset}
        job_key = compute_job_key(
            video_id=video_id,
            kind=JobKind.MEDIA_DOWNLOAD,
            options=options,
        )
        record, created = self._job_repo.enqueue_or_get(
            principal_id=principal_id,
            kind=JobKind.MEDIA_DOWNLOAD,
            video_id=video_id,
            job_key=job_key,
            options=options,
        )
        self._audit.record(
            principal_id=principal_id,
            action="submit_media_job",
            object_id=record.id,
            status="enqueued" if created else "deduplicated",
        )
        if created:
            self._scheduler.notify()
        return record

    def get_status(self, job_id: str) -> JobStatusResponse:
        rec = self._job_repo.get(job_id)
        return JobStatusResponse(
            job_id=rec.id,
            kind=rec.kind,
            video_id=rec.video_id,
            job_state=rec.status,
            progress=rec.progress,
            error_code=rec.error_code,
            error_message=rec.error_message,
            result_ref=rec.result_ref,
            created_at=rec.created_at,
            started_at=rec.started_at,
            completed_at=rec.completed_at,
        )

    def cancel(self, *, principal_id: str, job_id: str) -> JobCancelResponse:
        rec, changed = self._job_repo.mark_cancelled(job_id)
        if changed:
            self._asr_runner.cancel(job_id)
            self._cleanup.clean_job_workspace(job_id)
            self._audit.record(
                principal_id=principal_id,
                action="cancel_job",
                object_id=job_id,
                status="cancelled",
            )
            return JobCancelResponse(
                job_id=rec.id,
                cancelled=True,
                job_state=rec.status,
                message="Job cancelled and temporary resources cleaned up.",
            )
        return JobCancelResponse(
            job_id=rec.id,
            cancelled=False,
            job_state=rec.status,
            message=f"Job is already in terminal state '{rec.status.value}'.",
        )
