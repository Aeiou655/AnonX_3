"""Health endpoint."""

import logging

from fastapi import APIRouter, Request

from AnonX_3.downloader_api.core.dependencies import RequestIdDep
from AnonX_3.downloader_api.schemas.health import HealthResponse, ResourceUsage, QueueStatus, ComponentHealth
from AnonX_3.downloader_api.monitoring.health_monitor import health_monitor
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor
from AnonX_3.downloader_api.queue.queue_manager import queue_manager
from AnonX_3.downloader_api.cache.cache_manager import cache_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def get_health(
    request: Request,
    request_id: RequestIdDep,
):
    health_state = await health_monitor.get_health_state()
    snapshot = await resource_monitor.get_snapshot()
    components = await health_monitor.check_components()
    queue_stats = queue_manager.get_queue_stats()
    cache_stats = cache_manager.get_stats()

    return HealthResponse(
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
        cache_size_mb=cache_stats.total_size_bytes / (1024 * 1024),
        cache_entries=cache_stats.total_entries,
        ytdlp_ready=health_monitor.is_ytdlp_ready(),
        ffmpeg_ready=health_monitor.is_ffmpeg_ready(),
        ffprobe_ready=health_monitor.is_ffprobe_ready(),
        components=[
            ComponentHealth(
                name=c.name,
                healthy=c.healthy,
                message=c.message,
                latency_ms=c.latency_ms,
            )
            for c in components
        ],
        uptime_seconds=health_monitor.uptime_seconds,
        request_id=request_id,
    )
