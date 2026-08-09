"""URL security guard."""

import re
import logging
from urllib.parse import urlparse
from typing import Set, Tuple

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import SUPPORTED_SOURCES
from AnonX_3.downloader_api.core.exceptions import InvalidURLError, UnsupportedSourceError

logger = logging.getLogger(__name__)


class URLGuard:
    def __init__(self):
        self.max_url_length = settings.max_url_length
        self.allowed_schemes = {"http", "https"}
        self.blocked_schemes = {"file", "ftp", "data", "javascript"}
        self.supported_hosts = SUPPORTED_SOURCES

        self.private_ip_patterns = [
            re.compile(r"^10\."),
            re.compile(r"^172\.(1[6-9]|2[0-9]|3[0-1])\."),
            re.compile(r"^192\.168\."),
            re.compile(r"^127\."),
            re.compile(r"^0\."),
            re.compile(r"^169\.254\."),
        ]

        self.localhost_patterns = {
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "::1",
            "[::1]",
        }

    def validate(self, url: str) -> Tuple[str, str]:
        url = url.strip()

        if not url:
            raise InvalidURLError("URL cannot be empty")

        if len(url) > self.max_url_length:
            raise InvalidURLError(f"URL exceeds maximum length of {self.max_url_length}")

        if self._is_video_id(url):
            return url, "youtube.com"

        try:
            parsed = urlparse(url)
        except Exception:
            raise InvalidURLError("Invalid URL format")

        if parsed.scheme.lower() in self.blocked_schemes:
            raise InvalidURLError(f"URL scheme '{parsed.scheme}' is not allowed")

        if parsed.scheme.lower() not in self.allowed_schemes:
            raise InvalidURLError(f"URL scheme '{parsed.scheme}' is not supported")

        host = parsed.netloc.lower()
        if not host:
            raise InvalidURLError("URL must have a valid host")

        host_without_port = host.split(":")[0]

        if host_without_port in self.localhost_patterns:
            raise InvalidURLError("Localhost URLs are not allowed")

        if self._is_private_ip(host_without_port):
            raise InvalidURLError("Private IP addresses are not allowed")

        if ".." in url or "/./" in url:
            raise InvalidURLError("Path traversal detected")

        if host_without_port not in self.supported_hosts:
            raise UnsupportedSourceError(f"Unsupported source: {host_without_port}")

        return url, host_without_port

    def _is_video_id(self, value: str) -> bool:
        return bool(re.match(r"^[a-zA-Z0-9_-]{11}$", value))

    def _is_private_ip(self, host: str) -> bool:
        for pattern in self.private_ip_patterns:
            if pattern.match(host):
                return True
        return False

    def sanitize_url(self, url: str) -> str:
        url = url.strip()
        url = re.sub(r"\s+", "", url)
        return url


url_guard = URLGuard()
