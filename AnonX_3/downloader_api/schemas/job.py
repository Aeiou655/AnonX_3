"""Job schemas."""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field

from AnonX_3.downloader_api.core.constants import JobState, JobPriority, MediaType, ProcessingMode


class JobCreate(BaseModel):
    url: str
    media_type: MediaType
    format: str
    quality: str
    force: bool = False


class JobInfo(BaseModel):
    job_id: str
    video_id: str
    state: JobState
    progress: float = Field(default=0.0, ge=0.0, le=100.0)
    speed: Optional[str] = None
    eta: Optional[int] = None
    attempt: int = Field(default=1)
    selected_quality: Optional[str] = None
    selected_format: Optional[str] = None
    processing_mode: Optional[ProcessingMode] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class JobResponse(BaseModel):
    success: bool = True
    job: JobInfo
    request_id: str


class JobListResponse(BaseModel):
    success: bool = True
    jobs: list[JobInfo]
    total: int
    request_id: str


class Job(BaseModel):
    job_id: str
    request_id: str
    video_id: str
    url: str
    normalized_url: str
    media_type: MediaType
    format: str
    quality: str
    cache_key: str
    state: JobState = JobState.CREATED
    priority: JobPriority = JobPriority.NORMAL_AUDIO
    progress: float = 0.0
    speed: Optional[str] = None
    eta: Optional[int] = None
    attempt: int = 1
    max_attempts: int = 5
    selected_quality: Optional[str] = None
    selected_format: Optional[str] = None
    selected_format_id: Optional[str] = None
    processing_mode: Optional[ProcessingMode] = None
    title: Optional[str] = None
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    estimated_size: Optional[int] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    temp_dir: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    force: bool = False

    def to_info(self) -> JobInfo:
        return JobInfo(
            job_id=self.job_id,
            video_id=self.video_id,
            state=self.state,
            progress=self.progress,
            speed=self.speed,
            eta=self.eta,
            attempt=self.attempt,
            selected_quality=self.selected_quality,
            selected_format=self.selected_format,
            processing_mode=self.processing_mode,
            error_code=self.error_code,
            error_message=self.error_message,
            created_at=self.created_at,
            updated_at=self.updated_at,
            completed_at=self.completed_at,
        )
