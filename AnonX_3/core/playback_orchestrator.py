# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Playback orchestration helpers: race decision matrix + startup success gate.

Does not replace calls.play_media; provides shared policy so direct vs local
failover stays consistent and testable.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from AnonX_3 import config, logger
from AnonX_3.core.cache.hub import CacheEntry, cache_hub
from AnonX_3.core.cache.keys import detect_source, make_cache_key
from AnonX_3.core.cache.states import CacheState
from AnonX_3.core.downloader.singleflight import download_flight, resolve_flight
from AnonX_3.core.downloader.validation import is_valid_media_file
from AnonX_3.core.metrics import (
    mark_cache_hit,
    mark_cache_miss,
    mark_direct_fail,
    mark_direct_ok,
    mark_local_failover,
    mark_ffmpeg_ok,
)


class PlayPath(str, Enum):
    CACHE_HIT = "cache_hit"
    DIRECT = "direct"
    LOCAL = "local"
    CDN = "cdn"
    FALLBACK_SOURCE = "fallback_source"
    FAILED = "failed"


class RaceDecision(str, Enum):
    USE_DIRECT = "use_direct"
    SWITCH_LOCAL = "switch_local"
    WAIT_LOCAL = "wait_local"
    FALLBACK = "fallback"
    FAIL = "fail"


@dataclass
class RaceOutcome:
    decision: RaceDecision
    play_path: str | None = None
    reason: str = ""


@dataclass
class StartupGateResult:
    ok: bool
    reason: str = ""
    elapsed_sec: float = 0.0
    fatal: bool = False


@dataclass
class PlaySession:
    """Tracks one media start attempt for metrics / gate."""

    chat_id: int
    cache_key: str
    media_id: str
    video: bool = False
    started_at: float = field(default_factory=time.time)
    direct_attempted: bool = False
    direct_ok: bool = False
    proof_complete: bool = False
    local_ready: bool = False
    path: PlayPath = PlayPath.FAILED
    notes: list[str] = field(default_factory=list)
    fatal_reason: str = ""
    _fatal_event: asyncio.Event | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._fatal_event is None:
            self._fatal_event = asyncio.Event()

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def signal_fatal(self, reason: str) -> None:
        self.fatal_reason = reason or "fatal"
        self.note(f"fatal:{self.fatal_reason}")
        if self._fatal_event is not None:
            self._fatal_event.set()

    @property
    def fatal_event(self) -> asyncio.Event:
        if self._fatal_event is None:
            self._fatal_event = asyncio.Event()
        return self._fatal_event


def build_key(media: Any, quality_tier: str | None = None) -> str:
    return cache_hub.key_for(media, quality_tier=quality_tier)


def _probe_mode() -> str:
    """soft (default) | strict | off — see DIRECT_URL_PROBE in config."""
    try:
        raw = (getattr(config, "DIRECT_URL_PROBE", None) or "soft").strip().lower()
    except Exception:
        raw = "soft"
    if raw in {"off", "0", "false", "no", "skip"}:
        return "off"
    if raw in {"strict", "hard", "require"}:
        return "strict"
    return "soft"


def validate_direct_url(url: str | None) -> tuple[bool, str]:
    """Perform the non-network validation required before direct playback.

    Network/ffmpeg probes are diagnostics and must not delay initial audio, but
    URL shape and SSRF protection remain a hard foreground boundary.
    """
    if not url or not str(url).startswith(("http://", "https://")):
        return False, "invalid_url"
    try:
        from AnonX_3.core.security import validate_http_url

        ok, reason = validate_http_url(url, allow_private=False)
    except Exception as ex:
        return False, f"ssrf_validation_error:{type(ex).__name__}"
    if not ok:
        return False, f"ssrf:{reason}"
    return True, "validated"


