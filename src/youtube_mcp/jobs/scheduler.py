from __future__ import annotations

import asyncio
import logging
from typing import Any

from youtube_mcp.config import Settings
from youtube_mcp.jobs.cleanup import CleanupManager
from youtube_mcp.jobs.persistence import JobRepository
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.job import JobKind, JobRecord, JobStatus
from youtube_mcp.services.ffmpeg import FfmpegService
from youtube_mcp.services.yt_dlp_adapter import YtDlpAdapter
from youtube_mcp.storage.artifacts import ArtifactRepository
from youtube_mcp.storage.cache import CacheRepository, compute_cache_key
from youtube_mcp.workers.subprocess_runner import SubprocessAsrRunner

logger = logging.getLogger("youtube_mcp")


class JobScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        job_repo: JobRepository,
        cache_repo: CacheRepository,
        artifact_repo: ArtifactRepository,
        cleanup: CleanupManager,
        yt_dlp: YtDlpAdapter,
        ffmpeg: FfmpegService,
        asr_runner: SubprocessAsrRunner,
    ) -> None:
        self._settings = settings
        self._job_repo = job_repo
        self._cache_repo = cache_repo
        self._artifact_repo = artifact_repo
        self._cleanup = cleanup
        self._yt_dlp = yt_dlp
        self._ffmpeg = ffmpeg
        self._asr_runner = asr_runner
        self._wake_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._stopping = False

    def notify(self) -> None:
        self._wake_event.set()

    async def start(self) -> None:
        self._stopping = False
        self._job_repo.reconcile_on_startup(auto_requeue_idempotent=True)
        self._cleanup.run_maintenance(clean_tmp=True)
        self._wake_event.set()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._run_loop())
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        self._stopping = True
        self._wake_event.set()
        for task in (self._worker_task, self._maintenance_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _maintenance_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._settings.cleanup_interval_seconds)
            try:
                self._cleanup.run_maintenance(clean_tmp=False)
            except Exception as exc:
                logger.warning("Maintenance cleanup error: %s", exc)

    async def _run_loop(self) -> None:
        while not self._stopping:
            job = self._job_repo.claim_next_queued()
            if job is None:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue

            await self.execute_claimed_job(job)

    async def execute_claimed_job(self, job: JobRecord) -> JobRecord:
        workspace = (self._settings.tmp_dir / f"job_{job.id}").resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            if job.kind == JobKind.ASR_TRANSCRIBE:
                return await self._execute_asr_job(job, workspace)
            if job.kind == JobKind.MEDIA_DOWNLOAD:
                return await self._execute_media_job(job, workspace)
            raise AppError(ErrorCode.UNSUPPORTED_OPERATION, f"Unknown job kind: {job.kind}")
        except AppError as exc:
            if exc.code == ErrorCode.JOB_CANCELLED:
                return self._job_repo.get(job.id)
            return self._job_repo.mark_failed(job.id, exc.code.value, exc.message)
        except Exception as exc:
            logger.error("Unexpected job failure: %s", exc, extra={"job_id": job.id})
            return self._job_repo.mark_failed(
                job.id,
                ErrorCode.INTERNAL_ERROR.value,
                "Unexpected internal worker error.",
            )
        finally:
            self._cleanup.clean_job_workspace(job.id)

    async def _execute_asr_job(self, job: JobRecord, workspace: Any) -> JobRecord:
        self._cache_repo.enforce_storage_budget(0)
        canonical_url = f"https://www.youtube.com/watch?v={job.video_id}"
        requested_lang = str(job.options.get("language") or "")

        info = await self._yt_dlp.extract_video_info(job.video_id, canonical_url)
        if info.is_live:
            raise AppError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "Live stream transcription is not supported.",
                details={"video_id": job.video_id},
            )
        if (
            info.duration_seconds is not None
            and info.duration_seconds > self._settings.max_asr_duration_seconds
        ):
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                f"Video duration ({info.duration_seconds}s) exceeds maximum ASR limit ({self._settings.max_asr_duration_seconds}s).",
                details={"video_id": job.video_id, "duration_seconds": info.duration_seconds},
            )

        self._job_repo.update_progress(job.id, 20.0)
        raw_audio = await self._yt_dlp.download_audio_for_asr(
            job.video_id, canonical_url, workspace
        )

        self._job_repo.update_progress(job.id, 45.0)
        wav_path = workspace / f"{job.video_id}_16k.wav"
        await self._ffmpeg.convert_to_16k_mono_wav(raw_audio, wav_path)
        raw_audio.unlink(missing_ok=True)

        self._job_repo.update_progress(job.id, 60.0)
        output_json = workspace / f"{job.video_id}_asr.json"
        record = await self._asr_runner.run(
            job_id=job.id,
            video_id=job.video_id,
            audio_wav_path=wav_path,
            output_json_path=output_json,
            language=requested_lang or None,
        )

        cache_key = compute_cache_key(
            provider="asr",
            video_id=job.video_id,
            language=record.language,
            source_policy="whisper_asr",
            version=record.extractor_version,
        )
        self._cache_repo.put(
            key=cache_key,
            kind="asr",
            video_id=job.video_id,
            payload=record.model_dump(),
            language=record.language,
            source=record.source.value,
            extractor_version=record.extractor_version,
        )
        return self._job_repo.mark_succeeded(job.id, cache_key)

    async def _execute_media_job(self, job: JobRecord, workspace: Any) -> JobRecord:
        if not self._settings.enable_media_download:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Media download is disabled by operator policy.",
            )
        self._cache_repo.enforce_storage_budget(0)
        canonical_url = f"https://www.youtube.com/watch?v={job.video_id}"
        info = await self._yt_dlp.extract_video_info(job.video_id, canonical_url)
        if info.is_live:
            raise AppError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "Live stream media acquisition is not supported.",
            )
        if (
            info.duration_seconds is not None
            and info.duration_seconds > self._settings.max_asr_duration_seconds
        ):
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                f"Video duration ({info.duration_seconds}s) exceeds maximum allowed limit ({self._settings.max_asr_duration_seconds}s).",
            )

        self._job_repo.update_progress(job.id, 30.0)
        kind = str(job.options.get("kind") or "audio")
        format_preset = str(job.options.get("format_preset") or "m4a")
        file_path, mime, ext = await self._yt_dlp.download_authorized_media(
            job.video_id,
            canonical_url,
            kind=kind,
            format_preset=format_preset,
            work_dir=workspace,
        )
        self._job_repo.update_progress(job.id, 85.0)
        artifact = self._artifact_repo.register_file(
            job_id=job.id,
            source_file=file_path,
            mime=mime,
            extension=ext,
        )
        return self._job_repo.mark_succeeded(job.id, artifact.id)
