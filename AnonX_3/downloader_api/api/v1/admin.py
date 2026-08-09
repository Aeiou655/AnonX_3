"""Admin endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Path as PathParam, Request

from AnonX_3.downloader_api.core.dependencies import RequestIdDep, AdminApiKeyDep
from AnonX_3.downloader_api.core.exceptions import JobNotFoundError
from AnonX_3.downloader_api.schemas.cache import CacheCleanupResponse
from AnonX_3.downloader_api.schemas.health import AdminStatusResponse, ResourceUsage, QueueStatus
from AnonX_3.downloader_api.cache.cleanup_manager import cleanup_manager
from AnonX_3.downloader_api.cache.cache_manager import cache_manager
from AnonX_3.downloader_api.queue.queue_manager import queue_manager
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor
from AnonX_3.downloader_api.monitoring.health_monitor import health_monitor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


@router.post("/cache/cleanup", response_model=CacheCleanupResponse)
async def cleanup_cache(
    request: Request,
    request_id: RequestIdDep,
    admin_api_key: AdminApiKeyDep,
    emergency: bool = False,
):
    result = await cleanup_manager.run_cleanup(force=True, emergency=emergency)

    return CacheCleanupResponse(
        success=True,
        result=result,
        request_id=request_id,
    )


@router.delete("/cache")
async def clear_cache(
    request: Request,
    request_id: RequestIdDep,
    admin_api_key: AdminApiKeyDep,
):
    result = await cleanup_manager.clear_all_cache()

    return {
        "success": True,
        "removed_entries": result.removed_entries,
        "removed_size_bytes": result.removed_size_bytes,
        "request_id": request_id,
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    request: Request,
    request_id: RequestIdDep,
    admin_api_key: AdminApiKeyDep,
    job_id: Annotated[str, PathParam(min_length=1)],
):
    job = await queue_manager.get_job(job_id)
    if not job:
        raise JobNotFoundError(f"Job {job_id} not found")

    success = await queue_manager.cancel_job(job_id)

    return {
        "success": success,
        "job_id": job_id,
        "message": "Job cancelled" if success else "Could not cancel job",
        "request_id": request_id,
    }


@router.get("/status", response_model=AdminStatusResponse)
async def get_admin_status(
    request: Request,
    request_id: RequestIdDep,
    admin_api_key: AdminApiKeyDep,
):
    health_state = await health_monitor.get_health_state()
    snapshot = await resource_monitor.get_snapshot()
    queue_stats = queue_manager.get_queue_stats()
    cache_stats = cache_manager.get_stats()

    return AdminStatusResponse(
        success=True,
        state=health_state,
        resource_state=snapshot.resource_state,
        disk_state=snapshot.disk_state,
        resources=ResourceUsage(
            cpu_percent=snapshot.cpu_percent,
            memory_percent=snapshot.memory_percent,
            memory_used_mb=snapshot.memory_used_mb,
            memory_total_mb=snapshot.memory_total_mb,
            disk_free_gb=snapshot.disk_free_gb,
            disk_total_gb=snapshot.disk_total_gb,
            disk_percent=snapshot.disk_percent,
        ),
        queue=QueueStatus(
            active_jobs=queue_stats["active_jobs"],
            queued_jobs=queue_stats["queued_jobs"],
            completed_jobs=queue_stats["completed_jobs"],
            failed_jobs=queue_stats["failed_jobs"],
        ),
        cache_stats={
            "total_entries": cache_stats.total_entries,
            "total_size_bytes": cache_stats.total_size_bytes,
            "audio_entries": cache_stats.audio_entries,
            "video_entries": cache_stats.video_entries,
        },
        worker_stats={
            "audio_queue_size": queue_stats.get("audio_queue_size", 0),
            "video_queue_size": queue_stats.get("video_queue_size", 0),
        },
        circuit_breakers={},
        recent_errors=[],
        request_id=request_id,
    )
