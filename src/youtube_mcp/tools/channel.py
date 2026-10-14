from __future__ import annotations

from typing import Any

from youtube_mcp.security.authorization import Scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.services.youtube import YouTubeService
from youtube_mcp.tools import run_protected_tool


def register_channel_tools(
    mcp: Any,
    *,
    youtube_service: YouTubeService,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    async def youtube_list_channel(
        channel_url: str,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List recent videos from a YouTube channel with cursor pagination (limit <= 50)."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = await youtube_service.list_channel(
                channel_url, cursor=cursor, limit=limit
            )
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_list_channel",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    mcp.tool()(youtube_list_channel)
    return {"youtube_list_channel": youtube_list_channel}
