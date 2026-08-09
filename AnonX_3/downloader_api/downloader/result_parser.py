"""Result parsing utilities."""

import logging
from typing import Optional, Any
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_extraction_result(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "video_id": info.get("id", ""),
        "title": info.get("title", "Untitled"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "is_live": info.get("is_live", False),
        "was_live": info.get("was_live", False),
        "formats": info.get("formats", []),
        "requested_formats": info.get("requested_formats"),
        "extractor": info.get("extractor", "youtube"),
        "webpage_url": info.get("webpage_url"),
        "description": info.get("description"),
        "categories": info.get("categories", []),
        "tags": info.get("tags", []),
    }


def get_actual_output_path(
    template_path: Path,
    info: dict[str, Any],
) -> Optional[Path]:
    if template_path.exists():
        return template_path

    parent = template_path.parent
    stem = template_path.stem

    if parent.exists():
        for file in parent.iterdir():
            if file.stem.startswith(stem) or stem in file.stem:
                if file.is_file():
                    return file

    return None


def extract_format_info(format_dict: dict) -> dict[str, Any]:
    return {
        "format_id": format_dict.get("format_id", ""),
        "ext": format_dict.get("ext", ""),
        "resolution": format_dict.get("resolution"),
        "fps": format_dict.get("fps"),
        "vcodec": format_dict.get("vcodec"),
        "acodec": format_dict.get("acodec"),
        "filesize": format_dict.get("filesize"),
        "filesize_approx": format_dict.get("filesize_approx"),
        "tbr": format_dict.get("tbr"),
        "abr": format_dict.get("abr"),
        "vbr": format_dict.get("vbr"),
        "height": format_dict.get("height"),
        "width": format_dict.get("width"),
        "quality": format_dict.get("quality"),
        "has_audio": format_dict.get("acodec") not in (None, "none"),
        "has_video": format_dict.get("vcodec") not in (None, "none"),
    }


def is_format_available(format_dict: dict) -> bool:
    return format_dict.get("url") is not None or format_dict.get("fragments") is not None


def estimate_size_from_format(format_dict: dict, duration: Optional[int]) -> Optional[int]:
    filesize = format_dict.get("filesize") or format_dict.get("filesize_approx")
    if filesize:
        return filesize

    if duration:
        tbr = format_dict.get("tbr")
        if tbr:
            return int((tbr * 1000 / 8) * duration)

    return None
