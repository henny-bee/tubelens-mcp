from __future__ import annotations

from typing import Any

from youtube_mcp.security.authorization import Scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.services.youtube import YouTubeService
from youtube_mcp.tools import run_protected_tool


def register_video_tools(
    mcp: Any,
    *,
    youtube_service: YouTubeService,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    async def youtube_get_video_info(
        url_or_id: str,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Get normalized YouTube video metadata (title, channel, duration, views, availability)."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = await youtube_service.get_video_info(url_or_id, refresh=refresh)
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_get_video_info",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    mcp.tool()(youtube_get_video_info)
    return {"youtube_get_video_info": youtube_get_video_info}
