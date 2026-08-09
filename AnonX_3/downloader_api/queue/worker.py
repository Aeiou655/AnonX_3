"""Worker for processing download jobs."""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional, Callable, Awaitable
from datetime import datetime, timezone

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import JobState, MediaType, ProcessingMode, CacheStatus
from AnonX_3.downloader_api.core.exceptions import DownloaderAPIError
from AnonX_3.downloader_api.schemas.job import Job
from AnonX_3.downloader_api.queue.queue_manager import queue_manager
from AnonX_3.downloader_api.queue.job_manager import job_manager
from AnonX_3.downloader_api.downloader.engine import download_engine, DownloadResult
from AnonX_3.downloader_api.downloader.extractor import metadata_extractor
from AnonX_3.downloader_api.downloader.progress_hook import DownloadProgress
from AnonX_3.downloader_api.processing.validator import media_validator
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor
from AnonX_3.downloader_api.dynamic.decision_engine import decision_engine
from AnonX_3.downloader_api.storage.path_manager import path_manager
from AnonX_3.downloader_api.storage.file_manager import file_manager

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        name: str,
        media_type: MediaType,
        on_complete: Optional[Callable[[Job], Awaitable[None]]] = None,
    ):
        self.name = name
        self.media_type = media_type
        self.on_complete = on_complete
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._current_job: Optional[Job] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def start(self, max_concurrent: int = 1) -> None:
        self._running = True
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Worker {self.name} started (max_concurrent={max_concurrent})")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"Worker {self.name} stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                if self.media_type == MediaType.AUDIO:
                    job = await queue_manager.get_next_audio_job(timeout=1.0)
                else:
                    job = await queue_manager.get_next_video_job(timeout=1.0)

                if job:
                    async with self._semaphore:
                        await self._process_job(job)
                        queue_manager._audio_queue.task_done() if self.media_type == MediaType.AUDIO else queue_manager._video_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {self.name} loop error: {e}")
                await asyncio.sleep(1)

    async def _process_job(self, job: Job) -> None:
        self._current_job = job
        job.started_at = datetime.now(timezone.utc)

        logger.info(
            f"Processing job {job.job_id}",
            extra={"job_id": job.job_id, "video_id": job.video_id},
        )

        try:
            await queue_manager.update_job_state(job.job_id, JobState.EXTRACTING)

            metadata = await metadata_extractor.extract(
                url=job.normalized_url,
                video_id=job.video_id,
            )

            await job_manager.update_job_metadata(
                job=job,
                title=metadata.title,
                duration=metadata.duration,
                thumbnail=metadata.thumbnail,
            )

            resource_state = await resource_monitor.get_resource_state()

            await queue_manager.update_job_state(job.job_id, JobState.DOWNLOADING)

            def progress_callback(progress: DownloadProgress):
                asyncio.create_task(
                    queue_manager.update_job_progress(
                        job_id=job.job_id,
                        progress=progress.percent,
                        speed=progress.speed_str,
                        eta=progress.eta,
                    )
                )

            result = await download_engine.download(
                job=job,
                resource_state=resource_state,
                progress_callback=progress_callback,
            )

            if not result.success:
                raise DownloaderAPIError(
                    code="DOWNLOAD_FAILED",
                    message=result.error or "Download failed",
                    retryable=True,
                    status_code=502,
                )

            await queue_manager.update_job_state(job.job_id, JobState.VALIDATING_FILE)

            validation = await media_validator.validate(
                file_path=result.file_path,
                media_type=job.media_type,
                expected_duration=job.duration,
            )

            if not validation.is_valid:
                raise DownloaderAPIError(
                    code="VALIDATION_FAILED",
                    message=validation.error or "File validation failed",
                    retryable=False,
                    status_code=502,
                )

            await queue_manager.update_job_state(job.job_id, JobState.SAVING)

            final_path = path_manager.get_cache_path(
                cache_key=job.cache_key,
                extension=result.extension,
                media_type=job.media_type,
            )

            success = file_manager.atomic_move(
                source_path=result.file_path,
                target_path=final_path,
                overwrite=job.force,
            )

            if not success:
                raise DownloaderAPIError(
                    code="SAVE_FAILED",
                    message="Failed to save file to cache",
                    retryable=True,
                    status_code=500,
                )

            await job_manager.set_job_result(
                job=job,
                file_path=str(final_path),
                file_size=final_path.stat().st_size,
                mime_type=validation.media_info.format_name if validation.media_info else "application/octet-stream",
                selected_format=result.selected_format,
                selected_quality=result.selected_quality,
            )

            await queue_manager.update_job_state(job.job_id, JobState.READY)

            logger.info(
                f"Job {job.job_id} completed successfully",
                extra={
                    "job_id": job.job_id,
                    "file_size": job.file_size,
                },
            )

            if self.on_complete:
                await self.on_complete(job)

        except DownloaderAPIError as e:
            await job_manager.set_job_error(job, e.code, e.message)
            await queue_manager.fail_job(job.job_id, e.code, e.message)
            logger.error(f"Job {job.job_id} failed: {e.code} - {e.message}")

        except Exception as e:
            error_message = str(e)
            await job_manager.set_job_error(job, "INTERNAL_ERROR", error_message)
            await queue_manager.fail_job(job.job_id, "INTERNAL_ERROR", error_message)
            logger.exception(f"Job {job.job_id} failed with unexpected error")

        finally:
            self._current_job = None
            path_manager.cleanup_temp_job(job.job_id)

    @property
    def is_busy(self) -> bool:
        return self._current_job is not None

    @property
    def current_job_id(self) -> Optional[str]:
        return self._current_job.job_id if self._current_job else None
