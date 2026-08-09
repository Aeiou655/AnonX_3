"""Disk utilities."""

import shutil
from pathlib import Path
from typing import Tuple

from AnonX_3.downloader_api.core.constants import DiskState


def get_disk_usage(path: str | Path) -> Tuple[int, int, int]:
    path = Path(path)

    while not path.exists() and path != path.parent:
        path = path.parent

    try:
        usage = shutil.disk_usage(str(path))
        return usage.total, usage.used, usage.free
    except Exception:
        return 0, 0, 0


def get_disk_free_gb(path: str | Path) -> float:
    _, _, free = get_disk_usage(path)
    return free / (1024 ** 3)


def get_disk_percent(path: str | Path) -> float:
    total, used, _ = get_disk_usage(path)
    if total == 0:
        return 0.0
    return (used / total) * 100


def get_disk_state(
    path: str | Path,
    warning_percent: float = 20.0,
    critical_percent: float = 5.0,
) -> DiskState:
    total, _, free = get_disk_usage(path)

    if total == 0:
        return DiskState.CRITICAL

    free_percent = (free / total) * 100

    if free_percent < critical_percent:
        return DiskState.CRITICAL
    elif free_percent < 10:
        return DiskState.HIGH_PRESSURE
    elif free_percent < warning_percent:
        return DiskState.WARNING
    return DiskState.NORMAL


def has_sufficient_space(path: str | Path, required_bytes: int) -> bool:
    _, _, free = get_disk_usage(path)
    return free > required_bytes


def format_bytes(size_bytes: int) -> str:
    if size_bytes < 0:
        return "0 B"

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0

    return f"{size_bytes:.1f} PB"


def get_directory_size(path: str | Path) -> int:
    path = Path(path)
    if not path.exists():
        return 0

    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception:
        pass

    return total
