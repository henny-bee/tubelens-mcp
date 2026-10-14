from __future__ import annotations

from typing import Any

from youtube_mcp.jobs.manager import JobManager
from youtube_mcp.security.authorization import Scope
from youtube_mcp.security.rate_limits import RateLimiter
from youtube_mcp.tools import run_protected_tool


def register_job_tools(
    mcp: Any,
    *,
    job_manager: JobManager,
    rate_limiter: RateLimiter,
) -> dict[str, Any]:
    async def youtube_get_job_status(job_id: str) -> dict[str, Any]:
        """Get current state, progress, error code, and result reference for an ASR or media job."""

        async def _op(_principal_id: str) -> dict[str, Any]:
            resp = job_manager.get_status(job_id)
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_get_job_status",
            scope=Scope.READ,
            rate_limiter=rate_limiter,
            action=_op,
        )

    async def youtube_cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a queued or running ASR/media job and terminate any active worker subprocess."""

        async def _op(principal_id: str) -> dict[str, Any]:
            resp = job_manager.cancel(principal_id=principal_id, job_id=job_id)
            return resp.model_dump()

        return await run_protected_tool(
            tool_name="youtube_cancel_job",
            scope=Scope.ADMIN_JOBS,
            rate_limiter=rate_limiter,
            action=_op,
        )

    mcp.tool()(youtube_get_job_status)
    mcp.tool()(youtube_cancel_job)
    return {
        "youtube_get_job_status": youtube_get_job_status,
        "youtube_cancel_job": youtube_cancel_job,
    }
