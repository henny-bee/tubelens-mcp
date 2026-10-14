from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from youtube_mcp.schemas.common import AppError, ErrorCode, ErrorResponse
from youtube_mcp.security.auth import get_request_id
from youtube_mcp.security.authorization import Scope, require_scope
from youtube_mcp.security.rate_limits import RateLimiter

logger = logging.getLogger("youtube_mcp")


async def run_protected_tool(
    *,
    tool_name: str,
    scope: Scope,
    rate_limiter: RateLimiter,
    action: Callable[[str], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    start_ts = time.perf_counter()
    req_id = get_request_id()
    principal_id = "anonymous"
    try:
        principal = require_scope(scope)
        principal_id = principal.principal_id
        async with rate_limiter.guard(principal_id):
            result = await action(principal_id)
        elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        logger.info(
            "Tool call completed",
            extra={
                "request_id": req_id,
                "principal_id": principal_id,
                "tool": tool_name,
                "elapsed_ms": elapsed_ms,
                "cache_hit": result.get("cached"),
                "job_id": result.get("job_id"),
                "job_state": result.get("job_state"),
                "video_id": result.get("video_id"),
            },
        )
        return result
    except AppError as exc:
        elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        logger.warning(
            "Tool call rejected: %s",
            exc.message,
            extra={
                "request_id": req_id,
                "principal_id": principal_id,
                "tool": tool_name,
                "elapsed_ms": elapsed_ms,
                "error_code": exc.code.value,
            },
        )
        return exc.to_response()
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        logger.error(
            "Unhandled tool error: %s",
            exc.__class__.__name__,
            extra={
                "request_id": req_id,
                "principal_id": principal_id,
                "tool": tool_name,
                "elapsed_ms": elapsed_ms,
                "error_code": ErrorCode.INTERNAL_ERROR.value,
            },
        )
        return ErrorResponse(
            code=ErrorCode.INTERNAL_ERROR,
            message="An unexpected internal server error occurred.",
            retryable=False,
        ).model_dump()
