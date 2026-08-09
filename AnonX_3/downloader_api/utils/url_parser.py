"""URL parsing utilities."""

import re
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

from AnonX_3.downloader_api.core.constants import SUPPORTED_SOURCES
from AnonX_3.downloader_api.core.exceptions import InvalidURLError, UnsupportedSourceError


YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")

YOUTUBE_URL_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?.*v=|youtu\.be/)([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/v/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})"),
    re.compile(r"music\.youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})"),
]


def is_valid_url(url: str) -> bool:
    if not url or len(url) > 2048:
        return False

    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def is_video_id(value: str) -> bool:
    return bool(YOUTUBE_VIDEO_ID_PATTERN.match(value))


def extract_video_id(url: str) -> Optional[str]:
    url = url.strip()

    if is_video_id(url):
        return url

    for pattern in YOUTUBE_URL_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)

    try:
        parsed = urlparse(url)
        if parsed.query:
            params = parse_qs(parsed.query)
            if "v" in params and params["v"]:
                video_id = params["v"][0]
                if is_video_id(video_id):
                    return video_id
    except Exception:
        pass

    return None


def normalize_url(url: str) -> str:
    url = url.strip()

    if is_video_id(url):
        return f"https://www.youtube.com/watch?v={url}"

    video_id = extract_video_id(url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"

    return url


def get_source_host(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower()
    except Exception:
        return None


def validate_source_url(url: str) -> Tuple[str, str]:
    url = url.strip()

    if is_video_id(url):
        return url, "youtube.com"

    if not is_valid_url(url):
        raise InvalidURLError(f"Invalid URL format: {url[:100]}")

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise InvalidURLError(f"Unsupported URL scheme: {parsed.scheme}")

    if parsed.scheme == "file":
        raise InvalidURLError("File URLs are not allowed")

    host = parsed.netloc.lower()

    if not host:
        raise InvalidURLError("URL must have a valid host")

    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise InvalidURLError("Localhost URLs are not allowed")

    ip_pattern = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
    if ip_pattern.match(host):
        parts = [int(p) for p in host.split(".")]
        if parts[0] == 10:
            raise InvalidURLError("Private IP addresses are not allowed")
        if parts[0] == 172 and 16 <= parts[1] <= 31:
            raise InvalidURLError("Private IP addresses are not allowed")
        if parts[0] == 192 and parts[1] == 168:
            raise InvalidURLError("Private IP addresses are not allowed")

    if ".." in url or "/./" in url:
        raise InvalidURLError("Path traversal detected")

    host_parts = host.split(":")
    clean_host = host_parts[0]

    if clean_host not in SUPPORTED_SOURCES:
        raise UnsupportedSourceError(f"Unsupported source: {clean_host}")

    video_id = extract_video_id(url)
    if not video_id:
        raise InvalidURLError("Could not extract video ID from URL")

    return video_id, clean_host


def build_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
