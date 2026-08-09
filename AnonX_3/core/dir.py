# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import shutil
from pathlib import Path

from AnonX_3 import logger


RUNTIME_DIRS = ("cache", "downloads")


def _cdn_media_paths() -> list[Path]:
    """Return CDN media directories when enabled (never wiped on restart)."""
    try:
        from AnonX_3 import config

        if not getattr(config, "CDN_ENABLED", False):
            return []
        root = Path(getattr(config, "CDN_MEDIA_ROOT", "media") or "media")
        if not root.is_absolute():
            root = Path.cwd().resolve() / root
        return [root / "tmp", root / "ready", root]
    except Exception:
        return []


def reset_runtime_dirs() -> bool:
    """Compatibility helper: preserve reusable media and ensure directories exist.

    Restart paths used to delete ``cache`` and ``downloads``. Final releases
    keep validated/reusable media across manual and scheduled fresh-process
    restarts; disk-pressure cleanup remains the owner of reclamation.
    """
    base = Path.cwd().resolve()
    success = True
    for dirname in RUNTIME_DIRS:
        root = base / dirname
        try:
            if root.is_symlink() or (root.exists() and not root.is_dir()):
                root.unlink()
            root.mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            success = False
            logger.warning("Failed to ensure runtime directory %s: %s", dirname, ex)
    if success:
        logger.info("Runtime directories ensured; reusable media preserved.")
    return success


def runtime_storage_percent() -> float:
    """Return used percentage for the filesystem containing this deployment."""
    usage = shutil.disk_usage(Path.cwd())
    if not usage.total:
        return 0.0
    return (usage.used / usage.total) * 100.0


def ensure_dirs():
    """
    Ensure that the necessary directories exist.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg must be installed and accessible in the system PATH.")

    for dir in ["cache", "downloads", "AnonX_3/cookies"]:
        Path(dir).mkdir(parents=True, exist_ok=True)

    for path in _cdn_media_paths():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            logger.warning("Failed to create CDN directory %s: %s", path, ex)

    logger.info("Cache directories updated.")


