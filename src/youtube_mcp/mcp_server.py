from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import ASGIApp

from youtube_mcp.config import Settings, configure_logging
from youtube_mcp.jobs.cleanup import CleanupManager
from youtube_mcp.jobs.manager import JobManager
from youtube_mcp.jobs.persistence import JobRepository
from youtube_mcp.jobs.scheduler import JobScheduler
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.security.audit import AuditLogger
from youtube_mcp.security.auth import AuthMiddleware, TokenAuthenticator
from youtube_mcp.security.authorization import Scope, require_scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.services.ffmpeg import FfmpegService
from youtube_mcp.services.media import MediaService
from youtube_mcp.services.transcript_api_adapter import TranscriptApiAdapter
from youtube_mcp.services.transcript_resolver import TranscriptResolver
from youtube_mcp.services.youtube import YouTubeService
from youtube_mcp.services.yt_dlp_adapter import YtDlpAdapter
from youtube_mcp.storage.artifacts import ArtifactRepository
from youtube_mcp.storage.cache import CacheRepository
from youtube_mcp.storage.db import Database
from youtube_mcp.storage.migrations import apply_migrations
from youtube_mcp.tools.captions import register_caption_tools
from youtube_mcp.tools.channel import register_channel_tools
from youtube_mcp.tools.comments import register_comment_tools
from youtube_mcp.tools.jobs import register_job_tools
from youtube_mcp.tools.media import register_media_tools
from youtube_mcp.tools.playlist import register_playlist_tools
from youtube_mcp.tools.search import register_search_tools
from youtube_mcp.tools.transcript import register_transcript_tools
from youtube_mcp.tools.video import register_video_tools
from youtube_mcp.workers.subprocess_runner import SubprocessAsrRunner


def _create_mcp_instance(name: str) -> Any:
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name)
    except ImportError:
        from fastmcp import FastMCP  # type: ignore[import-not-found]

        return FastMCP(name)


@dataclass
class AppRuntime:
    settings: Settings
    db: Database
    cache_repo: CacheRepository
    artifact_repo: ArtifactRepository
    job_repo: JobRepository
    cleanup: CleanupManager
    audit: AuditLogger
    rate_limiter: RateLimiter
    authenticator: TokenAuthenticator
    yt_dlp: YtDlpAdapter
    transcript_api: TranscriptApiAdapter
    ffmpeg: FfmpegService
    asr_runner: SubprocessAsrRunner
    scheduler: JobScheduler
    job_manager: JobManager
    transcript_resolver: TranscriptResolver
    youtube_service: YouTubeService
    media_service: MediaService
    mcp: Any
    tools: dict[str, Any]


