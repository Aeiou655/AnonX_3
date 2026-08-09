"""Metrics endpoint."""

import logging

from fastapi import APIRouter, Request

from AnonX_3.downloader_api.core.dependencies import RequestIdDep, ApiKeyDep
from AnonX_3.downloader_api.schemas.health import MetricsResponse, ResourceUsage
from AnonX_3.downloader_api.monitoring.metrics import metrics_collector
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor
from AnonX_3.downloader_api.queue.queue_manager import queue_manager
from AnonX_3.downloader_api.processing.ffmpeg_manager import ffmpeg_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(
    request: Request,
    request_id: RequestIdDep,
    api_key: ApiKeyDep,
):
    metrics = metrics_collector.get_metrics()
    snapshot = await resource_monitor.get_snapshot()
    queue_stats = queue_manager.get_queue_stats()

    return MetricsResponse(
        success=True,
        total_requests=metrics["total_requests"],
        successful_requests=metrics["successful_requests"],
        failed_requests=metrics["failed_requests"],
        cache_hit_ratio=metrics["cache_hit_ratio"],
        avg_extraction_time_ms=metrics["avg_extraction_time_ms"],
        avg_download_time_ms=metrics["avg_download_time_ms"],
        avg_processing_time_ms=metrics["avg_processing_time_ms"],
        active_jobs=queue_stats["active_jobs"],
        queued_jobs=queue_stats["queued_jobs"],
        download_throughput_mbps=metrics["download_throughput_mbps"],
        ffmpeg_process_count=len(ffmpeg_manager.active_processes),
        resources=ResourceUsage(
            cpu_percent=snapshot.cpu_percent,
            memory_percent=snapshot.memory_percent,
            memory_used_mb=snapshot.memory_used_mb,
            memory_total_mb=snapshot.memory_total_mb,
            disk_free_gb=snapshot.disk_free_gb,
            disk_total_gb=snapshot.disk_total_gb,
            disk_percent=snapshot.disk_percent,
        ),
        error_counts=metrics["error_counts"],
        request_id=request_id,
    )
