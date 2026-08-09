"""Security utilities."""

import secrets
import hashlib
from typing import Optional

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.exceptions import InvalidAPIKeyError


def constant_time_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def validate_api_key(api_key: Optional[str], require_admin: bool = False) -> bool:
    if not api_key:
        raise InvalidAPIKeyError("API key is required")

    if require_admin:
        if not settings.admin_api_key:
            raise InvalidAPIKeyError("Admin API key not configured")
        if not constant_time_compare(api_key, settings.admin_api_key):
            raise InvalidAPIKeyError("Invalid admin API key")
    else:
        if not settings.api_key:
            raise InvalidAPIKeyError("API key not configured")
        if not constant_time_compare(api_key, settings.api_key):
            raise InvalidAPIKeyError("Invalid API key")

    return True


def generate_api_key(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def mask_api_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:]
