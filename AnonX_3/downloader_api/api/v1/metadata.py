"""Metadata endpoint."""

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request

from AnonX_3.downloader_api.core.dependencies import RequestIdDep, ApiKeyDep
from AnonX_3.downloader_api.schemas.metadata import MetadataResponse, FormatInfo
from AnonX_3.downloader_api.security.rate_limiter import rate_limiter
from AnonX_3.downloader_api.security.request_limits import request_limits
from AnonX_3.downloader_api.utils.url_parser import validate_source_url
from AnonX_3.downloader_api.downloader.extractor import metadata_extractor

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/metadata", response_model=MetadataResponse)
async def get_metadata(
    request: Request,
    request_id: RequestIdDep,
    api_key: ApiKeyDep,
    url: Annotated[str, Query(min_length=1, max_length=2048)],
):
    client_ip = request.client.host if request.client else "unknown"
    await rate_limiter.acquire(client_ip)

    request_limits.validate_url_length(url)
    video_id, source = validate_source_url(url)

    metadata = await metadata_extractor.extract(
        url=url,
        video_id=video_id,
        use_cache=True,
    )

    audio_formats = []
    video_formats = []

    for f in metadata.formats:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")

        format_info = FormatInfo(
            format_id=f.get("format_id", ""),
            ext=f.get("ext", ""),
            resolution=f.get("resolution"),
            fps=f.get("fps"),
            vcodec=vcodec,
            acodec=acodec,
            filesize=f.get("filesize"),
            filesize_approx=f.get("filesize_approx"),
            tbr=f.get("tbr"),
            abr=f.get("abr"),
            vbr=f.get("vbr"),
            height=f.get("height"),
            width=f.get("width"),
            quality=f.get("quality"),
            has_audio=acodec not in (None, "none"),
            has_video=vcodec not in (None, "none"),
        )

        if vcodec in (None, "none") and acodec not in (None, "none"):
            audio_formats.append(format_info)
        elif vcodec not in (None, "none"):
            video_formats.append(format_info)

    estimated_audio_size = None
    estimated_video_size = None

    if metadata.duration:
        for af in audio_formats:
            if af.filesize:
                estimated_audio_size = af.filesize
                break
            elif af.tbr:
                estimated_audio_size = int((af.tbr * 1000 / 8) * metadata.duration)
                break

        for vf in video_formats:
            if vf.filesize:
                estimated_video_size = vf.filesize
                break

    return MetadataResponse(
        success=True,
        video_id=video_id,
        title=metadata.title,
        duration=metadata.duration,
        thumbnail=metadata.thumbnail,
        uploader=metadata.uploader,
        upload_date=metadata.upload_date,
        view_count=metadata.view_count,
        is_live=metadata.is_live,
        is_available=metadata.is_available,
        audio_formats=audio_formats,
        video_formats=video_formats,
        estimated_audio_size=estimated_audio_size,
        estimated_video_size=estimated_video_size,
        request_id=request_id,
    )
