"""Scheduler for background tasks."""

import asyncio
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("Scheduler started")

    async def stop(self) -> None:
        self._running = False

        for name, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.debug(f"Cancelled task: {name}")

        self._tasks.clear()
        logger.info("Scheduler stopped")

    def schedule_recurring(
        self,
        name: str,
        func: Callable[[], Awaitable[None]],
        interval_seconds: float,
        initial_delay: float = 0,
    ) -> None:
        if name in self._tasks:
            self._tasks[name].cancel()

        async def _run():
            if initial_delay > 0:
                await asyncio.sleep(initial_delay)

            while self._running:
                try:
                    await func()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Scheduled task {name} failed: {e}")

                await asyncio.sleep(interval_seconds)

        task = asyncio.create_task(_run())
        self._tasks[name] = task
        logger.debug(f"Scheduled recurring task: {name} (every {interval_seconds}s)")

    def cancel_task(self, name: str) -> bool:
        if name in self._tasks:
            self._tasks[name].cancel()
            del self._tasks[name]
            return True
        return False


scheduler = Scheduler()
