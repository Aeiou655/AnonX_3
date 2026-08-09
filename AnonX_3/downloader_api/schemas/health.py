"""Health and metrics schemas."""

from typing import Optional
from pydantic import BaseModel, Field

from AnonX_3.downloader_api.core.constants import HealthState, ResourceState, DiskState


class ComponentHealth(BaseModel):
    name: str
    healthy: bool
    message: Optional[str] = None
    latency_ms: Optional[float] = None


class ResourceUsage(BaseModel):
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_percent: float = 0.0


class QueueStatus(BaseModel):
    active_jobs: int = 0
    queued_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0


class HealthResponse(BaseModel):
    success: bool = True
    state: HealthState
    resource_state: ResourceState
    disk_state: DiskState
    resources: ResourceUsage
    queue: QueueStatus
    cache_size_mb: float = 0.0
    cache_entries: int = 0
    ytdlp_ready: bool = False
    ffmpeg_ready: bool = False
    ffprobe_ready: bool = False
    components: list[ComponentHealth] = Field(default_factory=list)
    uptime_seconds: float = 0.0
    request_id: str


class MetricsResponse(BaseModel):
    success: bool = True
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    cache_hit_ratio: float = 0.0
    avg_extraction_time_ms: float = 0.0
    avg_download_time_ms: float = 0.0
    avg_processing_time_ms: float = 0.0
    active_jobs: int = 0
    queued_jobs: int = 0
    download_throughput_mbps: float = 0.0
    ffmpeg_process_count: int = 0
    resources: ResourceUsage
    error_counts: dict[str, int] = Field(default_factory=dict)
    request_id: str


class AdminStatusResponse(BaseModel):
    success: bool = True
    state: HealthState
    resource_state: ResourceState
    disk_state: DiskState
    resources: ResourceUsage
    queue: QueueStatus
    cache_stats: dict
    worker_stats: dict
    circuit_breakers: dict
    recent_errors: list[dict] = Field(default_factory=list)
    request_id: str
