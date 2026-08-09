"""Jobs endpoint."""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Path as PathParam, Query, Request
from fastapi.responses import FileResponse

from AnonX_3.downloader_api.core.dependencies import RequestIdDep, ApiKeyDep
from AnonX_3.downloader_api.core.constants import JobState
from AnonX_3.downloader_api.core.exceptions import JobNotFoundError, JobNotReadyError
from AnonX_3.downloader_api.schemas.job import JobResponse, JobListResponse
from AnonX_3.downloader_api.queue.queue_manager import queue_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    request: Request,
    request_id: RequestIdDep,
    api_key: ApiKeyDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    all_jobs = queue_manager.get_all_jobs()

    all_jobs.sort(key=lambda j: j.created_at, reverse=True)

    paginated = all_jobs[offset:offset + limit]

    return JobListResponse(
        success=True,
        jobs=[j.to_info() for j in paginated],
        total=len(all_jobs),
        request_id=request_id,
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    request: Request,
    request_id: RequestIdDep,
    api_key: ApiKeyDep,
    job_id: Annotated[str, PathParam(min_length=1)],
):
    job = await queue_manager.get_job(job_id)

    if not job:
        raise JobNotFoundError(f"Job {job_id} not found")

    return JobResponse(
        success=True,
        job=job.to_info(),
        request_id=request_id,
    )


@router.get("/jobs/{job_id}/file")
async def get_job_file(
    request: Request,
    request_id: RequestIdDep,
    api_key: ApiKeyDep,
    job_id: Annotated[str, PathParam(min_length=1)],
):
    job = await queue_manager.get_job(job_id)

    if not job:
        raise JobNotFoundError(f"Job {job_id} not found")

    if job.state != JobState.READY:
        raise JobNotReadyError(f"Job {job_id} is not ready. Current state: {job.state.value}")

    if not job.file_path:
        raise JobNotReadyError(f"Job {job_id} has no output file")

    file_path = Path(job.file_path)
    if not file_path.exists():
        raise JobNotFoundError(f"Output file for job {job_id} not found")

    return FileResponse(
        path=str(file_path),
        media_type=job.mime_type or "application/octet-stream",
        filename=f"{job.video_id}.{job.selected_format or 'bin'}",
        headers={
            "X-Request-ID": request_id,
            "X-Job-ID": job.job_id,
            "X-Video-ID": job.video_id,
        },
    )


@router.post("/jobs")
async def create_job(
    request: Request,
    request_id: RequestIdDep,
    api_key: ApiKeyDep,
    url: Annotated[str, Query(min_length=1, max_length=2048)],
    type: Annotated[str, Query()] = "audio",
    format: Annotated[str, Query()] = "auto",
    quality: Annotated[str, Query()] = "auto",
):
    from AnonX_3.downloader_api.core.constants import MediaType, Quality
    from AnonX_3.downloader_api.queue.job_manager import job_manager
    from AnonX_3.downloader_api.utils.url_parser import validate_source_url, normalize_url

    video_id, source = validate_source_url(url)
    normalized_url = normalize_url(url)

    media_type = MediaType(type)
    format_value = format.lower() if format else "auto"
    if format_value == "auto":
        format_value = "original" if media_type == MediaType.AUDIO else "mp4"

    job = await job_manager.create_job(
        request_id=request_id,
        url=normalized_url,
        media_type=media_type,
        format=format_value,
        quality=quality,
        force=False,
    )

    await queue_manager.add_job(job)

    return {
        "success": True,
        "job_id": job.job_id,
        "video_id": job.video_id,
        "status": "queued",
        "request_id": request_id,
    }
