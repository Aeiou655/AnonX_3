# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

"""Dynamic Resource Control for playback concurrency.

Fixed env limits (MAX_YTDLP_CONCURRENT, MAX_DOWNLOAD_CONCURRENT, ...) are the
*floor contract*, not the runtime value. This module raises capacity when many
groups play at once and the machine is idle, and lowers it when CPU/RAM/load or
event-loop lag says the box is hurting.

Design rules enforced here:

* Foreground first. ``/play`` and ``/vplay`` acquire with ``Priority.FOREGROUND``
  and are served ahead of every background cache/prefetch/thumbnail waiter.
  While any foreground waiter is queued, background admission is paused.
* Never cut a live job. Capacity shrink only removes *future* permits; running
  VC streams, FFmpeg processes and downloads keep their permit until they exit.
* Bounded always. Every lane has a hard ceiling derived from the machine and
  ``DYNAMIC_*_CEILING``; there is no unlimited-worker mode.
* Fail safe. Any internal error latches the whole controller to the fixed env
  limits for the rest of the process lifetime (``degraded`` state) and logs once.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import enum
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("AnonX_3")

try:  # pragma: no cover - psutil is a hard dep in prod, optional in tests
    import psutil
except Exception:  # pragma: no cover
    psutil = None


class Priority(enum.IntEnum):
    """Lower value wins. Foreground playback outranks every background job."""

    FOREGROUND = 0
    BACKGROUND = 1


#: Lane names managed by the controller.
LANES = ("ytdlp", "downloads", "video", "ffmpeg", "streams")

#: Background lanes pause while foreground work waits; ``streams`` is admission
#: only (no queueing) so it is never paused.
_PAUSABLE_LANES = ("ytdlp", "downloads", "video", "ffmpeg")

#: Ambient priority for the running task. Defaults to FOREGROUND so any call
#: site we have not explicitly tagged keeps today's behaviour — background work
#: opts *in* to deprioritisation, it is never inferred.
_PRIORITY: contextvars.ContextVar[int] = contextvars.ContextVar(
    "anonx_job_priority", default=int(Priority.FOREGROUND)
)


def current_priority() -> int:
    try:
        return int(_PRIORITY.get())
    except Exception:
        return int(Priority.FOREGROUND)


@contextlib.contextmanager
def priority_scope(priority: int):
    """Tag every acquire inside this block with ``priority``."""
    token = _PRIORITY.set(int(priority))
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _PRIORITY.reset(token)


def background_scope():
    """``with background_scope():`` — mark cache/prefetch/thumbnail work."""
    return priority_scope(Priority.BACKGROUND)


def foreground_scope():
    """``with foreground_scope():`` — mark /play and /vplay request work."""
    return priority_scope(Priority.FOREGROUND)



def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _cpu_count() -> int:
    try:
        n = os.cpu_count() or 1
    except Exception:
        n = 1
    return max(1, int(n))


def _total_ram_mb() -> float:
    if psutil is None:
        return 0.0
    try:
        return float(psutil.virtual_memory().total) / (1024 * 1024)
    except Exception:
        return 0.0


@dataclass
class Sample:
    """One observation of machine + runtime pressure."""

    at: float
    cpu_percent: float
    ram_percent: float
    load_per_core: float
    loop_lag_ms: float


@dataclass
class LaneState:
    """Live capacity bookkeeping for one lane."""

    name: str
    floor: int
    ceiling: int
    capacity: int
    active: int = 0
    #: Waiters per priority, FIFO within a priority.
    waiters: dict[int, deque] = field(default_factory=dict)

    def waiting(self, priority: int | None = None) -> int:
        if priority is None:
            return sum(len(q) for q in self.waiters.values())
        return len(self.waiters.get(priority, ()))

    def free(self) -> int:
        return max(0, self.capacity - self.active)


@dataclass
class CapacityPlan:
    """Result of one recompute pass."""

    limits: dict[str, int]
    pressure: float
    demand: float
    reason: str
    sample: Sample
    #: Runtime-derived safe ceiling for VC streams (see ``_stream_target``).
    stream_safe_ceiling: int = 0
    #: Hard machine bound the safe ceiling was derived from.
    stream_hard_ceiling: int = 0
    #: Human-readable explanation of the current stream scaling decision.
    stream_reason: str = ""


@dataclass
class _Waiter:
    """One queued acquisition, tracked so it can be promoted while waiting."""

    lane: str
    priority: int
    fut: asyncio.Future
    task: Any = None


class _LoopLagMonitor:
    """Measure event-loop scheduling delay without a dedicated thread.

    A short ``sleep`` that returns late means the loop is saturated — the single
    most reliable "audio will stutter soon" signal for a PyTgCalls bot, because
    it captures GIL contention that CPU% alone misses.
    """

    def __init__(self, interval: float = 1.0) -> None:
        self._interval = max(0.2, float(interval))
        self._task: asyncio.Task | None = None
        self._lag_ms = 0.0
        self._samples: deque[float] = deque(maxlen=10)

    @property
    def lag_ms(self) -> float:
        if not self._samples:
            return self._lag_ms
        # Use the worst of the recent window: a single stall is what breaks VC.
        return max(self._samples)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run(), name="dynamic_capacity_lag")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _run(self) -> None:
        while True:
            started = time.perf_counter()
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            elapsed = time.perf_counter() - started
            lag = max(0.0, (elapsed - self._interval) * 1000.0)
            self._lag_ms = lag
            self._samples.append(lag)

    def observe_external(self, lag_ms: float) -> None:
        """Feed a lag sample from outside (used by tests and by /ping)."""
        lag = max(0.0, float(lag_ms))
        self._lag_ms = lag
        self._samples.append(lag)


class DynamicCapacityController:
    """Scale playback concurrency with live pressure and live demand."""

    def __init__(self) -> None:
        self._lanes: dict[str, LaneState] = {}
        self._lock: asyncio.Lock | None = None
        self._lag = _LoopLagMonitor()
        self._degraded = False
        self._degraded_reason = ""
        self._last_plan: CapacityPlan | None = None
        self._last_recompute = 0.0
        self._last_cpu_sample = 0.0
        self._cpu_cached = 0.0
        self._ram_cached = 0.0
        self._foreground_waiting = 0
        self._initialized = False
        #: Stream requests currently blocked waiting for a slot (explicit
        #: waiters), plus a short decaying window of recent admission refusals.
        #: A refused ``/play`` *is* a waiting stream request even though the
        #: streams lane never queues, so both feed stream demand.
        self._stream_waiting = 0
        self._stream_refusals: deque[float] = deque(maxlen=128)
        #: Queued acquisitions keyed by the task that is waiting, so a shared
        #: single-flight worker can be promoted when foreground joins it.
        self._waiters_by_task: dict[Any, list[_Waiter]] = {}
        self._stats = {
            "recomputes": 0,
            "scale_ups": 0,
            "scale_downs": 0,
            "background_pauses": 0,
            "foreground_preempts": 0,
            "priority_promotions": 0,
            "stream_refusals": 0,
            "stream_rejections": 0,
        }

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    def _cfg(self, name: str, default: Any) -> Any:
        try:
            from AnonX_3 import config

            value = getattr(config, name, default)
            return default if value is None else value
        except Exception:
            return default

    def enabled(self) -> bool:
        if self._degraded:
            return False
        return bool(self._cfg("DYNAMIC_RESOURCE_CONTROL", True))

    def _fixed_limits(self) -> dict[str, int]:
        """The env-configured limits — the guaranteed fallback contract."""
        return {
            "ytdlp": max(1, int(self._cfg("MAX_YTDLP_CONCURRENT", 2) or 2)),
            "downloads": max(1, int(self._cfg("MAX_DOWNLOAD_CONCURRENT", 2) or 2)),
            "video": max(1, int(self._cfg("MAX_VIDEO_JOBS", 1) or 1)),
            "ffmpeg": max(1, int(self._cfg("MAX_FFMPEG_CONCURRENT", 3) or 3)),
            "streams": max(1, int(self._cfg("MAX_ACTIVE_STREAMS", 20) or 20)),
        }

    def _ceilings(self, fixed: dict[str, int]) -> dict[str, int]:
        """Hard upper bounds. Never unlimited.

        Default ceilings scale with the machine (cores / RAM) but stay bounded by
        an explicit multiplier so a misconfigured box cannot spawn a fleet.
        """
        cores = _cpu_count()
        ram_mb = _total_ram_mb()
        mult = _clamp(float(self._cfg("DYNAMIC_CAPACITY_MAX_MULTIPLIER", 4.0) or 4.0), 1.0, 8.0)

        # RAM headroom: each concurrent yt-dlp/ffmpeg job costs roughly 150 MB.
        ram_jobs = int(ram_mb // 150) if ram_mb > 0 else cores * 2
        ram_jobs = max(1, ram_jobs)

        def bound(lane: str, machine_hint: int) -> int:
            override = self._cfg(f"DYNAMIC_{lane.upper()}_CEILING", 0)
            try:
                override = int(override or 0)
            except Exception:
                override = 0
            if override > 0:
                return max(fixed[lane], override)
            derived = int(min(machine_hint, ram_jobs, fixed[lane] * mult))
            return max(fixed[lane], derived)

        return {
            "ytdlp": bound("ytdlp", cores * 2),
            "downloads": bound("downloads", cores * 2),
            "video": bound("video", max(1, cores // 2)),
            "ffmpeg": bound("ffmpeg", cores * 2),
            "streams": self._stream_ceiling(fixed["streams"], cores, ram_mb, mult),
        }

    def _stream_ceiling(
        self, base: int, cores: int, ram_mb: float, mult: float
    ) -> int:
        """Hard machine bound for concurrent VC streams.

        A relayed VC stream is *not* a yt-dlp/ffmpeg job: it costs one ntgcalls
        pipe plus its share of the event loop, not ~150 MB of decode buffers.
        Sizing it off the download lane's ``ram_mb // 150`` unit pins a 2 GB VPS
        to 13 — below the configured baseline of 20 — so streams could never
        grow at all. Streams get their own, much cheaper unit.

        ``DYNAMIC_STREAMS_CEILING=0`` means *derive*; a positive value is an
        explicit operator override. Either way the result stays bounded by
        ``base * mult`` — there is no unlimited mode.
        """
        override = self._cfg("DYNAMIC_STREAMS_CEILING", 0)
        try:
            override = int(override or 0)
        except Exception:
            override = 0
        if override > 0:
            return max(base, override)

        # Per-stream cost, in MB of RAM and in fraction of a core. Both are
        # deliberately conservative: they bound the *ceiling*, and live pressure
        # is what actually decides how close we get to it.
        ram_per_stream = max(
            1.0, float(self._cfg("DYNAMIC_STREAM_RAM_MB", 24.0) or 24.0)
        )
        per_core = max(1, int(self._cfg("DYNAMIC_STREAMS_PER_CORE", 16) or 16))

        # Leave the OS and the bot process their own headroom before dividing.
        reserved_mb = max(0.0, float(self._cfg("DYNAMIC_STREAM_RAM_RESERVE_MB", 512.0) or 512.0))
        usable_mb = max(0.0, ram_mb - reserved_mb)
        ram_streams = int(usable_mb // ram_per_stream) if ram_mb > 0 else cores * per_core

        derived = int(min(cores * per_core, max(1, ram_streams), base * mult))
        return max(base, derived)

    def _floors(self, fixed: dict[str, int]) -> dict[str, int]:
        """Minimum capacity under maximum pressure.

        Never below 1, and never above the fixed limit — under pressure we must
        be able to fall all the way back to (or below) the configured values so
        a quiet box is genuinely cheap.
        """
        floors: dict[str, int] = {}
        for lane, value in fixed.items():
            raw = self._cfg(f"DYNAMIC_{lane.upper()}_FLOOR", 0)
            try:
                raw = int(raw or 0)
            except Exception:
                raw = 0
            if raw > 0:
                floors[lane] = max(1, min(raw, value))
            elif lane == "streams":
                # Existing sessions must never be evicted by a shrink; keep the
                # configured stream ceiling as the floor for admission.
                floors[lane] = value
            else:
                floors[lane] = 1
        return floors

    # ------------------------------------------------------------------
    # pressure sampling
    # ------------------------------------------------------------------

    def _sample(self) -> Sample:
        now = time.monotonic()
        cpu = self._cpu_cached
        ram = self._ram_cached
        if psutil is not None and (now - self._last_cpu_sample) >= 1.0:
            try:
                cpu = float(psutil.cpu_percent(interval=None))
            except Exception:
                cpu = self._cpu_cached
            try:
                ram = float(psutil.virtual_memory().percent)
            except Exception:
                ram = self._ram_cached
            self._cpu_cached = cpu
            self._ram_cached = ram
            self._last_cpu_sample = now

        load_per_core = 0.0
        try:
            getloadavg = getattr(os, "getloadavg", None)
            if getloadavg is not None:
                load_per_core = float(getloadavg()[0]) / _cpu_count()
        except Exception:
            load_per_core = 0.0

        return Sample(
            at=now,
            cpu_percent=cpu,
            ram_percent=ram,
            load_per_core=load_per_core,
            loop_lag_ms=self._lag.lag_ms,
        )

    def _pressure(self, sample: Sample) -> tuple[float, str]:
        """Normalise pressure to 0.0 (idle) .. 1.0 (saturated).

        The maximum of the individual signals wins: one saturated dimension is
        enough to stutter audio, and averaging would hide it.
        """
        cpu_high = float(self._cfg("ADAPTIVE_CPU_HIGH", 70) or 70)
        ram_high = float(self._cfg("ADAPTIVE_RAM_HIGH", 88) or 88)
        lag_high = float(self._cfg("DYNAMIC_LOOP_LAG_HIGH_MS", 250) or 250)
        load_high = float(self._cfg("DYNAMIC_LOAD_PER_CORE_HIGH", 1.5) or 1.5)

        signals = {
            "cpu": _clamp(sample.cpu_percent / cpu_high, 0.0, 1.5) if cpu_high > 0 else 0.0,
            "ram": _clamp(sample.ram_percent / ram_high, 0.0, 1.5) if ram_high > 0 else 0.0,
            "lag": _clamp(sample.loop_lag_ms / lag_high, 0.0, 1.5) if lag_high > 0 else 0.0,
            "load": _clamp(sample.load_per_core / load_high, 0.0, 1.5) if load_high > 0 else 0.0,
        }
        worst_name = max(signals, key=lambda k: signals[k])
        worst = _clamp(signals[worst_name], 0.0, 1.0)
        return worst, worst_name

    def _demand(self) -> tuple[float, int, int]:
        """How much work is queued relative to what we can currently serve.

        Streams are deliberately excluded from this ratio: they are admission
        only (no queue), and folding stream pressure into the download/ffmpeg
        growth term would inflate lanes that a VC stream does not consume.
        Stream demand is computed separately by :meth:`_stream_demand`.
        """
        total_wait = 0
        total_cap = 0
        for lane in _PAUSABLE_LANES:
            state = self._lanes.get(lane)
            if state is None:
                continue
            total_wait += state.waiting()
            total_cap += max(1, state.capacity)
        active_streams, _ = self._stream_demand()
        if total_cap <= 0:
            return 0.0, total_wait, active_streams
        return _clamp(total_wait / float(total_cap), 0.0, 2.0), total_wait, active_streams

    def _stream_demand(self) -> tuple[int, int]:
        """``(active, waiting)`` for the streams lane.

        ``waiting`` is the real thing the stream ceiling has to react to: the
        streams lane is non-blocking, so a group whose ``/play`` was refused
        does not sit in ``LaneState.waiters`` — it is simply gone. Counting
        recent refusals inside a short window turns those invisible rejections
        into the demand signal that grows capacity, and each later admission
        retires one refusal so demand does not double-count.
        """
        state = self._lanes.get("streams")
        active = state.active if state else 0
        try:
            window = float(self._cfg("DYNAMIC_STREAM_DEMAND_WINDOW_SEC", 20.0) or 20.0)
        except Exception:
            window = 20.0
        cutoff = time.monotonic() - max(1.0, window)
        while self._stream_refusals and self._stream_refusals[0] < cutoff:
            self._stream_refusals.popleft()
        waiting = max(0, int(self._stream_waiting)) + len(self._stream_refusals)
        return active, waiting

    # ------------------------------------------------------------------
    # capacity planning
    # ------------------------------------------------------------------

    def _plan(self) -> CapacityPlan:
        fixed = self._fixed_limits()
        floors = self._floors(fixed)
        ceilings = self._ceilings(fixed)
        sample = self._sample()
        pressure, worst = self._pressure(sample)
        demand, waiting, active_streams = self._demand()

        # Headroom shrinks as pressure grows. Above the release threshold we
        # actively cut below the configured limits to protect live audio.
        relief = float(self._cfg("DYNAMIC_PRESSURE_RELIEF", 0.85) or 0.85)
        grow_below = float(self._cfg("DYNAMIC_PRESSURE_GROW_BELOW", 0.55) or 0.55)

        limits: dict[str, int] = {}
        for lane in LANES:
            if lane == "streams":
                # Streams do not follow the generic job curve: they are admission
                # only, they must never shrink below the live session count, and
                # their growth is driven by refused /play requests rather than by
                # a queue. `_stream_target` owns the whole decision.
                continue
            base = fixed[lane]
            floor = floors[lane]
            ceiling = ceilings[lane]

            if pressure >= relief:
                # Hard pressure: fall to the floor, protect playing streams.
                target = floor
            elif pressure >= grow_below:
                # Between grow and relief: interpolate down from base to floor.
                span = max(1e-6, relief - grow_below)
                ratio = (pressure - grow_below) / span
                target = base - (base - floor) * ratio
            else:
                # Idle-to-moderate: grow with demand toward the ceiling.
                # No demand ⇒ stay at (or below) the configured base so a quiet
                # bot is cheap; heavy multi-group demand ⇒ climb to ceiling.
                idle_ratio = 1.0 - (pressure / max(1e-6, grow_below))
                growth = _clamp(demand, 0.0, 1.0) * idle_ratio
                target = base + (ceiling - base) * growth

            limits[lane] = int(_clamp(round(target), floor, ceiling))

        stream_target, safe_ceiling, stream_reason = self._stream_target(
            base=fixed["streams"],
            floor=floors["streams"],
            hard_ceiling=ceilings["streams"],
            pressure=pressure,
            worst=worst,
            relief=relief,
        )
        # Stream admission never shrinks below the configured maximum, so a
        # transient CPU spike can never reject a group that would have been
        # accepted a second earlier.
        limits["streams"] = max(stream_target, fixed["streams"])

        reason = (
            f"pressure={pressure:.2f}({worst}) demand={demand:.2f} "
            f"waiting={waiting} streams={active_streams}"
        )
        return CapacityPlan(
            limits=limits,
            pressure=pressure,
            demand=demand,
            reason=reason,
            sample=sample,
            stream_safe_ceiling=safe_ceiling,
            stream_hard_ceiling=ceilings["streams"],
            stream_reason=stream_reason,
        )

    def _stream_target(
        self,
        *,
        base: int,
        floor: int,
        hard_ceiling: int,
        pressure: float,
        worst: str,
        relief: float,
    ) -> tuple[int, int, str]:
        """Decide live stream capacity. Returns ``(target, safe_ceiling, why)``.

        ``MAX_ACTIVE_STREAMS`` is the *baseline*, not the cap. Two bounds apply:

        * ``hard_ceiling`` — what the machine could ever host (cores/RAM, see
          :meth:`_stream_ceiling`). Static for the process.
        * ``safe_ceiling`` — how much of that hard bound the box can bear *right
          now*, scaled by live headroom on the worst of CPU / RAM / loadavg /
          event-loop lag. At zero pressure it is the hard ceiling; at the relief
          threshold it collapses to the baseline.

        Inside that safe ceiling, capacity tracks real demand — live streams plus
        the stream requests that are waiting (or were just refused) — with a
        small spare-slot headroom so the next ``/play`` is admitted instantly
        rather than after the next recompute tick. So filling the baseline 20 is
        itself the growth trigger, and growth stops exactly where the machine
        says stop; nothing is pinned to a hardcoded number.

        Under relief pressure the target drops back to the baseline: that throttles
        *new admissions* only. Capacity is never lowered below the live session
        count, and ``recompute`` only ever moves ``capacity`` — never ``active``
        — so no playing VC is cut.
        """
        active, waiting = self._stream_demand()

        headroom = _clamp((relief - pressure) / max(1e-6, relief), 0.0, 1.0)
        safe_ceiling = int(
            _clamp(round(base + (hard_ceiling - base) * headroom), floor, hard_ceiling)
        )

        if pressure >= relief:
            target = floor
            mode = "relief"
        else:
            try:
                spare = int(self._cfg("DYNAMIC_STREAM_HEADROOM", 2) or 0)
            except Exception:
                spare = 2
            spare = max(0, min(spare, 8))
            want = active + waiting + spare
            target = int(_clamp(want, base, safe_ceiling))
            mode = "grow" if target > base else "baseline"

        # Never advertise less capacity than there are live sessions: a shrink
        # must throttle admission, never imply an eviction.
        target = max(target, floor, active)

        why = (
            f"{mode}: active={active} waiting={waiting} capacity={target} "
            f"baseline={base} safe_ceiling={safe_ceiling} hard_ceiling={hard_ceiling} "
            f"headroom={headroom:.2f} pressure={pressure:.2f}({worst})"
        )
        return target, safe_ceiling, why

    def _ensure_lanes(self) -> None:
        if self._initialized:
            return
        fixed = self._fixed_limits()
        floors = self._floors(fixed)
        ceilings = self._ceilings(fixed)
        for lane in LANES:
            self._lanes[lane] = LaneState(
                name=lane,
                floor=floors[lane],
                ceiling=ceilings[lane],
                capacity=fixed[lane],
            )
        self._initialized = True

    def recompute(self, *, force: bool = False) -> CapacityPlan | None:
        """Refresh capacity. Cheap enough to call on every acquire."""
        if not self.enabled():
            return None
        try:
            self._ensure_lanes()
            now = time.monotonic()
            interval = float(self._cfg("DYNAMIC_RECOMPUTE_INTERVAL_SEC", 2.0) or 2.0)
            if not force and self._last_plan and (now - self._last_recompute) < interval:
                return self._last_plan

            plan = self._plan()
            for lane, target in plan.limits.items():
                state = self._lanes.get(lane)
                if state is None:
                    continue
                previous = state.capacity
                # Only *future* permits move. `active` is untouched, so a shrink
                # below the live count never revokes a running VC stream, FFmpeg
                # process or download — they simply keep their permit until exit.
                state.capacity = target
                if target > previous:
                    self._stats["scale_ups"] += 1
                elif target < previous:
                    self._stats["scale_downs"] += 1

            self._last_plan = plan
            self._last_recompute = now
            self._stats["recomputes"] += 1

            # Growth may have freed permits for queued waiters.
            for lane in LANES:
                self._wake(lane)
            return plan
        except Exception as ex:  # pragma: no cover - defensive
            self._degrade(f"recompute_failed: {ex}")
            return None

    def _degrade(self, reason: str) -> None:
        """Latch to fixed env limits permanently and log once."""
        if self._degraded:
            return
        self._degraded = True
        self._degraded_reason = reason
        try:
            fixed = self._fixed_limits()
            for lane, value in fixed.items():
                state = self._lanes.get(lane)
                if state is not None:
                    state.capacity = value
            logger.error(
                "dynamic_capacity degraded to fixed limits (%s); limits=%s",
                reason,
                fixed,
            )
        except Exception:  # pragma: no cover
            logger.error("dynamic_capacity degraded (%s) and fixed limits unreadable", reason)

    # ------------------------------------------------------------------
    # admission
    # ------------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def capacity(self, lane: str) -> int:
        self._ensure_lanes()
        if not self.enabled():
            return self._fixed_limits().get(lane, 1)
        state = self._lanes.get(lane)
        return state.capacity if state else 1

    def active(self, lane: str) -> int:
        state = self._lanes.get(lane)
        return state.active if state else 0

    def waiting(self, lane: str) -> int:
        state = self._lanes.get(lane)
        return state.waiting() if state else 0

    def foreground_waiting(self) -> int:
        """How many foreground /play or /vplay requests are queued right now."""
        return int(self._foreground_waiting)

    async def defer_background(
        self, *, timeout: float = 2.0, poll: float = 0.05
    ) -> bool:
        """Pause background CPU work while foreground playback is waiting.

        For background work that holds no lane permit — thumbnail rendering is
        the one that matters, because its PIL pass fights the event loop for the
        GIL and that is what makes VC audio stutter. Bounded by ``timeout``, so
        such work is always merely delayed, never starved, and can never
        deadlock playback.

        Returns True when it actually waited.
        """
        try:
            if not self.enabled():
                return False
            if not bool(self._cfg("DYNAMIC_BACKGROUND_PAUSE", True)):
                return False
            if self._foreground_waiting <= 0:
                return False
            self._stats["background_pauses"] += 1
            deadline = time.monotonic() + max(0.0, float(timeout))
            step = max(0.01, float(poll))
            while self._foreground_waiting > 0 and time.monotonic() < deadline:
                await asyncio.sleep(step)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - never block background work
            return False

    def _background_paused(self, lane: str) -> bool:
        """True while a foreground waiter is queued anywhere.

        Foreground /play and /vplay must never sit behind a thumbnail render, so
        background admission stops globally — not just in the contended lane —
        until every foreground waiter has a permit.
        """
        if lane not in _PAUSABLE_LANES:
            return False
        if self._foreground_waiting <= 0:
            return False
        if not bool(self._cfg("DYNAMIC_BACKGROUND_PAUSE", True)):
            return False
        return True

    def _can_admit(self, lane: str, priority: int) -> bool:
        state = self._lanes.get(lane)
        if state is None:
            return True
        if state.free() <= 0:
            return False
        if priority > Priority.FOREGROUND:
            if self._background_paused(lane):
                return False
            # Never let background take the last permit out from under a
            # foreground waiter in this lane.
            if state.waiting(Priority.FOREGROUND) > 0:
                return False
            reserve = int(self._cfg("DYNAMIC_FOREGROUND_RESERVE", 1) or 0)
            if reserve > 0 and state.capacity > reserve and state.free() <= reserve:
                return False
        return True

    def _wake(self, lane: str) -> None:
        """Hand free permits to queued waiters, strict priority then FIFO."""
        state = self._lanes.get(lane)
        if state is None:
            return
        for priority in sorted(state.waiters):
            queue = state.waiters[priority]
            while queue and self._can_admit(lane, priority):
                fut = queue.popleft()
                if fut.done():
                    continue
                state.active += 1
                fut.set_result(True)
            if queue and priority == Priority.FOREGROUND:
                # Foreground still starved — do not serve background below it.
                return

    async def acquire(
        self,
        lane: str,
        *,
        priority: int | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Acquire one permit in ``lane``. Returns True when admitted."""
        if priority is None:
            priority = current_priority()
        if not self.enabled():
            return True
        try:
            self._ensure_lanes()
            self.recompute()
            state = self._lanes.get(lane)
            if state is None:
                return True

            if self._can_admit(lane, priority) and state.waiting() == 0:
                state.active += 1
                return True

            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            # `waiter.priority` is mutable: promote() can lift this exact
            # acquisition to foreground while it is queued, so the release
            # bookkeeping below must read the record, not the argument.
            waiter = _Waiter(lane=lane, priority=int(priority), fut=fut)
            state.waiters.setdefault(int(priority), deque()).append(fut)
            self._register_waiter(waiter)
            if waiter.priority == Priority.FOREGROUND:
                self._foreground_waiting += 1
                self._stats["foreground_preempts"] += 1
            else:
                if self._background_paused(lane):
                    self._stats["background_pauses"] += 1

            # A new waiter changes demand; re-plan so growth can serve it now.
            self.recompute(force=True)
            try:
                if timeout is not None:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
                else:
                    await fut
                return True
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not fut.done():
                    with contextlib.suppress(ValueError):
                        state.waiters.get(waiter.priority, deque()).remove(fut)
                    raise
                # Admitted concurrently with the timeout: keep the permit and
                # let the caller's finally release it.
                return True
            finally:
                self._unregister_waiter(waiter)
                if waiter.priority == Priority.FOREGROUND:
                    self._foreground_waiting = max(0, self._foreground_waiting - 1)
                    # Foreground drained ⇒ background may resume.
                    for other in _PAUSABLE_LANES:
                        self._wake(other)
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # pragma: no cover - defensive
            self._degrade(f"acquire_failed: {ex}")
            return True

    # ------------------------------------------------------------------
    # priority inheritance
    # ------------------------------------------------------------------

    def _register_waiter(self, waiter: _Waiter) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        waiter.task = task
        self._waiters_by_task.setdefault(task, []).append(waiter)

    def _unregister_waiter(self, waiter: _Waiter) -> None:
        task = waiter.task
        if task is None:
            return
        bucket = self._waiters_by_task.get(task)
        if not bucket:
            return
        with contextlib.suppress(ValueError):
            bucket.remove(waiter)
        if not bucket:
            self._waiters_by_task.pop(task, None)

    def promote(self, task: Any) -> int:
        """Lift a shared worker's queued permits to foreground priority.

        The same-song single-flight layer means one physical yt-dlp/download
        worker can be owned by a background prefetch and then *joined* by a
        foreground ``/play``.  Without this, the foreground request would
        silently inherit background admission and could sit behind the very
        background pause meant to protect it — a priority inversion.  Callers
        that join an existing worker call this first.

        Returns how many queued permits were promoted. Running permits need no
        promotion: they already hold capacity and are never revoked.
        """
        if task is None or not self.enabled():
            return 0
        try:
            promoted = 0
            for waiter in list(self._waiters_by_task.get(task, ())):
                if waiter.priority <= int(Priority.FOREGROUND):
                    continue
                state = self._lanes.get(waiter.lane)
                if state is None or waiter.fut.done():
                    continue
                queue = state.waiters.get(waiter.priority)
                if queue is not None:
                    with contextlib.suppress(ValueError):
                        queue.remove(waiter.fut)
                waiter.priority = int(Priority.FOREGROUND)
                state.waiters.setdefault(int(Priority.FOREGROUND), deque()).append(
                    waiter.fut
                )
                self._foreground_waiting += 1
                self._stats["priority_promotions"] += 1
                promoted += 1
                self._wake(waiter.lane)
            return promoted
        except Exception:  # pragma: no cover - never break a join
            return 0

    def promote_if_foreground(self, task: Any, *, priority: int | None = None) -> int:
        """``promote`` guarded by the joiner's own priority."""
        if priority is None:
            priority = current_priority()
        if int(priority) > int(Priority.FOREGROUND):
            return 0
        return self.promote(task)

    def release(self, lane: str) -> None:
        """Release one permit. Safe to call even after a degrade."""
        try:
            state = self._lanes.get(lane)
            if state is None:
                return
            state.active = max(0, state.active - 1)
            self._wake(lane)
        except Exception as ex:  # pragma: no cover - defensive
            self._degrade(f"release_failed: {ex}")

    def slot(self, lane: str, *, priority: int | None = None, timeout: float | None = None):
        """``async with controller.slot("downloads", priority=...)``."""
        return _Slot(self, lane, priority, timeout)

    # ------------------------------------------------------------------
    # admission for streams (non-blocking)
    # ------------------------------------------------------------------

    def try_admit(self, lane: str, *, priority: int = Priority.FOREGROUND) -> bool:
        """Non-blocking admission used by VC stream registration.

        For the streams lane this is the single growth trigger: filling the
        baseline is what makes capacity climb. A refusal is recorded as demand
        and the plan is refreshed *inside the same call*, so a healthy box
        admits the 21st group immediately instead of rejecting it and only
        growing on the next recompute tick.
        """
        if not self.enabled():
            return True
        try:
            self._ensure_lanes()
            self.recompute()
            state = self._lanes.get(lane)
            if state is None:
                return True
            if not self._can_admit(lane, priority):
                if lane != "streams":
                    return False
                self._stream_refusals.append(time.monotonic())
                self._stats["stream_refusals"] += 1
                self.recompute(force=True)
                if not self._can_admit(lane, priority):
                    # Genuinely out of safe capacity: throttle this *new*
                    # admission. Every live VC keeps its permit.
                    self._stats["stream_rejections"] += 1
                    return False
            if lane == "streams" and self._stream_refusals:
                # This admission satisfies one pending request, so demand does
                # not double-count it on the next plan.
                self._stream_refusals.popleft()
            state.active += 1
            return True
        except Exception as ex:  # pragma: no cover
            self._degrade(f"try_admit_failed: {ex}")
            return True

    def probe_stream_admission(self) -> bool:
        """Would a new VC stream be admitted right now?

        Read-only for ``active`` — the authoritative reservation still happens
        at VC start via :meth:`try_admit`. It *does* register the interest as
        demand and re-plan, exactly like ``try_admit``, so a caller that probes
        a full baseline triggers the same growth instead of being told "busy"
        while the box is idle. The matching ``try_admit`` retires that record,
        so probing then admitting counts the request once.
        """
        if not self.enabled():
            return True
        try:
            self._ensure_lanes()
            self.recompute()
            if self._can_admit("streams", Priority.FOREGROUND):
                return True
            self._stream_refusals.append(time.monotonic())
            self._stats["stream_refusals"] += 1
            self.recompute(force=True)
            return self._can_admit("streams", Priority.FOREGROUND)
        except Exception as ex:  # pragma: no cover - never block playback
            self._degrade(f"probe_stream_failed: {ex}")
            return True

    def stream_waiting(self) -> int:
        """Stream requests waiting for a slot (explicit waiters + refusals)."""
        try:
            self._ensure_lanes()
            _, waiting = self._stream_demand()
            return waiting
        except Exception:  # pragma: no cover - observability must not throw
            return 0

    @contextlib.contextmanager
    def stream_wait_scope(self):
        """Count the caller as a waiting stream request while it retries.

        ``with dynamic_capacity.stream_wait_scope():`` around a bounded admission
        retry makes that wait visible to the planner, so capacity grows for a
        caller that is genuinely blocked rather than one that gave up.
        """
        self._stream_waiting += 1
        try:
            yield
        finally:
            self._stream_waiting = max(0, self._stream_waiting - 1)

    def stream_view(self) -> dict[str, Any]:
        """Stream scaling view for /health: baseline → capacity → ceilings."""
        try:
            self._ensure_lanes()
            plan = self.recompute()
            fixed = self._fixed_limits()
            baseline = fixed["streams"]
            state = self._lanes.get("streams")
            active, waiting = self._stream_demand()
            if plan is None or not self.enabled():
                return {
                    "baseline": baseline,
                    "capacity": baseline,
                    "auto_ceiling": baseline,
                    "hard_ceiling": state.ceiling if state else baseline,
                    "active": active,
                    "waiting": waiting,
                    "scaling_reason": (
                        f"fixed: dynamic control off, capacity pinned to "
                        f"MAX_ACTIVE_STREAMS={baseline}"
                    ),
                }
            return {
                "baseline": baseline,
                "capacity": state.capacity if state else baseline,
                "auto_ceiling": plan.stream_safe_ceiling,
                "hard_ceiling": plan.stream_hard_ceiling,
                "active": active,
                "waiting": waiting,
                "scaling_reason": plan.stream_reason,
            }
        except Exception:  # pragma: no cover - observability must not throw
            baseline = self._fixed_limits().get("streams", 20)
            return {
                "baseline": baseline,
                "capacity": baseline,
                "auto_ceiling": baseline,
                "hard_ceiling": baseline,
                "active": 0,
                "waiting": 0,
                "scaling_reason": "unavailable",
            }

    # ------------------------------------------------------------------
    # lifecycle + observability
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin loop-lag sampling. Idempotent; safe without a running loop."""
        try:
            self._ensure_lanes()
            if self.enabled():
                self._lag.start()
        except Exception as ex:  # pragma: no cover
            self._degrade(f"start_failed: {ex}")

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self._lag.stop()

    def observe_loop_lag(self, lag_ms: float) -> None:
        with contextlib.suppress(Exception):
            self._lag.observe_external(lag_ms)

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason

    def loop_lag_ms(self) -> float:
        try:
            return round(float(self._lag.lag_ms), 2)
        except Exception:
            return 0.0

    def snapshot(self) -> dict[str, Any]:
        """Full view for /health: capacity, active, waiting, cpu, ram, lag."""
        try:
            self._ensure_lanes()
            plan = self.recompute()
            fixed = self._fixed_limits()
            lanes: dict[str, Any] = {}
            for lane in LANES:
                state = self._lanes.get(lane)
                if state is None:
                    continue
                lanes[lane] = {
                    "capacity": state.capacity if self.enabled() else fixed[lane],
                    "active": state.active,
                    "waiting": state.waiting(),
                    "waiting_foreground": state.waiting(Priority.FOREGROUND),
                    "waiting_background": state.waiting(Priority.BACKGROUND),
                    "floor": state.floor,
                    "ceiling": state.ceiling,
                    "fixed": fixed[lane],
                }
            sample = plan.sample if plan else self._sample()
            return {
                "mode": "fixed" if self._degraded else ("dynamic" if self.enabled() else "fixed"),
                "degraded": self._degraded,
                "degraded_reason": self._degraded_reason or None,
                "pressure": round(plan.pressure, 3) if plan else None,
                "demand": round(plan.demand, 3) if plan else None,
                "reason": plan.reason if plan else "dynamic_disabled",
                "cpu_percent": round(sample.cpu_percent, 1),
                "ram_percent": round(sample.ram_percent, 1),
                "load_per_core": round(sample.load_per_core, 2),
                "event_loop_lag_ms": round(sample.loop_lag_ms, 2),
                "foreground_waiting": self._foreground_waiting,
                "background_paused": self._foreground_waiting > 0,
                "streams": self.stream_view(),
                "lanes": lanes,
                "counters": dict(self._stats),
            }
        except Exception as ex:  # pragma: no cover
            self._degrade(f"snapshot_failed: {ex}")
            fixed = self._fixed_limits()
            return {
                "mode": "fixed",
                "degraded": True,
                "degraded_reason": self._degraded_reason,
                "lanes": {k: {"capacity": v, "fixed": v} for k, v in fixed.items()},
            }


class _Slot:
    """Async context manager wrapper around acquire/release."""

    __slots__ = ("_ctl", "_lane", "_priority", "_timeout", "_held")

    def __init__(
        self,
        controller: DynamicCapacityController,
        lane: str,
        priority: int | None,
        timeout: float | None,
    ) -> None:
        self._ctl = controller
        self._lane = lane
        self._priority = priority
        self._timeout = timeout
        self._held = False

    async def __aenter__(self) -> bool:
        self._held = await self._ctl.acquire(
            self._lane, priority=self._priority, timeout=self._timeout
        )
        return self._held

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._held:
            self._ctl.release(self._lane)
            self._held = False
        return False


class DynamicSemaphore:
    """Drop-in replacement for ``asyncio.Semaphore`` backed by the controller.

    Existing call sites keep ``async with resource_manager.download_semaphore():``
    unchanged; capacity underneath is now live. When the controller is disabled
    or has degraded, this transparently gates on a plain semaphore sized at the
    fixed env limit, so the fallback path is a real limit and not "unlimited".
    """

    def __init__(self, lane: str, controller: DynamicCapacityController | None = None) -> None:
        self._lane = lane
        self._ctl = controller if controller is not None else dynamic_capacity
        self._fallback: asyncio.Semaphore | None = None
        self._fallback_size = 0
        # `async with` reuses this object across tasks, so per-acquisition mode
        # is tracked per task (LIFO, so nesting is safe).
        self._modes: dict[Any, list[str]] = {}

    @property
    def lane(self) -> str:
        return self._lane

    def _fallback_sem(self) -> asyncio.Semaphore:
        size = max(1, int(self._ctl._fixed_limits().get(self._lane, 1)))
        if self._fallback is None or size != self._fallback_size:
            self._fallback = asyncio.Semaphore(size)
            self._fallback_size = size
        return self._fallback

    def _push(self, mode: str) -> None:
        task = asyncio.current_task()
        self._modes.setdefault(task, []).append(mode)

    def _pop(self) -> str:
        task = asyncio.current_task()
        stack = self._modes.get(task)
        if not stack:
            return "dynamic"
        mode = stack.pop()
        if not stack:
            self._modes.pop(task, None)
        return mode

    async def acquire(self, *, priority: int | None = None, timeout: float | None = None) -> bool:
        if self._ctl.enabled():
            await self._ctl.acquire(self._lane, priority=priority, timeout=timeout)
            self._push("dynamic")
            return True
        sem = self._fallback_sem()
        if timeout is not None:
            await asyncio.wait_for(sem.acquire(), timeout=timeout)
        else:
            await sem.acquire()
        self._push("fixed")
        return True

    def release(self) -> None:
        if self._pop() == "fixed":
            if self._fallback is not None:
                self._fallback.release()
            return
        self._ctl.release(self._lane)

    async def __aenter__(self) -> DynamicSemaphore:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def locked(self) -> bool:
        if self._ctl.enabled():
            return self._ctl.active(self._lane) >= self._ctl.capacity(self._lane)
        return self._fallback_sem().locked()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<DynamicSemaphore lane={self._lane} "
            f"capacity={self._ctl.capacity(self._lane)} active={self._ctl.active(self._lane)}>"
        )


dynamic_capacity = DynamicCapacityController()

__all__ = [
    "CapacityPlan",
    "DynamicCapacityController",
    "DynamicSemaphore",
    "LANES",
    "LaneState",
    "Priority",
    "Sample",
    "background_scope",
    "current_priority",
    "dynamic_capacity",
    "foreground_scope",
    "priority_scope",
]
