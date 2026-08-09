"""Queue manager for download jobs."""

import asyncio
import logging
import time
from typing import Optional, Dict, Callable, Awaitable

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import JobPriority, JobState, MediaType
from AnonX_3.downloader_api.schemas.job import Job
from AnonX_3.downloader_api.queue.priority_queue import PriorityQueue
from AnonX_3.downloader_api.queue.job_states import is_terminal

logger = logging.getLogger(__name__)


class QueueManager:
    def __init__(self):
        self._audio_queue = PriorityQueue(maxsize=settings.max_queue_size)
        self._video_queue = PriorityQueue(maxsize=settings.max_queue_size)
        self._processing_queue = PriorityQueue(maxsize=settings.max_queue_size)
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._job_events: Dict[str, asyncio.Event] = {}
        self._started = False

    async def add_job(self, job: Job) -> bool:
        async with self._lock:
            if job.job_id in self._jobs:
                logger.warning(f"Job {job.job_id} already exists")
                return False

            self._jobs[job.job_id] = job
            self._job_events[job.job_id] = asyncio.Event()

        if job.media_type == MediaType.AUDIO:
            queue = self._audio_queue
        else:
            queue = self._video_queue

        success = await queue.put(
            job_id=job.job_id,
            priority=job.priority,
            timestamp=job.created_at.timestamp(),
            data=job,
        )

        if success:
            job.state = JobState.QUEUED
            logger.info(
                f"Job {job.job_id} added to queue",
                extra={"job_id": job.job_id, "priority": job.priority.name},
            )

        return success

    async def get_next_audio_job(self, timeout: Optional[float] = None) -> Optional[Job]:
        item = await self._audio_queue.get(timeout=timeout)
        if item:
            return self._jobs.get(item.job_id)
        return None

    async def get_next_video_job(self, timeout: Optional[float] = None) -> Optional[Job]:
        item = await self._video_queue.get(timeout=timeout)
        if item:
            return self._jobs.get(item.job_id)
        return None

    async def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    async def update_job_state(
        self,
        job_id: str,
        state: JobState,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False

            job.state = state
            job.updated_at = asyncio.get_event_loop().time()

            if error_code:
                job.error_code = error_code
            if error_message:
                job.error_message = error_message

            if is_terminal(state):
                event = self._job_events.get(job_id)
                if event:
                    event.set()

            return True

    async def update_job_progress(
        self,
        job_id: str,
        progress: float,
        speed: Optional[str] = None,
        eta: Optional[int] = None,
    ) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False

        job.progress = progress
        if speed:
            job.speed = speed
        if eta is not None:
            job.eta = eta

        return True

    async def complete_job(
        self,
        job_id: str,
        file_path: str,
        file_size: int,
        mime_type: str,
    ) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False

            job.state = JobState.READY
            job.file_path = file_path
            job.file_size = file_size
            job.mime_type = mime_type
            job.progress = 100.0

            event = self._job_events.get(job_id)
            if event:
                event.set()

            return True

    async def fail_job(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
    ) -> bool:
        return await self.update_job_state(
            job_id=job_id,
            state=JobState.FAILED,
            error_code=error_code,
            error_message=error_message,
        )

    async def cancel_job(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False

            if is_terminal(job.state):
                return False

            job.state = JobState.CANCELLED

            await self._audio_queue.remove(job_id)
            await self._video_queue.remove(job_id)

            event = self._job_events.get(job_id)
            if event:
                event.set()

            return True

    async def wait_for_job(
        self,
        job_id: str,
        timeout: Optional[float] = None,
    ) -> Optional[Job]:
        event = self._job_events.get(job_id)
        if not event:
            return None

        try:
            if timeout:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            else:
                await event.wait()
        except asyncio.TimeoutError:
            pass

        return self._jobs.get(job_id)

    async def remove_job(self, job_id: str) -> bool:
        async with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
            if job_id in self._job_events:
                del self._job_events[job_id]

            await self._audio_queue.remove(job_id)
            await self._video_queue.remove(job_id)

            return True

    def get_queue_stats(self) -> dict:
        active = sum(1 for j in self._jobs.values() if j.state in (
            JobState.EXTRACTING, JobState.DOWNLOADING, JobState.PROCESSING
        ))
        queued = self._audio_queue.qsize() + self._video_queue.qsize()
        completed = sum(1 for j in self._jobs.values() if j.state == JobState.COMPLETED)
        failed = sum(1 for j in self._jobs.values() if j.state == JobState.FAILED)

        return {
            "active_jobs": active,
            "queued_jobs": queued,
            "completed_jobs": completed,
            "failed_jobs": failed,
            "total_jobs": len(self._jobs),
            "audio_queue_size": self._audio_queue.qsize(),
            "video_queue_size": self._video_queue.qsize(),
        }

    def get_all_jobs(self) -> list[Job]:
        return list(self._jobs.values())


queue_manager = QueueManager()
