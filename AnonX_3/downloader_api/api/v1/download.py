"""Download endpoint."""

import time
import logging
from pathlib import Path
from typing import Optional, Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from AnonX_3.downloader_api.core.constants import MediaType, Quality, CacheStatus, JobState
from AnonX_3.downloader_api.core.dependencies import RequestIdDep, ApiKeyDep
from AnonX_3.downloader_api.core.exceptions import (
    QueueFullError, ResourceBusyError, DownloaderAPIError,
)
from AnonX_3.downloader_api.schemas.download import DownloadRequest, DownloadResponse, AsyncDownloadResponse
from AnonX_3.downloader_api.schemas.job import Job
from AnonX_3.downloader_api.security.rate_limiter import rate_limiter
from AnonX_3.downloader_api.security.request_limits import request_limits
from AnonX_3.downloader_api.utils.url_parser import validate_source_url, normalize_url
from AnonX_3.downloader_api.utils.hashing import generate_cache_key
from AnonX_3.downloader_api.cache.cache_manager import cache_manager
from AnonX_3.downloader_api.queue.queue_manager import queue_manager
from AnonX_3.downloader_api.queue.job_manager import job_manager
from AnonX_3.downloader_api.dynamic.decision_engine import decision_engine
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor
from AnonX_3.downloader_api.downloader.extractor import metadata_extractor
from AnonX_3.downloader_api.monitoring.metrics import metrics_collector
from AnonX_3.downloader_api.monitoring.event_logger import log_cache_hit, log_cache_miss

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/download")
async def download(
    request: Request,
    request_id: RequestIdDep,
    api_key: ApiKeyDep,
    url: Annotated[str, Query(min_length=1, max_length=2048)],
    type: Annotated[MediaType, Query()] = MediaType.AUDIO,
    format: Annotated[str, Query()] = "auto",
    quality: Annotated[Quality, Query()] = Quality.AUTO,
    wait: Annotated[bool, Query()] = True,
    force: Annotated[bool, Query()] = False,
):
    start_time = time.time()

    client_ip = request.client.host if request.client else "unknown"
    await rate_limiter.acquire(client_ip)

    request_limits.validate_url_length(url)
    video_id, source = validate_source_url(url)
    normalized_url = normalize_url(url)

    format_value = format.lower() if format else "auto"
    if format_value == "auto":
        format_value = "original" if type == MediaType.AUDIO else "mp4"

    cache_key = generate_cache_key(
        video_id=video_id,
        media_type=type,
        format=format_value,
        quality=quality.value,
    )

    if not force:
        cache_status, cache_entry, cache_path = await cache_manager.get(cache_key)

        if cache_status == CacheStatus.HIT and cache_entry and cache_path:
            metrics_collector.record_cache_hit()
            log_cache_hit(request_id, video_id, cache_key)

            return FileResponse(
                path=str(cache_path),
                media_type=cache_entry.mime_type,
                filename=f"{video_id}.{cache_entry.format}",
                headers={
                    "X-Request-ID": request_id,
                    "X-Video-ID": video_id,
                    "X-Cache": "HIT",
                    "X-Selected-Quality": cache_entry.quality,
                    "X-Selected-Format": cache_entry.format,
                    "X-Processing-Mode": "none",
                },
            )

    metrics_collector.record_cache_miss()
    log_cache_miss(request_id, video_id, cache_key)

    job = await job_manager.create_job(
        request_id=request_id,
        url=normalized_url,
        media_type=type,
        format=format_value,
        quality=quality.value,
        force=force,
    )

    metadata = await metadata_extractor.extract(
        url=normalized_url,
        video_id=video_id,
    )

    if metadata.duration:
        request_limits.validate_duration(metadata.duration, type)

    await job_manager.update_job_metadata(
        job=job,
        title=metadata.title,
        duration=metadata.duration,
        thumbnail=metadata.thumbnail,
    )

    queue_stats = queue_manager.get_queue_stats()
    decision = await decision_engine.should_accept_download(
        media_type=type,
        duration=metadata.duration,
        estimated_size=job.estimated_size,
        requested_quality=quality,
        queue_length=queue_stats["queued_jobs"],
    )

    if not decision.should_proceed:
        raise ResourceBusyError(decision.reason or "Server is busy")

    if not await queue_manager.add_job(job):
        raise QueueFullError("Download queue is full")

    if not wait:
        return AsyncDownloadResponse(
            success=True,
            job_id=job.job_id,
            video_id=video_id,
            status="queued",
            message="Download job created. Use /jobs/{job_id} to check status.",
            request_id=request_id,
        )

    completed_job = await queue_manager.wait_for_job(
        job_id=job.job_id,
        timeout=900,
    )

    if not completed_job:
        raise DownloaderAPIError(
            code="DOWNLOAD_TIMEOUT",
            message="Download timed out",
            retryable=True,
            status_code=504,
        )

    if completed_job.state == JobState.FAILED:
        raise DownloaderAPIError(
            code=completed_job.error_code or "DOWNLOAD_FAILED",
            message=completed_job.error_message or "Download failed",
            retryable=True,
            status_code=502,
        )

    if completed_job.state != JobState.READY or not completed_job.file_path:
        raise DownloaderAPIError(
            code="DOWNLOAD_FAILED",
            message="Download did not complete successfully",
            retryable=True,
            status_code=502,
        )

    file_path = Path(completed_job.file_path)
    if not file_path.exists():
        raise DownloaderAPIError(
            code="FILE_NOT_FOUND",
            message="Downloaded file not found",
            retryable=True,
            status_code=500,
        )

    duration_ms = int((time.time() - start_time) * 1000)
    metrics_collector.record_request(success=True)
    metrics_collector.record_download_time(duration_ms, completed_job.file_size or 0)

    return FileResponse(
        path=str(file_path),
        media_type=completed_job.mime_type or "application/octet-stream",
        filename=f"{video_id}.{completed_job.selected_format or format_value}",
        headers={
            "X-Request-ID": request_id,
            "X-Job-ID": completed_job.job_id,
            "X-Video-ID": video_id,
            "X-Cache": "MISS",
            "X-Selected-Quality": completed_job.selected_quality or quality.value,
            "X-Selected-Format": completed_job.selected_format or format_value,
            "X-Processing-Mode": completed_job.processing_mode.value if completed_job.processing_mode else "none",
        },
    )
