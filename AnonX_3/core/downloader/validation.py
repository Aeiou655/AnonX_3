# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Local media file validation before cache READY / playback."""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


def is_valid_media_file(
    path: str | Path | None,
    *,
    min_bytes: int = 1024,
    video: bool = False,
) -> bool:
    """True when path exists, is a regular file, and meets minimum size."""
    if not path:
        return False
    p = Path(path)
    try:
        if not p.is_file():
            return False
        if p.name.endswith((".part", ".publishing", ".tmp", ".ytdl")):
            return False
        size = p.stat().st_size
        floor = max(min_bytes, 512 * 1024 if video else 64 * 1024)
        return size >= floor
    except OSError:
        return False


def validate_ready_file(
    path: str | Path | None,
    *,
    expected_min_bytes: int | None = None,
    video: bool = False,
) -> tuple[bool, str]:
    """Return (ok, reason)."""
    if not path:
        return False, "missing_path"
    p = Path(path)
    try:
        if not p.exists():
            return False, "not_found"
        if not p.is_file():
            return False, "not_file"
        if p.name.endswith((".part", ".publishing", ".tmp", ".ytdl")):
            return False, "incomplete_name"
        size = p.stat().st_size
        if size <= 0:
            return False, "empty"
        floor = expected_min_bytes
        if floor is None:
            floor = 512 * 1024 if video else 64 * 1024
        if size < int(floor):
            return False, f"too_small:{size}<{floor}"
        return True, "ok"
    except OSError as ex:
        return False, f"os_error:{ex}"


@lru_cache(maxsize=4096)
def _probe_media_stream_types(
    resolved_path: str,
    size: int,
    mtime_ns: int,
) -> frozenset[str]:
    """Probe once per immutable file fingerprint and retain both stream types."""
    target = Path(resolved_path)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return frozenset()
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            return frozenset()
        return frozenset(
            line.strip().lower()
            for line in result.stdout.splitlines()
            if line.strip().lower() in {"audio", "video"}
        )
    except Exception:
        return frozenset()


def has_media_stream(path: str | Path | None, stream: str) -> bool:
    """Use ffprobe to prove an artifact has the required audio/video stream."""
    if not path or stream not in {"audio", "video"}:
        return False
    target = Path(path)
    try:
        stat = target.stat()
        if (
            not target.is_file()
            or stat.st_size < 8 * 1024
            or any(marker in target.name.lower() for marker in (".part", ".ytdl"))
        ):
            return False
        resolved = str(target.resolve())
    except OSError:
        return False
    return stream in _probe_media_stream_types(
        resolved,
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def is_playable_media(path: str | Path | None, *, video: bool) -> bool:
    """Playback capability gate: audio is mandatory; video is mode-dependent."""
    if not has_media_stream(path, "audio"):
        return False
    return not video or has_media_stream(path, "video")


def matches_exact_playback_mode(
    path: str | Path | None,
    *,
    video: bool,
) -> bool:
    """Prove that a durable artifact belongs to the requested command mode.

    Audio playback deliberately rejects muxed video files.  This is stricter
    than ordinary playability: reusing a previous /vplay artifact for /play
    would suppress the fresh audio-direct extraction required on a mode switch.
    Likewise /vplay requires both audio and video streams and cannot accept an
    old audio-only .m4a/.webm cache file.
    """
    if not has_media_stream(path, "audio"):
        return False
    has_video = has_media_stream(path, "video")
    return has_video if video else not has_video
