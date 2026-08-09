"""Priority queue implementation."""

import asyncio
import logging
from typing import Optional, Any
from dataclasses import dataclass, field

from AnonX_3.downloader_api.core.constants import JobPriority

logger = logging.getLogger(__name__)


@dataclass(order=True)
class PriorityItem:
    priority: int
    timestamp: float = field(compare=True)
    job_id: str = field(compare=False)
    data: Any = field(compare=False, default=None)


class PriorityQueue:
    def __init__(self, maxsize: int = 0):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=maxsize)
        self._pending: set[str] = set()
        self._lock = asyncio.Lock()

    async def put(
        self,
        job_id: str,
        priority: JobPriority,
        timestamp: float,
        data: Any = None,
    ) -> bool:
        async with self._lock:
            if job_id in self._pending:
                logger.debug(f"Job {job_id} already in queue")
                return False

            item = PriorityItem(
                priority=priority.value,
                timestamp=timestamp,
                job_id=job_id,
                data=data,
            )

            try:
                self._queue.put_nowait(item)
                self._pending.add(job_id)
                return True
            except asyncio.QueueFull:
                logger.warning(f"Queue full, cannot add job {job_id}")
                return False

    async def get(self, timeout: Optional[float] = None) -> Optional[PriorityItem]:
        try:
            if timeout:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            else:
                item = await self._queue.get()

            async with self._lock:
                self._pending.discard(item.job_id)

            return item
        except asyncio.TimeoutError:
            return None

    async def get_nowait(self) -> Optional[PriorityItem]:
        try:
            item = self._queue.get_nowait()
            async with self._lock:
                self._pending.discard(item.job_id)
            return item
        except asyncio.QueueEmpty:
            return None

    def task_done(self) -> None:
        self._queue.task_done()

    async def contains(self, job_id: str) -> bool:
        async with self._lock:
            return job_id in self._pending

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def full(self) -> bool:
        return self._queue.full()

    async def remove(self, job_id: str) -> bool:
        async with self._lock:
            if job_id in self._pending:
                self._pending.discard(job_id)
                return True
            return False

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._pending)
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._pending.clear()
            return count
