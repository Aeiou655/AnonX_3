"""Filename utilities."""

import re
import unicodedata
from pathlib import Path
from typing import Optional


MAX_FILENAME_LENGTH = 200


def sanitize_filename(filename: str, max_length: int = MAX_FILENAME_LENGTH) -> str:
    if not filename:
        return "untitled"

    filename = unicodedata.normalize("NFKC", filename)

    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)

    filename = re.sub(r"[\s_]+", "_", filename)

    filename = filename.strip("._- ")

    if len(filename) > max_length:
        filename = filename[:max_length].rstrip("._- ")

    if not filename:
        return "untitled"

    return filename


def get_safe_extension(ext: str) -> str:
    ext = ext.lower().strip()
    if not ext.startswith("."):
        ext = f".{ext}"

    allowed = {
        ".mp3", ".m4a", ".opus", ".aac", ".webm", ".ogg", ".wav", ".flac",
        ".mp4", ".mkv", ".avi", ".mov",
    }

    return ext if ext in allowed else ".bin"


def generate_output_filename(
    video_id: str,
    title: Optional[str],
    ext: str,
    media_type: str,
    quality: Optional[str] = None,
) -> str:
    ext = get_safe_extension(ext)

    if title:
        safe_title = sanitize_filename(title, max_length=100)
        base = f"{safe_title}_{video_id}"
    else:
        base = video_id

    if quality and media_type == "video":
        base = f"{base}_{quality}"

    return f"{base}{ext}"


def get_temp_filename(job_id: str, suffix: str = ".part") -> str:
    return f"{job_id}{suffix}"


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent

    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1
        if counter > 1000:
            raise RuntimeError("Could not find unique filename")
