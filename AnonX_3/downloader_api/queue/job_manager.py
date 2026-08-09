"""Job manager for tracking and managing download jobs."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import MediaType, JobState, JobPriority, Quality
from AnonX_3.downloader_api.schemas.job import Job
from AnonX_3.downloader_api.utils.hashing import generate_cache_key, generate_job_id
from AnonX_3.downloader_api.utils.url_parser import validate_source_url, normalize_url
from AnonX_3.downloader_api.queue.queue_manager import queue_manager

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._lock_manager = asyncio.Lock()

    async def create_job(
        self,
        request_id: str,
        url: str,
        media_type: MediaType,
        format: str,
        quality: str,
        force: bool = False,
    ) -> Job:
        video_id, source = validate_source_url(url)
        normalized_url = normalize_url(url)

        quality_enum = Quality(quality) if quality in [q.value for q in Quality] else Quality.AUTO

        cache_key = generate_cache_key(
            video_id=video_id,
            media_type=media_type,
            format=format,
            quality=quality,
            source="youtube",
        )

        priority = self._calculate_priority(media_type, None, None)

        job = Job(
            job_id=generate_job_id(),
            request_id=request_id,
            video_id=video_id,
            url=url,
            normalized_url=normalized_url,
            media_type=media_type,
            format=format,
            quality=quality,
            cache_key=cache_key,
            state=JobState.CREATED,
            priority=priority,
            force=force,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        logger.info(
            f"Created job {job.job_id}",
            extra={
                "job_id": job.job_id,
                "video_id": video_id,
                "media_type": media_type.value,
            },
        )

        return job

    def _calculate_priority(
        self,
        media_type: MediaType,
        duration: Optional[int],
        estimated_size: Optional[int],
    ) -> JobPriority:
        if media_type == MediaType.AUDIO:
            if duration and duration <= 300:
                return JobPriority.SHORT_AUDIO
            return JobPriority.NORMAL_AUDIO

        if duration and duration <= 180:
            return JobPriority.SHORT_VIDEO

        if estimated_size and estimated_size > 500 * 1024 * 1024:
            return JobPriority.LARGE_MEDIA

        return JobPriority.NORMAL_VIDEO

    async def get_or_create_lock(self, cache_key: str) -> asyncio.Lock:
        async with self._lock_manager:
            if cache_key not in self._locks:
                self._locks[cache_key] = asyncio.Lock()
            return self._locks[cache_key]

    async def release_lock(self, cache_key: str) -> None:
        async with self._lock_manager:
            if cache_key in self._locks:
                lock = self._locks[cache_key]
                if not lock.locked():
                    del self._locks[cache_key]

    async def update_job_metadata(
        self,
        job: Job,
        title: Optional[str] = None,
        duration: Optional[int] = None,
        thumbnail: Optional[str] = None,
        estimated_size: Optional[int] = None,
    ) -> None:
        if title:
            job.title = title
        if duration is not None:
            job.duration = duration
        if thumbnail:
            job.thumbnail = thumbnail
        if estimated_size is not None:
            job.estimated_size = estimated_size

        if duration or estimated_size:
            job.priority = self._calculate_priority(
                job.media_type,
                duration,
                estimated_size,
            )

        job.updated_at = datetime.now(timezone.utc)

    async def set_job_result(
        self,
        job: Job,
        file_path: str,
        file_size: int,
        mime_type: str,
        selected_format: Optional[str] = None,
        selected_quality: Optional[str] = None,
    ) -> None:
        job.file_path = file_path
        job.file_size = file_size
        job.mime_type = mime_type

        if selected_format:
            job.selected_format = selected_format
        if selected_quality:
            job.selected_quality = selected_quality

        job.state = JobState.READY
        job.progress = 100.0
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)

    async def set_job_error(
        self,
        job: Job,
        error_code: str,
        error_message: str,
    ) -> None:
        job.state = JobState.FAILED
        job.error_code = error_code
        job.error_message = error_message
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)


job_manager = JobManager()
