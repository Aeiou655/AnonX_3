"""Rate limiter implementation."""

import asyncio
import time
import logging
from typing import Dict, Optional
from dataclasses import dataclass, field

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.exceptions import RateLimitedError

logger = logging.getLogger(__name__)


@dataclass
class RateLimitBucket:
    tokens: int
    last_update: float = field(default_factory=time.time)


class RateLimiter:
    def __init__(
        self,
        requests_per_window: int = 30,
        window_seconds: int = 60,
    ):
        self.requests_per_window = requests_per_window
        self.window_seconds = window_seconds
        self._buckets: Dict[str, RateLimitBucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> bool:
        if not settings.rate_limit_enabled:
            return True

        async with self._lock:
            now = time.time()

            if key not in self._buckets:
                self._buckets[key] = RateLimitBucket(
                    tokens=self.requests_per_window - 1,
                    last_update=now,
                )
                return True

            bucket = self._buckets[key]

            elapsed = now - bucket.last_update
            refill = int(elapsed / self.window_seconds * self.requests_per_window)

            if refill > 0:
                bucket.tokens = min(
                    self.requests_per_window,
                    bucket.tokens + refill,
                )
                bucket.last_update = now

            if bucket.tokens > 0:
                bucket.tokens -= 1
                return True

            return False

    async def acquire(self, key: str) -> None:
        if not await self.check(key):
            retry_after = self._get_retry_after(key)
            raise RateLimitedError(
                message=f"Rate limit exceeded. Retry after {retry_after} seconds.",
                retry_after=retry_after,
            )

    def _get_retry_after(self, key: str) -> int:
        if key not in self._buckets:
            return self.window_seconds

        bucket = self._buckets[key]
        elapsed = time.time() - bucket.last_update
        remaining = self.window_seconds - elapsed

        return max(1, int(remaining))

    async def get_remaining(self, key: str) -> int:
        async with self._lock:
            if key not in self._buckets:
                return self.requests_per_window

            bucket = self._buckets[key]
            now = time.time()
            elapsed = now - bucket.last_update
            refill = int(elapsed / self.window_seconds * self.requests_per_window)

            return min(self.requests_per_window, bucket.tokens + refill)

    async def reset(self, key: str) -> None:
        async with self._lock:
            if key in self._buckets:
                del self._buckets[key]

    async def cleanup_expired(self) -> int:
        async with self._lock:
            now = time.time()
            expired = [
                key for key, bucket in self._buckets.items()
                if now - bucket.last_update > self.window_seconds * 2
            ]

            for key in expired:
                del self._buckets[key]

            return len(expired)


rate_limiter = RateLimiter(
    requests_per_window=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
)
