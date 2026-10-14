from __future__ import annotations

from typing import Any

from youtube_mcp.security.authorization import Scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.services.youtube import YouTubeService
from youtube_mcp.tools import run_protected_tool


def register_comment_tools(
    mcp: Any,
    *,
    youtube_service: YouTubeService,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    async def youtube_get_comments(
        url_or_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Fetch top comments for a YouTube video on a best-effort basis (limit <= 100)."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = await youtube_service.get_comments(url_or_id, limit=limit)
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_get_comments",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    mcp.tool()(youtube_get_comments)
    return {"youtube_get_comments": youtube_get_comments}
