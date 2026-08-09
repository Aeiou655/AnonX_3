# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Central resource manager: concurrency limits, load band, quality hints.

Foreground VC start stays protected. Background extract/download/prefetch
scale down under high CPU/RAM pressure.

Concurrency limits are no longer fixed: :mod:`AnonX_3.core.dynamic_capacity`
recomputes each lane from live CPU/RAM/load/event-loop-lag plus real demand.
The ``MAX_*`` env values remain the guaranteed fallback contract — if the
dynamic layer fails for any reason it latches back to exactly those numbers.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

from AnonX_3 import config
from AnonX_3.core.dynamic_capacity import (
    DynamicSemaphore,
    Priority,
    current_priority,
    dynamic_capacity,
)

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


@dataclass
class LoadSnapshot:
    band: str  # low | medium | high
    cpu_percent: float
    ram_percent: float
    free_ram_mb: float
    active_downloads: int
    active_extracts: int
    active_streams: int
    reason: str


@dataclass
class QualityPlan:
    """Unified quality + concurrency plan (prompt dynamic quality)."""

    tier: str  # poor | normal | good
    band: str
    audio_preference: str  # best | medium | low
    video_max_height: int
    allow_prefetch: bool
    allow_video_prefetch: bool
    max_parallel_downloads: int
    reason: str


@dataclass
class StreamReservation:
    """One VC stream slot, reserved *before* the call starts.

    ``admitted`` is the admission decision; ``created`` records whether this
    particular reservation is the one holding the slot. A chat that is already
    streaming (queue auto-next, ``/skip``, an audio→video swap) re-enters with
    ``created=False``, so a failed restart releases nothing and the live session
    keeps its slot.
    """

    chat_id: int
    admitted: bool
    created: bool
    manager: Any = None

    def release(self) -> None:
        """Give the slot back. Only releases what this reservation created."""
        if not self.created or self.manager is None:
            return
        self.created = False
        try:
            self.manager.unregister_stream(self.chat_id)
        except Exception:  # pragma: no cover - release must never raise
            pass


