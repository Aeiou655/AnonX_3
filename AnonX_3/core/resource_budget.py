# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲
"""Dynamic resource budget for playback background work.

Foreground voice/video start is always protected. Background work
(current-cache, next prefetch, rich thumbnails) scales with the active
quality tier and live load band from ResourceManager.
"""

from __future__ import annotations

from AnonX_3 import config
from AnonX_3.core.resource_manager import resource_manager


def _tier(value: str | None) -> str:
    name = (value or "normal").strip().lower()
    if name in {"poor", "normal", "good"}:
        return name
    return "normal"


def allow_current_cache(tier: str | None) -> bool:
    if not config.YOUTUBE_DIRECT_CACHE_BG:
        return False
    if not resource_manager.allow_background_cache():
        return False
    return _tier(tier) != "poor"


def allow_prefetch_next(tier: str | None) -> bool:
    if not config.PREFETCH_NEXT:
        return False
    if not resource_manager.allow_prefetch_next():
        return False
    return _tier(tier) != "poor"


def allow_prefetch_video(tier: str | None, is_video: bool) -> bool:
    if not is_video:
        return True
    if not config.PREFETCH_VIDEO:
        return False
    if not resource_manager.allow_prefetch_video():
        return False
    return _tier(tier) == "good"


def thumb_cheap(tier: str | None) -> bool:
    """True when thumbnail should use a lower-cost render path."""
    if resource_manager.snapshot().band == "high":
        return True
    return _tier(tier) == "poor"


def thumb_size(tier: str | None) -> tuple[int, int]:
    if thumb_cheap(tier):
        return (960, 540)
    return (1280, 720)


def effective_quality_tier(preferred: str | None = None) -> str:
    """Load-aware tier for download/format selection."""
    return resource_manager.quality_tier_for_load(preferred)


def effective_quality_plan(preferred: str | None = None):
    """Full quality plan (tier + concurrency + prefetch flags)."""
    return resource_manager.select_quality_plan(preferred)


def log_effective_playback_mode(logger) -> None:
    """Startup visibility so fixed env quality cannot silently confuse operators."""
    snap = resource_manager.snapshot()
    logger.info(
        "playback_mode DYNAMIC_QUALITY=%s STREAM_ADAPTIVE=%s AUDIO_QUALITY=%s "
        "VIDEO_QUALITY=%s VIDEO_MAX_HEIGHT=%s PREFETCH_NEXT=%s PREFETCH_VIDEO=%s "
        "YOUTUBE_DIRECT_STREAM=%s THUMB_GEN=%s load_band=%s limits=%s",
        getattr(config, "DYNAMIC_QUALITY", None),
        getattr(config, "STREAM_ADAPTIVE", None),
        getattr(config, "AUDIO_QUALITY", None),
        getattr(config, "VIDEO_QUALITY", None),
        getattr(config, "VIDEO_MAX_HEIGHT", None),
        getattr(config, "PREFETCH_NEXT", None),
        getattr(config, "PREFETCH_VIDEO", None),
        getattr(config, "YOUTUBE_DIRECT_STREAM", None),
        getattr(config, "THUMB_GEN", None),
        snap.band,
        resource_manager.stats().get("limits"),
    )
