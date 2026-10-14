from __future__ import annotations

from typing import Any

from youtube_mcp.security.authorization import Scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.services.transcript_resolver import TranscriptResolver
from youtube_mcp.tools import run_protected_tool


def register_caption_tools(
    mcp: Any,
    *,
    transcript_resolver: TranscriptResolver,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    async def youtube_list_captions(url_or_id: str) -> dict[str, Any]:
        """List available manual and auto-generated caption tracks for a YouTube video."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = await transcript_resolver.list_captions(url_or_id)
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_list_captions",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    mcp.tool()(youtube_list_captions)
    return {"youtube_list_captions": youtube_list_captions}
