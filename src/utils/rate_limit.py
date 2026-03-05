"""非同步 Rate Limit 與退避工具。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Deque

from ..errors import RateLimitTimeoutError


@dataclass(slots=True)
class RateLimitConfig:
    requests: int
    per_seconds: float
    timeout: float | None = None


class AsyncRateLimiter:
    """簡易滑動視窗 Rate Limiter。"""

    def __init__(self, config: RateLimitConfig) -> None:
        self._requests = config.requests
        self._per = config.per_seconds
        self._timeout = config.timeout
        self._queue: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        start = time.perf_counter()
        waited = 0.0
        while True:
            async with self._lock:
                now = time.perf_counter()
                window_start = now - self._per
                while self._queue and self._queue[0] < window_start:
                    self._queue.popleft()

                if len(self._queue) < self._requests:
                    self._queue.append(now)
                    return waited

                wait_time = max(self._queue[0] + self._per - now, 0.0)

                if self._timeout is not None:
                    elapsed = now - start
                    remaining = self._timeout - elapsed
                    if remaining <= 0 or wait_time > remaining:
                        raise RateLimitTimeoutError(
                            "Rate limiter acquire timed out",
                            waited=elapsed,
                            limit=self._requests,
                            per_seconds=self._per,
                        )

            await asyncio.sleep(wait_time)
            waited = time.perf_counter() - start

    async def release(self) -> None:
        return None

    @asynccontextmanager
    async def throttle(self) -> AsyncIterator[float]:
        waited = await self.acquire()
        try:
            yield waited
        finally:
            await self.release()
