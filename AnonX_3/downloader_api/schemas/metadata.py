"""Metadata schemas."""

from typing import Optional, List
from pydantic import BaseModel, Field


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    resolution: Optional[str] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    filesize: Optional[int] = None
    filesize_approx: Optional[int] = None
    tbr: Optional[float] = None
    abr: Optional[float] = None
    vbr: Optional[float] = None
    height: Optional[int] = None
    width: Optional[int] = None
    quality: Optional[float] = None
    has_audio: bool = False
    has_video: bool = False


class MetadataRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)


class MetadataResponse(BaseModel):
    success: bool = True
    video_id: str
    title: str
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    upload_date: Optional[str] = None
    view_count: Optional[int] = None
    is_live: bool = False
    is_available: bool = True
    audio_formats: List[FormatInfo] = Field(default_factory=list)
    video_formats: List[FormatInfo] = Field(default_factory=list)
    estimated_audio_size: Optional[int] = None
    estimated_video_size: Optional[int] = None
    request_id: str


class ExtractedMetadata(BaseModel):
    video_id: str
    title: str
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    upload_date: Optional[str] = None
    view_count: Optional[int] = None
    is_live: bool = False
    is_available: bool = True
    formats: List[dict] = Field(default_factory=list)
    requested_formats: Optional[List[dict]] = None
    extractor: str = "youtube"
    webpage_url: Optional[str] = None
