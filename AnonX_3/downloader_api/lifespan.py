"""Application lifespan management."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.logging_config import setup_logging
from AnonX_3.downloader_api.storage.database import Database
from AnonX_3.downloader_api.cache.cleanup_manager import cleanup_manager
from AnonX_3.downloader_api.queue.scheduler import scheduler
from AnonX_3.downloader_api.queue.worker import Worker
from AnonX_3.downloader_api.core.constants import MediaType

logger = logging.getLogger(__name__)

_workers: list[Worker] = []
_database: Database = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _workers, _database

    setup_logging()

    logger.info("Starting Self-Hosted Downloader API")

    settings.ensure_directories()

    _database = Database()

    await scheduler.start()

    scheduler.schedule_recurring(
        name="cache_cleanup",
        func=lambda: cleanup_manager.run_cleanup(),
        interval_seconds=settings.cleanup_interval_minutes * 60,
        initial_delay=60,
    )

    audio_worker = Worker(
        name="audio_worker",
        media_type=MediaType.AUDIO,
    )
    await audio_worker.start(max_concurrent=settings.max_audio_workers)
    _workers.append(audio_worker)

    video_worker = Worker(
        name="video_worker",
        media_type=MediaType.VIDEO,
    )
    await video_worker.start(max_concurrent=settings.max_video_workers)
    _workers.append(video_worker)

    logger.info("Application startup complete")

    yield

    logger.info("Shutting down application")

    for worker in _workers:
        await worker.stop()

    await scheduler.stop()

    logger.info("Application shutdown complete")