async def probe_direct_url(
    url: str | None,
    *,
    timeout: float = 1.5,
) -> tuple[bool, str]:
    """
    Light URL check before direct stream.

    Default mode=soft: googlevideo/CDN often return 403 to bare aiohttp probes
    (no cookies/player headers) even when ntgcalls/ffmpeg can still play.
    Soft mode does NOT kill direct; deferred local fallback is acquired only
    after direct URL/start/proof failure.

    strict: hard-fail on 4xx (old behavior). off: always pass (SSRF check only).
    """
    valid, validation_reason = validate_direct_url(url)
    if not valid:
        return False, validation_reason

    mode = _probe_mode()
    if mode == "off":
        return True, "probe_off"

    try:
        import aiohttp
    except Exception:
        return True, "aiohttp_missing_skip_probe"

    soft_block = {401, 403, 405, 429, 501}
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=max(0.8, float(timeout)))
        headers = {
            "Range": "bytes=0-1023",
            # yt-dlp-ish UA reduces some CDN 403s vs generic bot UA
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            try:
                async with session.head(
                    url, headers=headers, allow_redirects=True
                ) as resp:
                    if resp.status in {200, 206, 301, 302, 303, 307, 308}:
                        return True, f"head:{resp.status}"
                    if resp.status in soft_block:
                        pass  # fall through to GET / soft pass
                    elif resp.status >= 400 and mode == "strict":
                        return False, f"head:{resp.status}"
            except Exception:
                pass
            try:
                async with session.get(
                    url, headers=headers, allow_redirects=True
                ) as resp:
                    if resp.status in {200, 206}:
                        chunk = await resp.content.read(256)
                        if chunk:
                            return True, f"range:{resp.status}"
                        return True, f"range_empty:{resp.status}"
                    # Soft: 403/401/429 on CDN probe → still try direct play
                    if resp.status in soft_block or mode == "soft":
                        return True, f"soft_get:{resp.status}"
                    return False, f"get:{resp.status}"
            except Exception as ex:
                if mode == "soft":
                    # Network flake on probe — still try VC direct.
                    return True, f"soft_probe_error:{type(ex).__name__}"
                return False, f"probe_error:{type(ex).__name__}"
    except Exception as ex:
        if mode == "soft":
            return True, f"soft_probe_error:{type(ex).__name__}"
        return False, f"probe_error:{type(ex).__name__}"


async def await_parallel_ready(join_coro, source_coro):
    """Start VC preparation and source resolution together, then return both.

    Join readiness is authoritative: if it fails, the resolver is cancelled so
    an unmuted/usable VC is never replaced by unnecessary extraction or cache
    work. A source failure is propagated only after the concurrently-started
    join has settled, allowing the caller to reuse that empty binding for its
    local fallback.
    """
    join_task = asyncio.create_task(join_coro, name="initial-vc-ready")
    source_task = asyncio.create_task(source_coro, name="initial-direct-ready")
    try:
        try:
            join_result = await join_task
        except BaseException:
            if not source_task.done():
                source_task.cancel()
            await asyncio.gather(source_task, return_exceptions=True)
            raise
        source_result = await source_task
        return join_result, source_result
    finally:
        pending = [task for task in (join_task, source_task) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def prepare_cache_hit(
    media: Any,
    *,
    quality_tier: str | None = None,
) -> CacheEntry | None:
    """Step 1 of prompt flow: local/CDN READY verify-on-HIT."""
    entry = cache_hit_path(media, quality_tier=quality_tier)
    if entry and entry.local_path:
        try:
            cache_hub.mark_status(entry.key, CacheState.READY)
        except Exception:
            pass
        logger.info(
            "orchestrator CACHE HIT key=%s path=%s",
            entry.key,
            entry.local_path,
        )
        return entry
    return None


def decide_race(
    *,
    direct_ok: bool | None,
    local_path: str | None,
    local_pending: bool,
    video: bool = False,
) -> RaceOutcome:
    """Apply direct-vs-local decision matrix (prompt § race)."""
    local_valid = is_valid_media_file(local_path, video=video)

    if direct_ok is True:
        return RaceOutcome(
            decision=RaceDecision.USE_DIRECT,
            play_path=local_path if local_valid else None,
            reason="direct_healthy",
        )

    if direct_ok is False and local_valid:
        try:
            mark_local_failover()
        except Exception:
            pass
        return RaceOutcome(
            decision=RaceDecision.SWITCH_LOCAL,
            play_path=local_path,
            reason="direct_failed_local_ready",
        )

    if direct_ok is False and local_pending and not local_valid:
        return RaceOutcome(
            decision=RaceDecision.WAIT_LOCAL,
            reason="direct_failed_await_local",
        )

    if direct_ok is None and local_valid:
        return RaceOutcome(
            decision=RaceDecision.SWITCH_LOCAL,
            play_path=local_path,
            reason="no_direct_local_ready",
        )

    if direct_ok is None and local_pending:
        return RaceOutcome(
            decision=RaceDecision.WAIT_LOCAL,
            reason="await_local",
        )

    if direct_ok is False and not local_valid and not local_pending:
        return RaceOutcome(
            decision=RaceDecision.FALLBACK,
            reason="both_failed",
        )

    return RaceOutcome(decision=RaceDecision.FAIL, reason="unresolved")


class PlaybackStartupGate:
    """Mark direct playback successful when the VC play operation completes."""

    def __init__(self) -> None:
        self._active: dict[int, PlaySession] = {}

    def begin(
        self,
        chat_id: int,
        media: Any,
        *,
        quality_tier: str | None = None,
    ) -> PlaySession:
        # Release any previous gate for this chat (skip/next track).
        prev = self._active.pop(chat_id, None)
        if prev:
            cache_hub.release(prev.cache_key)
        key = build_key(media, quality_tier=quality_tier)
        session = PlaySession(
            chat_id=chat_id,
            cache_key=key,
            media_id=str(getattr(media, "id", "") or ""),
            video=bool(getattr(media, "video", False)),
        )
        self._active[chat_id] = session
        cache_hub.acquire(key)
        return session

    def end(self, chat_id: int) -> None:
        session = self._active.pop(chat_id, None)
        if session:
            cache_hub.release(session.cache_key)

    def get(self, chat_id: int) -> PlaySession | None:
        return self._active.get(chat_id)

    def signal_fatal(self, chat_id: int, reason: str) -> None:
        session = self._active.get(chat_id)
        if session:
            session.signal_fatal(reason)

    def mark_direct_dispatched(self, chat_id: int) -> PlaySession | None:
        """Open the proof window without waiting for ``client.play()``.

        The cold direct path submits PyTgCalls in an owned task.  This method
        makes StreamEnded/fatal events observable from the instant of dispatch
        while the task's completion is monitored independently.
        """
        session = self._active.get(chat_id)
        if session:
            session.direct_attempted = True
            session.direct_ok = False
            session.proof_complete = False
            session.fatal_reason = ""
            session.fatal_event.clear()
            session.note("direct_play_dispatched")
        return session

    def mark_direct_attached(self, chat_id: int) -> None:
        session = self._active.get(chat_id)
        if session:
            session.note("direct_stream_attached")

    def mark_direct_start_failed(self, chat_id: int, reason: str) -> None:
        session = self._active.get(chat_id)
        if session:
            session.direct_ok = False
            session.proof_complete = True
            session.signal_fatal(reason or "direct_start_failed")

    def in_gate_window(self, chat_id: int) -> bool:
        """True only while the direct ``client.play`` event is unresolved.

        Local/CDN paths call begin() only for cache refcount. Those sessions
        must NOT block StreamEnded → play_next (AnonX end-of-song leave).
        """
        session = self._active.get(chat_id)
        if not session or session.proof_complete:
            return False
        # Refcount-only session (no direct play attempt) is never a gate window.
        if not session.direct_attempted:
            return False
        return True

    async def accept_direct_start(
        self,
        chat_id: int,
        *,
        play_coro,
        on_started=None,
    ) -> StartupGateResult:
        """Submit direct media and return as soon as PyTgCalls accepts it."""
        session = self.mark_direct_dispatched(chat_id)
        t0 = time.time()
        try:
            await play_coro
        except Exception as ex:
            elapsed = time.time() - t0
            if session:
                session.proof_complete = True
                session.note(f"direct_start_error:{ex}")
            try:
                mark_direct_fail()
            except Exception:
                pass
            return StartupGateResult(
                ok=False,
                reason=f"play_exception:{type(ex).__name__}",
                elapsed_sec=elapsed,
                fatal=True,
            )

        if on_started is not None:
            try:
                on_started()
            except Exception:
                pass
        elapsed = time.time() - t0
        if session:
            session.note("direct_stream_accepted")
        return StartupGateResult(
            ok=True,
            reason="accepted",
            elapsed_sec=elapsed,
        )

    async def monitor_direct_proof(
        self,
        chat_id: int,
        *,
        proof_sec: float | None = None,
    ) -> StartupGateResult:
        """Observe the early-fatal window without blocking the playback path."""
        session = self._active.get(chat_id)
        if session is None or not session.direct_attempted:
            return StartupGateResult(
                ok=False,
                reason="missing_direct_session",
                fatal=False,
            )
        if proof_sec is None:
            try:
                proof_sec = float(
                    getattr(config, "DIRECT_START_PROOF_SEC", 3.0) or 0
                )
            except Exception:
                proof_sec = 3.0
        proof_sec = max(0.0, float(proof_sec or 0.0))
        if proof_sec > 0:
            try:
                await asyncio.wait_for(
                    session.fatal_event.wait(),
                    timeout=proof_sec,
                )
            except asyncio.TimeoutError:
                pass

        if self._active.get(chat_id) is not session:
            return StartupGateResult(
                ok=False,
                reason="stale_direct_session",
                fatal=False,
            )

        elapsed = max(0.0, time.time() - session.started_at)
        session.proof_complete = True
        if session.fatal_event.is_set():
            session.direct_ok = False
            session.note(f"direct_proof_failed:{session.fatal_reason}")
            try:
                mark_direct_fail()
            except Exception:
                pass
            logger.info(
                "playback startup proof failed chat_id=%s elapsed=%.2fs reason=%s key=%s",
                chat_id,
                elapsed,
                session.fatal_reason or "fatal",
                session.cache_key,
            )
            return StartupGateResult(
                ok=False,
                reason=session.fatal_reason or "direct_proof_failed",
                elapsed_sec=elapsed,
                fatal=True,
            )

        session.direct_ok = True
        session.path = PlayPath.DIRECT
        session.note("direct_proof_ok")
        try:
            mark_direct_ok(elapsed)
            mark_ffmpeg_ok()
        except Exception:
            pass
        logger.info(
            "playback startup proof ok chat_id=%s elapsed=%.2fs key=%s",
            chat_id,
            elapsed,
            session.cache_key,
        )
        return StartupGateResult(
            ok=True,
            reason="proved",
            elapsed_sec=elapsed,
        )

    async def confirm_direct_start(
        self,
        chat_id: int,
        *,
        play_coro,
        proof_sec: float | None = None,
        on_started=None,
    ) -> StartupGateResult:
        """
        Event-driven startup gate: direct succeeds only when ``client.play()``
        returns and no early StreamEnded/fatal signal arrives during a short
        proof window.

        ``on_started`` (optional) is invoked exactly once, immediately after
        ``play_coro`` returns — i.e. the moment PyTgCalls has accepted the
        stream and audio is audible — BEFORE the proof window. It exists purely
        for latency instrumentation and must never raise into the gate.
        """
        accepted = await self.accept_direct_start(
            chat_id,
            play_coro=play_coro,
            on_started=on_started,
        )
        if not accepted.ok:
            return accepted
        return await self.monitor_direct_proof(
            chat_id,
            proof_sec=proof_sec,
        )


# Module singletons
startup_gate = PlaybackStartupGate()


async def resolve_singleflight(
    key: str,
    factory,
    *,
    timeout: float | None = None,
):
    """Shared resolve job (yt-dlp extract / direct URL)."""
    cache_hub.mark_status(key, CacheState.RESOLVING)
    try:
        result = await resolve_flight.do(key, factory, timeout=timeout)
        return result
    except Exception:
        cache_hub.mark_status(
            key,
            CacheState.FAILED_TEMPORARY,
            failure_reason="resolve_failed",
            increment_retry=True,
        )
        raise


async def download_singleflight(
    key: str,
    factory,
    *,
    timeout: float | None = None,
):
    """Shared download job into local/CDN READY."""
    cache_hub.mark_status(key, CacheState.DOWNLOADING)
    try:
        result = await download_flight.do(key, factory, timeout=timeout)
        return result
    except Exception:
        cache_hub.mark_status(
            key,
            CacheState.FAILED_TEMPORARY,
            failure_reason="download_failed",
            increment_retry=True,
        )
        raise


def cache_hit_path(media: Any, quality_tier: str | None = None) -> CacheEntry | None:
    """Return valid cache entry if present (local/CDN READY)."""
    entry = cache_hub.lookup_media(media, quality_tier=quality_tier)
    try:
        if entry:
            mark_cache_hit()
        else:
            mark_cache_miss()
    except Exception:
        pass
    return entry


__all__ = [
    "PlayPath",
    "RaceDecision",
    "RaceOutcome",
    "StartupGateResult",
    "PlaySession",
    "startup_gate",
    "decide_race",
    "build_key",
    "cache_hit_path",
    "prepare_cache_hit",
    "probe_direct_url",
    "resolve_singleflight",
    "download_singleflight",
    "make_cache_key",
    "detect_source",
]