class ResourceManager:
    def __init__(self) -> None:
        self._extract_sem: DynamicSemaphore | None = None
        self._download_sem: DynamicSemaphore | None = None
        self._video_sem: DynamicSemaphore | None = None
        self._ffmpeg_sem: DynamicSemaphore | None = None
        self._active_downloads = 0
        self._active_extracts = 0
        self._active_video_jobs = 0
        self._active_ffmpeg = 0
        self._active_stream_chats: set[int] = set()
        #: Chats whose slot consumed a dynamic streams-lane permit, so release
        #: stays symmetric even if the controller is toggled or degrades.
        self._stream_lane_chats: set[int] = set()
        self._lock = asyncio.Lock()
        self._last_snap: LoadSnapshot | None = None
        self._last_snap_at = 0.0
        self._last_band = "low"

    # --- fixed env limits: the guaranteed fallback contract ---

    def fixed_ytdlp(self) -> int:
        return max(1, int(getattr(config, "MAX_YTDLP_CONCURRENT", 2) or 2))

    def fixed_downloads(self) -> int:
        return max(1, int(getattr(config, "MAX_DOWNLOAD_CONCURRENT", 2) or 2))

    def fixed_ffmpeg(self) -> int:
        return max(1, int(getattr(config, "MAX_FFMPEG_CONCURRENT", 3) or 3))

    def fixed_video_jobs(self) -> int:
        return max(1, int(getattr(config, "MAX_VIDEO_JOBS", 1) or 1))

    def fixed_active_streams(self) -> int:
        return max(1, int(getattr(config, "MAX_ACTIVE_STREAMS", 20) or 20))

    # --- effective limits: live dynamic capacity, fixed on fallback ---

    def _effective(self, lane: str, fixed: int) -> int:
        try:
            if dynamic_capacity.enabled():
                return max(1, int(dynamic_capacity.capacity(lane)))
        except Exception:
            pass
        return fixed

    def max_ytdlp(self) -> int:
        return self._effective("ytdlp", self.fixed_ytdlp())

    def max_downloads(self) -> int:
        return self._effective("downloads", self.fixed_downloads())

    def max_ffmpeg(self) -> int:
        return self._effective("ffmpeg", self.fixed_ffmpeg())

    def max_video_jobs(self) -> int:
        return self._effective("video", self.fixed_video_jobs())

    def max_active_streams(self) -> int:
        return self._effective("streams", self.fixed_active_streams())

    def max_media_duration_sec(self) -> int:
        return max(60, int(getattr(config, "MAX_MEDIA_DURATION_SEC", 7200) or 7200))

    def max_download_mb(self) -> int:
        return max(16, int(getattr(config, "MAX_DOWNLOAD_MB", 2048) or 2048))

    def disk_high_water_pct(self) -> float:
        return min(98.0, max(50.0, float(getattr(config, "DISK_HIGH_WATER_PCT", 80) or 80)))

    def disk_target_pct(self) -> float:
        # Free down toward this after high-water triggered
        return min(self.disk_high_water_pct() - 5, float(getattr(config, "DISK_TARGET_PCT", 75) or 75))

    def _ensure_semaphores(self) -> None:
        if self._extract_sem is None:
            self._extract_sem = DynamicSemaphore("ytdlp")
        if self._download_sem is None:
            self._download_sem = DynamicSemaphore("downloads")
        if self._video_sem is None:
            self._video_sem = DynamicSemaphore("video")
        if self._ffmpeg_sem is None:
            self._ffmpeg_sem = DynamicSemaphore("ffmpeg")

    def extract_semaphore(self) -> DynamicSemaphore:
        self._ensure_semaphores()
        assert self._extract_sem is not None
        return self._extract_sem

    def download_semaphore(self) -> DynamicSemaphore:
        self._ensure_semaphores()
        assert self._download_sem is not None
        return self._download_sem

    def video_semaphore(self) -> DynamicSemaphore:
        self._ensure_semaphores()
        assert self._video_sem is not None
        return self._video_sem

    def ffmpeg_semaphore(self) -> DynamicSemaphore:
        self._ensure_semaphores()
        assert self._ffmpeg_sem is not None
        return self._ffmpeg_sem

    def note_extract(self, delta: int) -> None:
        self._active_extracts = max(0, self._active_extracts + delta)

    def note_download(self, delta: int) -> None:
        self._active_downloads = max(0, self._active_downloads + delta)

    def note_video_job(self, delta: int) -> None:
        self._active_video_jobs = max(0, self._active_video_jobs + delta)

    def note_ffmpeg(self, delta: int) -> None:
        self._active_ffmpeg = max(0, self._active_ffmpeg + delta)

    def register_stream(self, chat_id: int) -> bool:
        """Reserve one VC stream slot. Idempotent per chat.

        Admission runs through the dynamic ``streams`` lane, so the live count,
        the planner's demand signal and ``/health`` all read one source of truth
        instead of a private set the planner could not see. ``MAX_ACTIVE_STREAMS``
        is the *baseline*: filling it is what makes the lane grow, bounded by the
        runtime-derived safe ceiling. An already-registered chat is always
        re-admitted, and a shrink only throttles new admissions — a live VC never
        loses its slot.
        """
        key = int(chat_id)
        if key in self._active_stream_chats:
            return True
        if not self._admit_stream(key):
            return False
        self._active_stream_chats.add(key)
        return True

    def _admit_stream(self, key: int) -> bool:
        """Take one streams-lane permit, or fall back to the fixed limit."""
        try:
            if dynamic_capacity.enabled():
                if not dynamic_capacity.try_admit("streams"):
                    return False
                # Remember that *this* chat consumed a lane permit so release is
                # exactly symmetric even if the controller degrades meanwhile.
                self._stream_lane_chats.add(key)
                return True
        except Exception:
            pass
        return len(self._active_stream_chats) < self.fixed_active_streams()

    def reserve_stream(self, chat_id: int) -> StreamReservation:
        """Reserve a slot before starting the call; release it if start fails.

        This is the single stream admission point. ``calls._play_with_startup_slot``
        is the only VC-start path, so direct, local and cache playback all obey
        exactly the same decision.
        """
        key = int(chat_id)
        already = key in self._active_stream_chats
        admitted = self.register_stream(key)
        return StreamReservation(
            chat_id=key,
            admitted=admitted,
            created=admitted and not already,
            manager=self,
        )

    def can_admit_stream(self, chat_id: int) -> bool:
        """Would this chat get a stream slot right now? Non-binding.

        Used as a cheap pre-flight so a saturated box refuses before a resolve
        or a download is paid for. It does not take the slot — the binding
        reservation is :meth:`reserve_stream` at VC start — but it does record
        the interest as stream demand, so probing a full baseline is exactly
        what grows capacity on a healthy VPS instead of reporting "busy". A
        chat that already streams always passes.
        """
        key = int(chat_id)
        if key in self._active_stream_chats:
            return True
        try:
            if dynamic_capacity.enabled():
                return bool(dynamic_capacity.probe_stream_admission())
        except Exception:  # pragma: no cover - never block playback
            return True
        return len(self._active_stream_chats) < self.fixed_active_streams()

    def unregister_stream(self, chat_id: int) -> None:
        key = int(chat_id)
        self._active_stream_chats.discard(key)
        if key in self._stream_lane_chats:
            self._stream_lane_chats.discard(key)
            try:
                dynamic_capacity.release("streams")
            except Exception:  # pragma: no cover - cleanup must never raise
                pass

    def active_stream_count(self) -> int:
        return len(self._active_stream_chats)

    def stream_scaling(self) -> dict[str, Any]:
        """Baseline / capacity / auto ceiling / active / reason for /health."""
        try:
            view = dict(dynamic_capacity.stream_view())
        except Exception:  # pragma: no cover - observability must not throw
            baseline = self.fixed_active_streams()
            view = {
                "baseline": baseline,
                "capacity": baseline,
                "auto_ceiling": baseline,
                "hard_ceiling": baseline,
                "waiting": 0,
                "scaling_reason": "unavailable",
            }
        # The chat set is the authority for "how many groups are streaming".
        view["active"] = self.active_stream_count()
        return view

    def can_start_ffmpeg(self) -> bool:
        return self._active_ffmpeg < self.max_ffmpeg()

    def snapshot(self, *, active_streams: int | None = None) -> LoadSnapshot:
        now = time.time()
        if self._last_snap and (now - self._last_snap_at) < 1.5:
            return self._last_snap

        cpu = 0.0
        ram_pct = 0.0
        free_mb = 0.0
        reason_parts: list[str] = []
        if psutil is not None:
            try:
                cpu = float(psutil.cpu_percent(interval=None))
            except Exception:
                cpu = 0.0
            try:
                vm = psutil.virtual_memory()
                ram_pct = float(vm.percent)
                free_mb = float(vm.available) / (1024 * 1024)
            except Exception:
                pass

        streams = int(
            active_streams
            if active_streams is not None
            else len(self._active_stream_chats)
        )
        high_cpu = float(getattr(config, "ADAPTIVE_CPU_HIGH", 70) or 70)
        recover_cpu = float(getattr(config, "ADAPTIVE_CPU_RECOVER", 55) or 55)
        high_ram = float(getattr(config, "ADAPTIVE_RAM_HIGH", 88) or 88)
        recover_ram = float(getattr(config, "ADAPTIVE_RAM_RECOVER", 78) or 78)
        disk_pct = self.disk_usage_pct()
        download_pressure = self._active_downloads >= self.max_downloads()
        extract_pressure = self._active_extracts >= self.max_ytdlp()
        stream_pressure = streams >= self.max_active_streams()
        disk_pressure = (
            disk_pct is not None and disk_pct >= self.disk_high_water_pct()
        )

        if cpu >= high_cpu:
            reason_parts.append("cpu_high")
        if ram_pct >= high_ram:
            reason_parts.append("ram_high")
        if disk_pressure:
            reason_parts.append("disk_high")
        if download_pressure:
            reason_parts.append("downloads_full")
        if extract_pressure:
            reason_parts.append("extracts_full")
        if stream_pressure:
            reason_parts.append("streams_full")

        if reason_parts:
            band = "high"
        else:
            moderate = (
                cpu >= recover_cpu
                or ram_pct >= recover_ram
                or self._active_downloads >= max(1, int(self.max_downloads() * 0.75))
                or self._active_extracts >= max(1, int(self.max_ytdlp() * 0.75))
                or streams >= max(1, int(self.max_active_streams() * 0.75))
            )
            if moderate:
                band = "medium"
                reason_parts.append("moderate_pressure")
            elif self._last_band == "high":
                # One sample of recovery remains balanced to prevent quality
                # oscillation after a transient spike.
                band = "medium"
                reason_parts.append("recovery_hysteresis")
            else:
                band = "low"
                reason_parts.append("healthy")
        self._last_band = band

        snap = LoadSnapshot(
            band=band,
            cpu_percent=cpu,
            ram_percent=ram_pct,
            free_ram_mb=free_mb,
            active_downloads=self._active_downloads,
            active_extracts=self._active_extracts,
            active_streams=streams,
            reason=",".join(reason_parts) or "n/a",
        )
        self._last_snap = snap
        self._last_snap_at = now
        try:
            from AnonX_3.core.metrics import (
                observe_cpu_pct,
                observe_disk_usage_pct,
                observe_ram_pct,
                set_active_downloads,
                set_active_extracts,
                set_active_streams,
            )

            observe_cpu_pct(cpu)
            observe_ram_pct(ram_pct)
            observe_disk_usage_pct(disk_pct)
            set_active_downloads(self._active_downloads)
            set_active_extracts(self._active_extracts)
            set_active_streams(streams)
        except Exception:
            pass
        return snap

    def quality_tier_for_load(self, preferred: str | None = None) -> str:
        """Map load band → poor/normal/good download/play tier."""
        return self.select_quality_plan(preferred).tier

    def select_quality_plan(self, preferred: str | None = None) -> QualityPlan:
        """Single algorithm for audio/video quality + concurrency under load."""
        snap = self.snapshot()
        band = snap.band
        pref = (preferred or "").strip().lower()
        if pref not in {"poor", "normal", "good"}:
            pref = "good" if band == "low" else ("normal" if band == "medium" else "poor")

        if band == "high":
            tier = "poor"
            audio = "low"
            height = 360
            allow_pf = False
            allow_vpf = False
            max_dl = 1
            reason = f"high_load:{snap.reason}"
        elif band == "medium":
            tier = "poor" if pref == "poor" else "normal"
            audio = "medium"
            height = 480
            allow_pf = True
            allow_vpf = False
            max_dl = max(1, self.max_downloads() - 1)
            reason = f"medium_load:{snap.reason}"
        else:
            tier = pref if pref in {"poor", "normal", "good"} else "good"
            audio = "best" if tier == "good" else ("medium" if tier == "normal" else "low")
            height = 720 if tier == "good" else (480 if tier == "normal" else 360)
            allow_pf = True
            allow_vpf = tier == "good"
            max_dl = self.max_downloads()
            reason = f"low_load:{snap.reason}"

        return QualityPlan(
            tier=tier,
            band=band,
            audio_preference=audio,
            video_max_height=height,
            allow_prefetch=allow_pf,
            allow_video_prefetch=allow_vpf,
            max_parallel_downloads=max_dl,
            reason=reason,
        )

    def allow_background_cache(self) -> bool:
        return self.snapshot().band != "high"

    def allow_prefetch_next(self) -> bool:
        return self.snapshot().band == "low"

    def allow_prefetch_video(self) -> bool:
        return self.snapshot().band == "low"

    def allow_new_heavy_job(
        self, *, video: bool = False, priority: int | None = None
    ) -> bool:
        """Backpressure gate for starting a new download/extract job.

        Foreground ``/play`` and ``/vplay`` are never rejected here: they queue
        on the (still bounded) lane semaphore instead, so a busy moment delays a
        song by seconds rather than failing it. Only background cache/prefetch
        work is shed, which is exactly the work we want to drop under pressure.
        """
        if priority is None:
            priority = current_priority()
        if int(priority) <= int(Priority.FOREGROUND):
            return True

        snap = self.snapshot()
        if snap.band == "high" and video:
            return False
        if self._active_downloads >= self.max_downloads():
            return False
        if video and self._active_video_jobs >= self.max_video_jobs():
            return False
        return True

    def disk_usage_pct(self, path: str | os.PathLike[str] | None = None) -> float | None:
        if psutil is None:
            return None
        try:
            target = str(path or getattr(config, "CDN_MEDIA_ROOT", "media") or "media")
            if not os.path.isabs(target):
                target = os.path.join(os.getcwd(), target)
            # Walk up until path exists
            p = target
            while p and not os.path.exists(p):
                parent = os.path.dirname(p)
                if parent == p:
                    break
                p = parent
            usage = psutil.disk_usage(p or os.getcwd())
            return float(usage.percent)
        except Exception:
            return None

    def over_disk_high_water(self, path: str | os.PathLike[str] | None = None) -> bool:
        pct = self.disk_usage_pct(path)
        if pct is None:
            return False
        return pct >= self.disk_high_water_pct()

    def stats(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "band": snap.band,
            "cpu": snap.cpu_percent,
            "ram": snap.ram_percent,
            "active_downloads": self._active_downloads,
            "active_extracts": self._active_extracts,
            "active_video_jobs": self._active_video_jobs,
            "limits": {
                "ytdlp": self.max_ytdlp(),
                "downloads": self.max_downloads(),
                "video": self.max_video_jobs(),
                "ffmpeg": self.max_ffmpeg(),
                "streams": self.max_active_streams(),
            },
            "fixed_limits": {
                "ytdlp": self.fixed_ytdlp(),
                "downloads": self.fixed_downloads(),
                "video": self.fixed_video_jobs(),
                "ffmpeg": self.fixed_ffmpeg(),
                "streams": self.fixed_active_streams(),
            },
            "dynamic": self.dynamic_stats(),
            "disk_pct": self.disk_usage_pct(),
        }

    # --- dynamic resource control surface ---

    def dynamic_stats(self) -> dict[str, Any]:
        """Live capacity/active/waiting view, or the degraded fallback marker."""
        try:
            return dynamic_capacity.snapshot()
        except Exception as ex:  # pragma: no cover - defensive
            return {"mode": "fixed", "degraded": True, "degraded_reason": str(ex)[:120]}

    def event_loop_lag_ms(self) -> float:
        try:
            return dynamic_capacity.loop_lag_ms()
        except Exception:
            return 0.0

    def start_dynamic_control(self) -> None:
        """Begin event-loop lag sampling. Safe to call more than once."""
        try:
            dynamic_capacity.start()
        except Exception:  # pragma: no cover - never block startup
            pass

    async def stop_dynamic_control(self) -> None:
        try:
            await dynamic_capacity.stop()
        except Exception:  # pragma: no cover
            pass


resource_manager = ResourceManager()
