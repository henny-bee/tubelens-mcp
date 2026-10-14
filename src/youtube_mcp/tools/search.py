from __future__ import annotations

from typing import Any

from youtube_mcp.security.authorization import Scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.services.youtube import YouTubeService
from youtube_mcp.tools import run_protected_tool


def register_search_tools(
    mcp: Any,
    *,
    youtube_service: YouTubeService,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    async def youtube_search_videos(
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search YouTube videos by keyword query (bounded to at most 20 results)."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = await youtube_service.search_videos(query, limit=limit)
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_search_videos",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    mcp.tool()(youtube_search_videos)
    return {"youtube_search_videos": youtube_search_videos}
