# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Process-local singleflight: one shared job per key, waiters join the same task.

Optional Redis backend when SINGLEFLIGHT_BACKEND=redis and REDIS_URL is set.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")
logger = logging.getLogger("AnonX_3")
_SINGLEFLIGHT_INSTANCES: weakref.WeakSet = weakref.WeakSet()


class SingleFlight:
    """Deduplicate concurrent async work by string key."""

    def __init__(self, name: str = "default") -> None:
        self.name = name
        self._inflight: dict[str, asyncio.Task] = {}
        self._factory_tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._owner: dict[str, str] = {}
        self._started_at: dict[str, float] = {}
        self._shutdown_lock = asyncio.Lock()
        self._closing = False
        self._shutdown_complete = False
        _SINGLEFLIGHT_INSTANCES.add(self)

    def _forget_factory_task(self, task: asyncio.Task) -> None:
        """Drop completed factory ownership and consume orphaned failures."""

        self._factory_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def get_task(self, key: str) -> asyncio.Task | None:
        task = self._inflight.get(key)
        if task is None or task.done():
            return None
        return task

    def is_running(self, key: str) -> bool:
        return self.get_task(key) is not None

    async def do(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
    ) -> T:
        """Run factory once per key; concurrent callers await the same task."""
        if self._closing:
            raise RuntimeError(f"singleflight {self.name!r} is shutting down")
        existing = self.get_task(key)
        if existing is not None:
            logger.debug(
                "singleflight join name=%s key=%s owner=%s",
                self.name,
                key,
                self._owner.get(key),
            )
            if timeout is not None:
                return await asyncio.wait_for(asyncio.shield(existing), timeout=timeout)
            return await asyncio.shield(existing)

        lock = self._lock_for(key)
        async with lock:
            if self._closing:
                raise RuntimeError(f"singleflight {self.name!r} is shutting down")
            existing = self.get_task(key)
            if existing is not None:
                if timeout is not None:
                    return await asyncio.wait_for(
                        asyncio.shield(existing), timeout=timeout
                    )
                return await asyncio.shield(existing)

            owner = uuid.uuid4().hex[:12]
            self._owner[key] = owner
            self._started_at[key] = time.time()

            async def _runner() -> T:
                if self._closing:
                    raise RuntimeError(f"singleflight {self.name!r} is shutting down")
                factory_task = asyncio.create_task(
                    factory(), name=f"sf-factory:{self.name}:{key}"
                )
                self._factory_tasks.add(factory_task)
                factory_task.add_done_callback(self._forget_factory_task)
                try:
                    # Shield the factory call to break deep cancellation chains.
                    # Waiters already shield from us (line 65/74/102); shielding
                    # the owner's work prevents 990+ recursive child.cancel()
                    # when the top-level request is cancelled mid-flight.
                    return await asyncio.shield(factory_task)
                except asyncio.CancelledError:
                    # Allow explicit cancellation but break the propagation chain.
                    # The factory() task continues shielded; we just exit cleanly.
                    raise
                finally:
                    if self._owner.get(key) == owner:
                        self._owner.pop(key, None)
                        self._started_at.pop(key, None)
                        cur = self._inflight.get(key)
                        if cur is asyncio.current_task():
                            self._inflight.pop(key, None)

            task: asyncio.Task = asyncio.create_task(
                _runner(), name=f"sf:{self.name}:{key}"
            )
            self._inflight[key] = task
            logger.debug(
                "singleflight start name=%s key=%s owner=%s", self.name, key, owner
            )

        if timeout is not None:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return await asyncio.shield(task)

    def cancel(self, key: str) -> bool:
        task = self._inflight.pop(key, None)
        self._owner.pop(key, None)
        self._started_at.pop(key, None)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "backend": "memory",
            "inflight": len([t for t in self._inflight.values() if not t.done()]),
            "factory_tasks": len(
                [task for task in self._factory_tasks if not task.done()]
            ),
            "keys": list(self._inflight.keys()),
        }

    async def shutdown(self) -> None:
        """Cancel, await, and forget every owner/factory task exactly once."""

        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._closing = True
            current = asyncio.current_task()
            owned = list(
                dict.fromkeys([*self._inflight.values(), *self._factory_tasks])
            )
            self._inflight.clear()
            self._factory_tasks.clear()
            self._locks.clear()
            self._owner.clear()
            self._started_at.clear()

            pending = [
                task for task in owned if task is not current and not task.done()
            ]
            for task in pending:
                task.cancel()

            failures: list[Exception] = []
            try:
                if owned:
                    results = await asyncio.gather(
                        *(task for task in owned if task is not current),
                        return_exceptions=True,
                    )
                    failures.extend(
                        result for result in results if isinstance(result, Exception)
                    )
            finally:
                self._shutdown_complete = True

            if failures:
                raise ExceptionGroup(
                    f"singleflight {self.name!r} shutdown failed",
                    failures,
                )


async def shutdown_singleflights() -> None:
    """Quiesce all live in-process singleflight instances without task scans."""

    instances = list(_SINGLEFLIGHT_INSTANCES)
    if not instances:
        return
    results = await asyncio.gather(
        *(instance.shutdown() for instance in instances),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, Exception)]
    if failures:
        raise ExceptionGroup("SingleFlight registry shutdown failed", failures)


def _build_flight(name: str):
    try:
        from AnonX_3 import config

        backend = (getattr(config, "SINGLEFLIGHT_BACKEND", "memory") or "memory").lower()
        redis_url = (getattr(config, "REDIS_URL", "") or "").strip()
        if backend == "redis" and redis_url:
            from AnonX_3.core.downloader.redis_singleflight import RedisSingleFlight

            return RedisSingleFlight(name)
    except Exception as ex:
        logger.debug("singleflight backend select failed: %s", ex)
    return SingleFlight(name)


# Shared instances for media resolve/download paths.
singleflight = _build_flight("media")
resolve_flight = _build_flight("resolve")
download_flight = _build_flight("download")
