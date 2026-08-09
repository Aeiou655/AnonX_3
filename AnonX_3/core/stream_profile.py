# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

from functools import lru_cache
from dataclasses import dataclass

import psutil
from pytgcalls import types

from AnonX_3 import config


SYSTEM_VIDEO_CAPS = {
    "poor": {"height": 360, "width": 640, "fps": 20},
    "normal": {"height": 480, "width": 854, "fps": 24},
    "good": {"height": 720, "width": 1280, "fps": 30},
}

DOWNLOAD_TIER_CAPS = {
    "poor": {"height": 360, "width": 640, "fps": 24},
    "normal": {"height": 480, "width": 854, "fps": 30},
    "good": {"height": 720, "width": 1280, "fps": 30},
}


@dataclass
class StreamProfile:
    tier: str
    reason: str
    cpu: float
    ping: float | None
    audio_parameters: object
    video_parameters: object
    download_tier: str | None
    max_height: int | None


def safe_ping_value(client=None) -> float | None:
    ping = getattr(client, "ping", None)
    try:
        value = float(ping) if ping is not None else None
    except Exception:
        return None
    if value is None or value <= 0:
        return None
    return value


def _audio_quality(tier: str = "normal"):
    quality = (config.AUDIO_QUALITY or "").lower()
    if quality in {"auto", ""}:
        if tier == "poor":
            return getattr(
                types.AudioQuality,
                "LOW",
                getattr(types.AudioQuality, "MEDIUM", types.AudioQuality.HIGH),
            )
        if tier == "good":
            return getattr(types.AudioQuality, "HIGH", types.AudioQuality.MEDIUM)
        return getattr(types.AudioQuality, "MEDIUM", types.AudioQuality.HIGH)
    if quality in {"low", "l"}:
        return getattr(types.AudioQuality, "LOW", types.AudioQuality.HIGH)
    if quality in {"high", "h"}:
        return getattr(types.AudioQuality, "HIGH", types.AudioQuality.HIGH)
    return getattr(types.AudioQuality, "MEDIUM", types.AudioQuality.HIGH)


def _video_quality(tier: str = "normal"):
    quality = (config.VIDEO_QUALITY or "").lower()
    if quality in {"auto", ""}:
        name = {
            "poor": "SD_360p",
            "normal": "SD_480p",
            "good": "HD_720p",
        }.get(tier, "SD_480p")
        return (
            getattr(types.VideoQuality, name, None)
            or getattr(types.VideoQuality, "SD_480p", None)
            or types.VideoQuality.HD_720p
        )

    mapping = {
        "360": "SD_360p",
        "sd360": "SD_360p",
        "sd_360p": "SD_360p",
        "480": "SD_480p",
        "sd480": "SD_480p",
        "sd_480p": "SD_480p",
        "720": "HD_720p",
        "hd720": "HD_720p",
        "hd_720p": "HD_720p",
        "1080": "FHD_1080p",
        "fhd1080": "FHD_1080p",
        "fhd_1080p": "FHD_1080p",
    }
    name = mapping.get(quality, "SD_480p")
    return (
        getattr(types.VideoQuality, name, None)
        or getattr(types.VideoQuality, "SD_480p", None)
        or types.VideoQuality.HD_720p
    )


def _is_auto_cap(value: int | str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"", "auto", "default"}


@lru_cache(maxsize=1)
def detect_system_video_tier() -> str:
    cpu_count = psutil.cpu_count(logical=True) or 1
    total_gb = psutil.virtual_memory().total / (1024 ** 3)
    if cpu_count <= 1 or total_gb < 1.8:
        return "poor"
    if cpu_count <= 2 or total_gb < 3.5:
        return "normal"
    return "good"


def resolve_video_caps(tier: str | None = None) -> dict[str, int | str]:
    system_tier = detect_system_video_tier()
    system_caps = SYSTEM_VIDEO_CAPS[system_tier]
    height = system_caps["height"] if _is_auto_cap(config.VIDEO_MAX_HEIGHT) else int(config.VIDEO_MAX_HEIGHT)
    width = system_caps["width"] if _is_auto_cap(config.VIDEO_MAX_WIDTH) else int(config.VIDEO_MAX_WIDTH)
    fps = system_caps["fps"] if _is_auto_cap(config.VIDEO_MAX_FPS) else int(config.VIDEO_MAX_FPS)

    tier_caps = DOWNLOAD_TIER_CAPS.get((tier or "").lower())
    quality_mode = (config.VIDEO_QUALITY or "").lower()
    if tier_caps:
        height = min(height, tier_caps["height"])
        if quality_mode in {"auto", ""}:
            width = min(width, tier_caps["width"])
            fps = min(fps, tier_caps["fps"])

    return {
        "height": max(240, min(int(height), 1080)),
        "width": max(320, min(int(width), 1920)),
        "fps": max(15, min(int(fps), 60)),
        "system_tier": system_tier,
    }


def tier_height(tier: str | None) -> int | None:
    if tier in {"poor", "normal", "good"}:
        return int(resolve_video_caps(tier)["height"])
    return None


