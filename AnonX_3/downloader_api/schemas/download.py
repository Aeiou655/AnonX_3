"""Download request and response schemas."""

from typing import Optional
from pydantic import BaseModel, Field, field_validator

from AnonX_3.downloader_api.core.constants import MediaType, AudioFormat, VideoFormat, Quality


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    type: MediaType = Field(default=MediaType.AUDIO)
    format: str = Field(default="auto")
    quality: Quality = Field(default=Quality.AUTO)
    wait: bool = Field(default=True)
    force: bool = Field(default=False)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL cannot be empty")
        return v

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str, info) -> str:
        v = v.lower().strip()
        media_type = info.data.get("type", MediaType.AUDIO)

        if media_type == MediaType.AUDIO:
            valid_formats = {f.value for f in AudioFormat}
        else:
            valid_formats = {f.value for f in VideoFormat}

        if v not in valid_formats:
            raise ValueError(f"Invalid format: {v}")
        return v


class DownloadResponse(BaseModel):
    success: bool = True
    job_id: str
    video_id: str
    title: Optional[str] = None
    duration: Optional[int] = None
    cache_status: str
    selected_quality: str
    selected_format: str
    processing_mode: str
    file_size: Optional[int] = None
    request_id: str


class AsyncDownloadResponse(BaseModel):
    success: bool = True
    job_id: str
    video_id: str
    status: str
    message: str
    request_id: str
