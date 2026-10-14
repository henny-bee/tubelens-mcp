from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import AsyncIterator

from youtube_mcp.schemas.common import AppError, ErrorCode


class RateLimiter:
    def __init__(self, requests_per_minute: int = 60, max_concurrent: int = 8) -> None:
        self._rpm = requests_per_minute
        self._max_concurrent = max_concurrent
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._in_flight: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def guard(self, principal_id: str) -> AsyncIterator[None]:
        now = time.monotonic()
        cutoff = now - 60.0
        async with self._lock:
            history = self._windows[principal_id]
            while history and history[0] < cutoff:
                history.popleft()
            if len(history) >= self._rpm:
                raise AppError(
                    ErrorCode.RATE_LIMITED,
                    f"Rate limit exceeded ({self._rpm} requests/minute).",
                    retryable=True,
                )
            if self._in_flight[principal_id] >= self._max_concurrent:
                raise AppError(
                    ErrorCode.RATE_LIMITED,
                    f"Concurrent tool call limit exceeded ({self._max_concurrent}).",
                    retryable=True,
                )
            history.append(now)
            self._in_flight[principal_id] += 1

        try:
            yield
        finally:
            async with self._lock:
                self._in_flight[principal_id] = max(0, self._in_flight[principal_id] - 1)
