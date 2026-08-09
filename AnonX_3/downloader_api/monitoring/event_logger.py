"""Event logging."""

import logging
from datetime import datetime, timezone
from typing import Optional, Any

logger = logging.getLogger("app.events")


def log_event(
    event: str,
    request_id: Optional[str] = None,
    job_id: Optional[str] = None,
    video_id: Optional[str] = None,
    media_type: Optional[str] = None,
    **kwargs: Any,
) -> None:
    extra = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if request_id:
        extra["request_id"] = request_id
    if job_id:
        extra["job_id"] = job_id
    if video_id:
        extra["video_id"] = video_id
    if media_type:
        extra["media_type"] = media_type

    extra.update(kwargs)

    logger.info(event, extra=extra)


def log_download_started(
    request_id: str,
    job_id: str,
    video_id: str,
    media_type: str,
) -> None:
    log_event(
        "download_started",
        request_id=request_id,
        job_id=job_id,
        video_id=video_id,
        media_type=media_type,
    )


def log_download_completed(
    request_id: str,
    job_id: str,
    video_id: str,
    media_type: str,
    file_size: int,
    duration_ms: int,
    cache_status: str,
) -> None:
    log_event(
        "download_completed",
        request_id=request_id,
        job_id=job_id,
        video_id=video_id,
        media_type=media_type,
        file_size=file_size,
        duration_ms=duration_ms,
        cache_status=cache_status,
    )


def log_download_failed(
    request_id: str,
    job_id: str,
    video_id: str,
    media_type: str,
    error_code: str,
    error_message: str,
) -> None:
    log_event(
        "download_failed",
        request_id=request_id,
        job_id=job_id,
        video_id=video_id,
        media_type=media_type,
        error_code=error_code,
        error_message=error_message,
    )


def log_cache_hit(
    request_id: str,
    video_id: str,
    cache_key: str,
) -> None:
    log_event(
        "cache_hit",
        request_id=request_id,
        video_id=video_id,
        cache_key=cache_key,
    )


def log_cache_miss(
    request_id: str,
    video_id: str,
    cache_key: str,
) -> None:
    log_event(
        "cache_miss",
        request_id=request_id,
        video_id=video_id,
        cache_key=cache_key,
    )