def build_runtime(
    settings: Settings,
    *,
    yt_dlp: YtDlpAdapter | None = None,
    transcript_api: TranscriptApiAdapter | None = None,
    ffmpeg: FfmpegService | None = None,
    asr_runner: SubprocessAsrRunner | None = None,
) -> AppRuntime:
    configure_logging(settings)
    settings.ensure_directories()

    db = Database(settings.db_path)
    apply_migrations(db)

    cache_repo = CacheRepository(db, settings)
    artifact_repo = ArtifactRepository(db, settings, cache_repo)
    job_repo = JobRepository(db, settings)
    cleanup = CleanupManager(settings, cache_repo, artifact_repo, job_repo)
    audit = AuditLogger(db)
    rate_limiter = RateLimiter(
        requests_per_minute=settings.rate_limit_requests_per_minute,
        max_concurrent=settings.max_concurrent_tool_calls,
    )
    authenticator = TokenAuthenticator(settings)

    yt_dlp_inst = yt_dlp or YtDlpAdapter(settings)
    transcript_api_inst = transcript_api or TranscriptApiAdapter(settings)
    ffmpeg_inst = ffmpeg or FfmpegService(settings)
    asr_runner_inst = asr_runner or SubprocessAsrRunner(settings)

    scheduler = JobScheduler(
        settings=settings,
        job_repo=job_repo,
        cache_repo=cache_repo,
        artifact_repo=artifact_repo,
        cleanup=cleanup,
        yt_dlp=yt_dlp_inst,
        ffmpeg=ffmpeg_inst,
        asr_runner=asr_runner_inst,
    )
    job_manager = JobManager(
        settings=settings,
        job_repo=job_repo,
        scheduler=scheduler,
        asr_runner=asr_runner_inst,
        cleanup=cleanup,
        audit=audit,
    )
    transcript_resolver = TranscriptResolver(
        settings=settings,
        cache_repo=cache_repo,
        transcript_api=transcript_api_inst,
        yt_dlp=yt_dlp_inst,
        job_manager=job_manager,
    )
    youtube_service = YouTubeService(
        settings=settings,
        cache_repo=cache_repo,
        yt_dlp=yt_dlp_inst,
    )
    media_service = MediaService(
        settings=settings,
        cache_repo=cache_repo,
        artifact_repo=artifact_repo,
        yt_dlp=yt_dlp_inst,
        job_manager=job_manager,
    )

    mcp = _create_mcp_instance("tubelens-mcp")
    tools: dict[str, Any] = {}
    tools.update(
        register_video_tools(mcp, youtube_service=youtube_service, rate_limiter=rate_limiter)
    )
    tools.update(
        register_caption_tools(
            mcp, transcript_resolver=transcript_resolver, rate_limiter=rate_limiter
        )
    )
    tools.update(
        register_transcript_tools(
            mcp, transcript_resolver=transcript_resolver, rate_limiter=rate_limiter
        )
    )
    tools.update(
        register_search_tools(mcp, youtube_service=youtube_service, rate_limiter=rate_limiter)
    )
    tools.update(
        register_playlist_tools(mcp, youtube_service=youtube_service, rate_limiter=rate_limiter)
    )
    tools.update(
        register_channel_tools(mcp, youtube_service=youtube_service, rate_limiter=rate_limiter)
    )
    tools.update(
        register_comment_tools(mcp, youtube_service=youtube_service, rate_limiter=rate_limiter)
    )
    tools.update(
        register_media_tools(
            mcp,
            youtube_service=youtube_service,
            media_service=media_service,
            rate_limiter=rate_limiter,
        )
    )
    tools.update(register_job_tools(mcp, job_manager=job_manager, rate_limiter=rate_limiter))

    return AppRuntime(
        settings=settings,
        db=db,
        cache_repo=cache_repo,
        artifact_repo=artifact_repo,
        job_repo=job_repo,
        cleanup=cleanup,
        audit=audit,
        rate_limiter=rate_limiter,
        authenticator=authenticator,
        yt_dlp=yt_dlp_inst,
        transcript_api=transcript_api_inst,
        ffmpeg=ffmpeg_inst,
        asr_runner=asr_runner_inst,
        scheduler=scheduler,
        job_manager=job_manager,
        transcript_resolver=transcript_resolver,
        youtube_service=youtube_service,
        media_service=media_service,
        mcp=mcp,
        tools=tools,
    )


def create_asgi_app(runtime: AppRuntime) -> ASGIApp:
    mcp = runtime.mcp

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"], include_in_schema=False)
    async def readyz(_request: Request) -> Response:
        db_ok = runtime.db.check_health()
        status_code = 200 if db_ok else 503
        return JSONResponse(
            {
                "status": "ready" if db_ok else "degraded",
                "database": "ok" if db_ok else "error",
                "queue_size": runtime.job_repo.active_queue_count(),
                "whisper_enabled": runtime.settings.whisper_enabled,
            },
            status_code=status_code,
        )

    @mcp.custom_route("/artifacts/{artifact_id}", methods=["GET"], include_in_schema=False)
    async def download_artifact(request: Request) -> Response:
        try:
            principal = require_scope(Scope.MEDIA_DOWNLOAD)
            artifact_id = request.path_params.get("artifact_id", "")
            meta = runtime.artifact_repo.get(artifact_id=artifact_id)
            runtime.audit.record(
                principal_id=principal.principal_id,
                action="download_artifact",
                object_id=meta.id,
                status="served",
            )
            file_path = Path(meta.path)
            return FileResponse(
                path=file_path,
                media_type=meta.mime,
                filename=file_path.name,
            )
        except AppError as exc:
            http_status = 404
            if exc.code == ErrorCode.UNAUTHORIZED:
                http_status = 401
            elif exc.code == ErrorCode.FORBIDDEN:
                http_status = 403
            elif exc.code == ErrorCode.INVALID_ID:
                http_status = 400
            return JSONResponse(exc.to_response(), status_code=http_status)

    starlette_app: Starlette = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
    )

    orig_lifespan = starlette_app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(app: Starlette) -> AsyncIterator[None]:
        await runtime.scheduler.start()
        try:
            async with orig_lifespan(app):
                yield
        finally:
            await runtime.scheduler.stop()

    starlette_app.router.lifespan_context = combined_lifespan
    return AuthMiddleware(starlette_app, runtime.authenticator)
