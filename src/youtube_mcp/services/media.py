from __future__ import annotations

from typing import Any

from youtube_mcp.config import Settings
from youtube_mcp.jobs.manager import JobManager
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.job import ArtifactResponse
from youtube_mcp.services.url_validator import parse_video_id
from youtube_mcp.services.yt_dlp_adapter import YtDlpAdapter
from youtube_mcp.storage.artifacts import ArtifactRepository
from youtube_mcp.storage.cache import CacheRepository, compute_cache_key


class MediaService:
    def __init__(
        self,
        *,
        settings: Settings,
        cache_repo: CacheRepository,
        artifact_repo: ArtifactRepository,
        yt_dlp: YtDlpAdapter,
        job_manager: JobManager,
    ) -> None:
        self._settings = settings
        self._cache_repo = cache_repo
        self._artifact_repo = artifact_repo
        self._yt_dlp = yt_dlp
        self._job_manager = job_manager

    async def schedule_download(
        self,
        *,
        principal_id: str,
        url_or_id: str,
        kind: str = "audio",
        format_preset: str = "m4a",
        confirm_authorized: bool = False,
    ) -> dict[str, Any]:
        if not self._settings.enable_media_download:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Media download is disabled by operator policy (ENABLE_MEDIA_DOWNLOAD=false).",
            )
        if not confirm_authorized:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Explicit confirmation of rights (confirm_authorized=true) is required.",
            )

        target = parse_video_id(url_or_id)
        meta_key = compute_cache_key(provider="yt_dlp_metadata", video_id=target.video_id)
        cached_meta = self._cache_repo.get(meta_key)
        known_duration: int | None = None
        if cached_meta and cached_meta.get("duration_seconds") is not None:
            known_duration = int(cached_meta["duration_seconds"])
        else:
            info = await self._yt_dlp.extract_video_info(target.video_id, target.canonical_url)
            self._cache_repo.put(
                key=meta_key,
                kind="metadata",
                video_id=target.video_id,
                payload=info.model_dump(),
                extractor_version=info.extractor_version,
            )
            if info.is_live:
                raise AppError(
                    ErrorCode.UNSUPPORTED_OPERATION,
                    "Live stream media acquisition is not supported.",
                )
            known_duration = info.duration_seconds

        job = self._job_manager.submit_media_job(
            principal_id=principal_id,
            video_id=target.video_id,
            kind=kind,
            format_preset=format_preset,
            confirm_authorized=confirm_authorized,
            known_duration_seconds=known_duration,
        )
        return {
            "status": "pending" if job.status.value in ("queued", "running") else "ready",
            "video_id": target.video_id,
            "job_id": job.id,
            "job_state": job.status.value,
            "message": "Authorized media job scheduled.",
            "poll_tool": "youtube_get_job_status",
        }

    def get_artifact_metadata(
        self,
        *,
        job_id: str | None = None,
        artifact_id: str | None = None,
    ) -> ArtifactResponse:
        meta = self._artifact_repo.get(artifact_id=artifact_id, job_id=job_id)
        download_url = f"{self._settings.mcp_public_base_url}/artifacts/{meta.id}"
        return ArtifactResponse(
            artifact_id=meta.id,
            job_id=meta.job_id,
            mime=meta.mime,
            size_bytes=meta.size_bytes,
            expires_at=meta.expires_at,
            download_url=download_url,
        )
