"""Cache schemas."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

from AnonX_3.downloader_api.core.constants import MediaType, ValidationState


class CacheEntry(BaseModel):
    cache_key: str
    video_id: str
    source: str = "youtube"
    media_type: MediaType
    format: str
    quality: str
    title: Optional[str] = None
    duration: Optional[int] = None
    file_path: str
    file_size: int
    mime_type: str
    validation_state: ValidationState = ValidationState.VALID
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_accessed_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    hit_count: int = 0


class CacheStats(BaseModel):
    total_entries: int = 0
    total_size_bytes: int = 0
    audio_entries: int = 0
    video_entries: int = 0
    audio_size_bytes: int = 0
    video_size_bytes: int = 0
    hit_count_total: int = 0
    oldest_entry: Optional[datetime] = None
    newest_entry: Optional[datetime] = None


class CacheStatsResponse(BaseModel):
    success: bool = True
    stats: CacheStats
    request_id: str


class CacheCleanupResult(BaseModel):
    removed_entries: int = 0
    removed_size_bytes: int = 0
    removed_temp_files: int = 0
    removed_quarantine_files: int = 0
    errors: list[str] = Field(default_factory=list)


class CacheCleanupResponse(BaseModel):
    success: bool = True
    result: CacheCleanupResult
    request_id: str