class StreamProfileManager:
    def __init__(self):
        self.tiers: dict[int, str] = {}
        self.recover_hits: dict[int, int] = {}
        self.profiles: dict[int, StreamProfile] = {}
        self._process = psutil.Process()
        self._cpu_ewma: float | None = None
        self._memory_ewma: float | None = None

    def clear(self, chat_id: int) -> None:
        self.tiers.pop(chat_id, None)
        self.recover_hits.pop(chat_id, None)
        self.profiles.pop(chat_id, None)

    @staticmethod
    def _build_profile(
        tier: str,
        *,
        reason: str,
        cpu: float,
        ping: float | None,
        lightweight: bool = False,
    ) -> StreamProfile:
        download_tier = (
            tier if (config.VIDEO_QUALITY or "").lower() in {"auto", ""} else None
        )
        if lightweight and download_tier:
            max_height = int(DOWNLOAD_TIER_CAPS[download_tier]["height"])
        else:
            max_height = tier_height(download_tier)
        return StreamProfile(
            tier=tier,
            reason=reason,
            cpu=cpu,
            ping=ping,
            audio_parameters=_audio_quality(tier),
            video_parameters=_video_quality(tier),
            download_tier=download_tier,
            max_height=max_height,
        )

    def cached_or_default(self, chat_id: int) -> StreamProfile:
        """Return hot-path parameters without sampling live CPU/RAM/ping."""
        cached = self.profiles.get(chat_id)
        if cached is not None:
            return cached
        tier = self.tiers.get(chat_id, "normal")
        return self._build_profile(
            tier,
            reason="cached_default",
            cpu=float(self._cpu_ewma or 0.0),
            ping=None,
            lightweight=True,
        )

    def _auto_enabled(self) -> bool:
        if not config.STREAM_ADAPTIVE:
            return False
        return (config.AUDIO_QUALITY or "").lower() in {"auto", ""} or (
            config.VIDEO_QUALITY or ""
        ).lower() in {"auto", ""}

    def select(self, chat_id: int, client=None) -> StreamProfile:
        logical_cpus = max(1, int(psutil.cpu_count(logical=True) or 1))
        system_cpu = float(psutil.cpu_percent(interval=None))
        try:
            process_cpu = float(self._process.cpu_percent(interval=None)) / logical_cpus
        except Exception:
            process_cpu = 0.0
        sample_cpu = max(process_cpu, system_cpu * 0.40)
        memory = float(psutil.virtual_memory().percent)
        alpha = 0.30
        self._cpu_ewma = (
            sample_cpu
            if self._cpu_ewma is None
            else (alpha * sample_cpu) + ((1.0 - alpha) * self._cpu_ewma)
        )
        self._memory_ewma = (
            memory
            if self._memory_ewma is None
            else (alpha * memory) + ((1.0 - alpha) * self._memory_ewma)
        )
        memory_pressure = max(0.0, self._memory_ewma - 78.0) * 0.8
        active_pressure = max(0, len(self.tiers) - 1) * 2.5
        cpu = min(100.0, self._cpu_ewma + memory_pressure + active_pressure)
        ping = safe_ping_value(client)

        if not self._auto_enabled():
            tier = "normal"
            reason = "adaptive_disabled"
            self.clear(chat_id)
        else:
            tier, reason = self._select_auto_tier(chat_id, cpu, ping)

        profile = self._build_profile(
            tier,
            reason=reason,
            cpu=cpu,
            ping=ping,
        )
        self.profiles[chat_id] = profile
        return profile

    def _select_auto_tier(self, chat_id: int, cpu: float, ping: float | None) -> tuple[str, str]:
        current = self.tiers.get(chat_id, "normal")
        is_stressed = cpu >= config.ADAPTIVE_CPU_HIGH or (
            ping is not None and ping >= config.ADAPTIVE_PING_HIGH
        )
        is_healthy = cpu <= config.ADAPTIVE_CPU_RECOVER and (
            ping is None or ping <= config.ADAPTIVE_PING_RECOVER
        )

        if is_stressed:
            self.tiers[chat_id] = "poor"
            self.recover_hits[chat_id] = 0
            reasons = []
            if cpu >= config.ADAPTIVE_CPU_HIGH:
                reasons.append("cpu")
            if ping is not None and ping >= config.ADAPTIVE_PING_HIGH:
                reasons.append("ping")
            return "poor", "+".join(reasons) or "stress"

        if current == "poor":
            if is_healthy:
                hits = self.recover_hits.get(chat_id, 0) + 1
                self.recover_hits[chat_id] = hits
                if hits >= 2:
                    self.tiers[chat_id] = "normal"
                    self.recover_hits[chat_id] = 0
                    return "normal", "recovered"
                return "poor", "recovery_hysteresis"
            self.recover_hits[chat_id] = 0
            return "poor", "wait_recovery"

        if is_healthy:
            if current == "good":
                self.tiers[chat_id] = "good"
                self.recover_hits[chat_id] = 0
                return "good", "healthy"
            hits = self.recover_hits.get(chat_id, 0) + 1
            self.recover_hits[chat_id] = hits
            if hits >= 3:
                self.tiers[chat_id] = "good"
                self.recover_hits[chat_id] = 0
                return "good", "healthy_stable"
            self.tiers[chat_id] = "normal"
            return "normal", "upshift_hysteresis"

        self.tiers[chat_id] = "normal"
        self.recover_hits[chat_id] = 0
        return "normal", "baseline"

