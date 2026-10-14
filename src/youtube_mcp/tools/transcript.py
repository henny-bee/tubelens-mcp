from __future__ import annotations

from typing import Any

from youtube_mcp.security.authorization import Scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.services.transcript_resolver import TranscriptResolver
from youtube_mcp.tools import run_protected_tool


def register_transcript_tools(
    mcp: Any,
    *,
    transcript_resolver: TranscriptResolver,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    async def youtube_get_transcript(
        url_or_id: str,
        languages: list[str] | None = None,
        allow_asr: bool = True,
        allow_other_language: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Retrieve paginated timestamped transcript segments; enqueues Whisper ASR if captions are missing and allow_asr=True."""

        async def _op(principal_id: str) -> dict[str, Any]:
            resp = await transcript_resolver.get_transcript(
                url_or_id=url_or_id,
                principal_id=principal_id,
                languages=languages or ["id", "en"],
                allow_asr=allow_asr,
                allow_other_language=allow_other_language,
                offset=offset,
                limit=limit,
            )
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_get_transcript",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    async def youtube_get_transcript_window(
        video_id: str,
        start_sec: float,
        end_sec: float,
        max_chars: int = 12000,
        languages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Retrieve transcript segments within a specific [start_sec, end_sec] time window."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = await transcript_resolver.get_transcript_window(
                video_id_or_url=video_id,
                start_sec=start_sec,
                end_sec=end_sec,
                languages=languages or ["id", "en"],
                max_chars=max_chars,
            )
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_get_transcript_window",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    async def youtube_search_transcript(
        video_id: str,
        query: str,
        limit: int = 20,
        languages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search within a video's transcript and return matching segments with timestamps."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = await transcript_resolver.search_transcript(
                video_id_or_url=video_id,
                query=query,
                limit=limit,
                languages=languages or ["id", "en"],
            )
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_search_transcript",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    mcp.tool()(youtube_get_transcript)
    mcp.tool()(youtube_get_transcript_window)
    mcp.tool()(youtube_search_transcript)
    return {
        "youtube_get_transcript": youtube_get_transcript,
        "youtube_get_transcript_window": youtube_get_transcript_window,
        "youtube_search_transcript": youtube_search_transcript,
    }
