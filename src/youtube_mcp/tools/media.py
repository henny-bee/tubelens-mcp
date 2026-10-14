from __future__ import annotations

from typing import Any

from youtube_mcp.security.authorization import Scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.services.media import MediaService
from youtube_mcp.services.youtube import YouTubeService
from youtube_mcp.tools import run_protected_tool


def register_media_tools(
    mcp: Any,
    *,
    youtube_service: YouTubeService,
    media_service: MediaService,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    async def youtube_get_formats(url_or_id: str) -> dict[str, Any]:
        """List available audio and video stream formats, codecs, and estimated sizes."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = await youtube_service.get_formats(url_or_id)
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_get_formats",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    async def youtube_download_media(
        url_or_id: str,
        kind: str = "audio",
        format: str = "m4a",
        confirm_authorized: bool = False,
    ) -> dict[str, Any]:
        """Schedule an asynchronous media acquisition job; requires ENABLE_MEDIA_DOWNLOAD=true, media:download scope, and confirm_authorized=true."""

        async def _op(principal_id: str) -> dict[str, Any]:
            return await media_service.schedule_download(
                principal_id=principal_id,
                url_or_id=url_or_id,
                kind=kind,
                format_preset=format,
                confirm_authorized=confirm_authorized,
            )

        return await run_protected_tool(
            tool_name="youtube_download_media",
            scope=Scope.MEDIA_DOWNLOAD,
            rate_limiter=rate_limiter,
            action=_op,
        )

    async def youtube_get_artifact(
        job_id: str | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve artifact metadata and short-lived authenticated download URL for a completed media job."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = media_service.get_artifact_metadata(
                job_id=job_id,
                artifact_id=artifact_id,
            )
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_get_artifact",
            scope=Scope.MEDIA_DOWNLOAD,
            rate_limiter=rate_limiter,
            action=_op,
        )

    mcp.tool()(youtube_get_formats)
    mcp.tool()(youtube_download_media)
    mcp.tool()(youtube_get_artifact)
    return {
        "youtube_get_formats": youtube_get_formats,
        "youtube_download_media": youtube_download_media,
        "youtube_get_artifact": youtube_get_artifact,
    }
