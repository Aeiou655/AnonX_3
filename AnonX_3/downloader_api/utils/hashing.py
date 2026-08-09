"""Hashing utilities."""

import hashlib
from typing import Optional

from AnonX_3.downloader_api.core.constants import MediaType


def generate_cache_key(
    video_id: str,
    media_type: MediaType,
    format: str,
    quality: str,
    source: str = "youtube",
) -> str:
    key_parts = [source, video_id, media_type.value, format, quality]
    key_string = ":".join(key_parts)
    return hashlib.sha256(key_string.encode()).hexdigest()[:32]


def generate_short_hash(data: str, length: int = 8) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:length]


def hash_file_partial(file_path: str, chunk_size: int = 65536) -> Optional[str]:
    try:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            chunk = f.read(chunk_size)
            if chunk:
                hasher.update(chunk)

            f.seek(0, 2)
            size = f.tell()
            if size > chunk_size * 2:
                f.seek(-chunk_size, 2)
                chunk = f.read(chunk_size)
                if chunk:
                    hasher.update(chunk)

        return hasher.hexdigest()[:16]
    except Exception:
        return None


def generate_job_id() -> str:
    import uuid
    return str(uuid.uuid4())


def generate_request_id() -> str:
    import uuid
    return str(uuid.uuid4())
