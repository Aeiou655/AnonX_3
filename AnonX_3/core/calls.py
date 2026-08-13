# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import copy
import inspect
import importlib.metadata as importlib_metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from ntgcalls import (ConnectionNotFound, FrameData, MediaSource, ShellError, SignalingError,
                      StreamDevice, TelegramServerError,
                      RTMPStreamingUnsupported, ConnectionError)
from pyrogram import errors, raw
from pyrogram.types import Message
from pytgcalls import PyTgCalls, exceptions, types
from pytgcalls.list_to_cmd import list_to_cmd
from pytgcalls.pytgcalls_session import PyTgCallsSession

from AnonX_3 import app, config, db, lang, logger, queue, tg, thumb, tiktok, facebook, userbot, yt
from AnonX_3.core.cdn import cdn
from AnonX_3.core import netbind
from AnonX_3.core.playback import update_now_playing
from AnonX_3.core.playback_orchestrator import (
    RaceDecision,
    decide_race,
    prepare_cache_hit,
    probe_direct_url,
    startup_gate,
    validate_direct_url,
)
from AnonX_3.core.stream_watch import direct_watchdog
from AnonX_3.core.metrics import metrics, mark_local_failover, mark_ffmpeg_fail, mark_ffmpeg_ok, mark_download_ok, mark_download_fail
from AnonX_3.core.resource_manager import resource_manager
from AnonX_3.core.prefetch import PrefetchManager
from AnonX_3.core.request_context import enrich_request
from AnonX_3.core.resource_budget import allow_current_cache
from AnonX_3.core.stream_profile import StreamProfileManager
from AnonX_3.helpers import Media, Track, utils

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class StreamCapacityError(RuntimeError):
    """No VC stream slot was available, so the call was never started.

    Raised by :meth:`TgCall._play_with_startup_slot` — the single point every
    playback path (direct, local, cache) funnels through. Distinct from a
    playback failure: nothing was started, so handlers must not treat it as a
    broken track and must not burn the queue.
    """

    def __init__(self, chat_id: int) -> None:
        super().__init__(f"stream_capacity_reached: chat_id={chat_id}")
        self.chat_id = int(chat_id)


class AssistantUnmuteError(RuntimeError):
    """The assistant joined but could not become audible in the group call."""


class InitialVoiceJoinError(RuntimeError):
    """The cold-start empty VC binding could not be prepared safely."""


class InitialPlaybackLease:
    """Exclusive ownership of one chat's provisional initial-play transaction."""

    def __init__(self, chat_id: int, lock: asyncio.Lock) -> None:
        self.chat_id = int(chat_id)
        self._lock = lock
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self._lock.locked():
            self._lock.release()


class TgCall(PyTgCalls):
    READY_TIMEOUT_SEC = 45.0

    AUTOPLAY_ARTIST_KEY_STOPWORDS = {
        "official",
        "topic",
        "music",
        "records",
        "record",
        "channel",
        "vevo",
        "audio",
        "video",
        "lyrics",
        "lyric",
        "hd",
    }
    AUTOPLAY_TITLE_KEY_STOPWORDS = {
        "official",
        "video",
        "audio",
        "lyrics",
        "lyric",
        "topic",
        "mv",
        "song",
        "songs",
        "music",
        "hd",
        "4k",
        "8k",
        "feat",
        "ft",
        "featuring",
        "version",
        "full",
        "album",
        "playlist",
        "mix",
        "best",
        "new",
        "record",
        "records",
        "channel",
        "edit",
        "ost",
        "soundtrack",
        "x",
        "shorts",
        "short",
        "သီချင်း",
        "သီခ္င်း",
    }

    def __init__(self):
        self.clients = []
        self._ready = asyncio.Event()
        self._startup_error = None
        self.prefetch_manager = PrefetchManager()
        self.prefetch = self.prefetch_manager.prefetch
        self.stream_profile = StreamProfileManager()
        self.autoplay_recent_window = max(5, int(config.AUTOPLAY_RECENT_WINDOW))
        self.autoplay_max_artist_streak = max(1, int(config.AUTOPLAY_MAX_ARTIST_STREAK))
        self.autoplay_recent_ids: dict[int, deque[str]] = defaultdict(
            lambda: deque(maxlen=self.autoplay_recent_window)
        )
        self.autoplay_recent = self.autoplay_recent_ids
        self.autoplay_recent_titles: dict[int, deque[str]] = defaultdict(
            lambda: deque(maxlen=self.autoplay_recent_window)
        )
        self.autoplay_recent_artists: dict[int, deque[str]] = defaultdict(
            lambda: deque(maxlen=self.autoplay_recent_window)
        )
        self.autoplay_artist_streak: dict[int, int] = defaultdict(int)
        self.autoplay_last_artist: dict[int, str] = {}
        self._flood_tried: dict[int, set[int]] = {}
        # UNLIMITED VPS: 10X Maximum concurrent VC startups for instant response
        self._startup_semaphore = asyncio.Semaphore(30)
        # play_next / StreamEnded re-entrancy guards (double end → double pop → stuck VC)
        self._play_next_locks: dict[int, asyncio.Lock] = {}
        # The command layer holds this across "is there a VC?" → queue admission
        # → first play.  It prevents two first-play requests from both treating
        # themselves as position zero.
        self._initial_playback_locks: dict[int, asyncio.Lock] = {}
        self._stop_locks: dict[int, asyncio.Lock] = {}
        # This records a confirmed (or already-complete) leave only.  It must
        # never suppress local teardown: a later failed request may have placed
        # fresh queue state in the same chat.
        self._stopped_chats: set[int] = set()
        self._stream_end_at: dict[int, float] = {}
        self._play_next_depth: dict[int, int] = {}
        # Residual StreamEnded from track A after track B already started
        self._stream_switch_until: dict[int, float] = {}
        self._active_media_id: dict[int, str] = {}
        self._ending_media_id: dict[int, str] = {}
        # The raw cold path (MediaSource.SHELL, no ffprobe) is the only path
        # whose failure mode is process-wide rather than per-track: if ntgcalls
        # cannot launch the shell command at all, every future play pays a
        # wasted VC join plus a full failover.  One such failure disables it for
        # this process so playback degrades to the proven MediaStream path.
        self._raw_direct_disabled_reason: str = ""
        # NTgCalls 2.1.0 parses MediaSource.SHELL through Boost.Process v2.
        # Keep the launcher contract cached after one boot-time validation so
        # the first /play never discovers an executable/path problem.
        self._raw_ffmpeg_launcher: tuple[tuple[str, ...], str, str] | None = None
        self._raw_ffmpeg_launcher_probe_ms: int = -1
        # Keep background self-unmute attempts alive without adding latency to
        # the first-play path.
        self._vc_unmute_tasks: set[asyncio.Task] = set()
        self._vc_metadata_tasks: set[asyncio.Task] = set()
        self._vc_call_ref_cache: dict[tuple[int, int], tuple[float, object]] = {}
        self._vc_native_payload_cache: dict[tuple[int, int], tuple[float, object]] = {}
        self._vc_binding_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._direct_cache_tasks: set[asyncio.Task] = set()
        self._direct_external_audio_tasks: set[asyncio.Task] = set()
        # One live EXTERNAL/JIT capture session per chat. Unified stop and
        # source-switch cleanup owns this registry so a 10ms pump tick cannot
        # outlive leave_call() and surface a noisy ConnectionNotFound.
        self._direct_external_audio_sessions: dict[int, dict] = {}
        self._thumbnail_tasks: set[asyncio.Task] = set()
        self._post_start_tasks: set[asyncio.Task] = set()
        self._post_start_by_chat: dict[int, set[asyncio.Task]] = {}
        self._startup_proof_tasks: dict[int, asyncio.Task] = {}
        self._owned_tasks: set[asyncio.Task] = set()
        self._shutdown_lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_complete = False

    def _track_owned_task(
        self,
        task: asyncio.Task,
        registry: set[asyncio.Task],
    ) -> asyncio.Task:
        """Register a task in both its feature bucket and the call owner."""
        registry.add(task)
        self._owned_tasks.add(task)

        def _cleanup(done: asyncio.Task) -> None:
            registry.discard(done)
            self._owned_tasks.discard(done)

        task.add_done_callback(_cleanup)
        return task

    @staticmethod
    async def _get_input_group_call(client, chat_id: int):
        peer = await client.resolve_peer(chat_id)
        if isinstance(peer, raw.types.InputPeerChannel):
            result = await client.invoke(
                raw.functions.channels.GetFullChannel(
                    channel=raw.types.InputChannel(
                        channel_id=peer.channel_id,
                        access_hash=peer.access_hash,
                    )
                )
            )
        elif isinstance(peer, raw.types.InputPeerChat):
            result = await client.invoke(
                raw.functions.messages.GetFullChat(chat_id=peer.chat_id)
            )
        else:
            raise ValueError(f"Unsupported voice-chat peer: {type(peer).__name__}")

        call = getattr(getattr(result, "full_chat", None), "call", None)
        if call is None:
            raise ValueError("No active group call")
        return call

    @staticmethod
    def _vc_metadata_key(call_client, chat_id: int) -> tuple[int, int]:
        assistant = getattr(call_client, "mtproto_client", None)
        assistant_id = int(getattr(assistant, "id", 0) or 0)
        return assistant_id, int(chat_id)

    def _cached_vc_call_ref(self, call_client, chat_id: int):
        key = self._vc_metadata_key(call_client, chat_id)
        item = self._vc_call_ref_cache.get(key)
        if item is None:
            return None
        expiry, call = item
        if expiry <= time.monotonic():
            self._vc_call_ref_cache.pop(key, None)
            return None
        return call

    @staticmethod
    def _seed_pytgcalls_input_call_cache(call_client, chat_id: int, call_ref) -> bool:
        """Seed PyTgCalls' own ClientCache with an already-fetched call ref.

        PyTgCalls 2.2.x wraps the Pyrogram bridge in ``MtProtoClient`` and keeps
        the authoritative ``InputGroupCall`` in ``_bind_client._cache``.  Our
        late-join metadata warmer fetches that same object directly with a
        cheaper GetFullChannel/GetFullChat request.  Without seeding the bridge,
        public/private play paths immediately repeat get_call(), including a
        GetGroupCall round-trip, and prepared-native handoff reports
        ``pytgcalls_input_call=0`` even though we already have a valid call ref.

        Return False on layout drift so compatibility degrades to the public
        PyTgCalls path rather than making startup correctness depend on a private
        attribute.
        """
        if call_ref is None:
            return False
        bridge = getattr(call_client, "_app", None)
        candidates = (
            getattr(getattr(bridge, "_bind_client", None), "_cache", None),
            getattr(bridge, "_cache", None),
        )
        for cache in candidates:
            set_cache = getattr(cache, "set_cache", None)
            if not callable(set_cache):
                continue
            try:
                set_cache(int(chat_id), call_ref)
                return True
            except Exception:
                continue
        return False

    def _schedule_vc_metadata_warm(self, call_client, chat_id: int) -> asyncio.Task | None:
        """Prime PyTgCalls/MTProto call metadata without joining the VC.

        PyTgCalls play() asks its ClientCache for get_input_call() immediately
        before _connect_call(). Warming that cache while YouTube resolves moves
        the FullChat lookup off the visible join path. We also retain the same
        short-lived InputGroupCall for the post-join self-unmute request. No
        JoinGroupCall request is sent here, so the assistant is not visible in VC.
        """
        if self._shutting_down or not bool(
            getattr(config, "DIRECT_VC_METADATA_PREWARM", True)
        ):
            return None
        key = self._vc_metadata_key(call_client, chat_id)
        now = time.monotonic()
        cached_call_ref = self._cached_vc_call_ref(call_client, int(chat_id))
        native_item = self._vc_native_payload_cache.get(key)
        native_ready = bool(native_item is not None and native_item[0] > now)
        if cached_call_ref is not None and native_ready:
            logger.info(
                "vc_join_metadata_warm_cache_hit chat_id=%s assistant=%s "
                "call_ref=1 native_payload=1 joined=0",
                chat_id, key[0],
            )
            return None
        for task in tuple(self._vc_metadata_tasks):
            if getattr(task, "_anonx_vc_meta_key", None) == key and not task.done():
                return task

        async def _runner() -> None:
            started = time.perf_counter()
            pytgcalls_hit = False
            call_ref = None
            cache_seeded = False
            try:
                assistant = getattr(call_client, "mtproto_client", None)
                # Fetch the minimal InputGroupCall once. PyTgCalls' normal
                # ClientCache miss path performs GetFull* + GetGroupCall; the
                # direct Pyrogram lookup below is sufficient to prove an active
                # group call and is also the exact object JoinGroupCall needs.
                if assistant is not None:
                    call_ref = await self._get_input_group_call(
                        assistant, int(chat_id)
                    )
                if call_ref is not None:
                    ttl = max(
                        5.0,
                        float(
                            getattr(config, "DIRECT_VC_METADATA_TTL_SEC", 20.0)
                            or 20.0
                        ),
                    )
                    self._vc_call_ref_cache[key] = (
                        time.monotonic() + ttl, call_ref
                    )
                    cache_seeded = self._seed_pytgcalls_input_call_cache(
                        call_client, int(chat_id), call_ref
                    )
                    # Verify only from the now-seeded cache. This should be a
                    # pure local lookup; if a future PyTgCalls layout changes,
                    # avoid turning metadata warming into another network trip.
                    if cache_seeded:
                        bridge = getattr(call_client, "_app", None)
                        get_input_call = getattr(bridge, "get_input_call", None)
                        if callable(get_input_call):
                            try:
                                cached = await get_input_call(int(chat_id))
                            except Exception:
                                cached = None
                            pytgcalls_hit = cached is not None

                native_ready = False
                if (
                    not bool(getattr(config, "DIRECT_STARTUP_V4", True))
                    and bool(getattr(config, "DIRECT_VC_NATIVE_PREWARM", True))
                ):
                    binding = getattr(call_client, "_binding", None)
                    create_call = getattr(binding, "create_call", None)
                    calls_fn = getattr(binding, "calls", None)
                    if callable(create_call) and callable(calls_fn):
                        existing_calls = await calls_fn()
                        if int(chat_id) not in existing_calls:
                            payload = await create_call(int(chat_id))
                            native_ttl = max(
                                3.0,
                                float(
                                    getattr(
                                        config,
                                        "DIRECT_VC_NATIVE_PREWARM_TTL_SEC",
                                        12.0,
                                    )
                                    or 12.0
                                ),
                            )
                            self._vc_native_payload_cache[key] = (
                                time.monotonic() + native_ttl, payload
                            )
                            native_ready = True

                            async def _expire_payload(
                                expected_payload=payload,
                                expected_key=key,
                                delay=native_ttl,
                                expected_client=call_client,
                            ) -> None:
                                await asyncio.sleep(delay)
                                item = self._vc_native_payload_cache.get(expected_key)
                                if item is None or item[1] is not expected_payload:
                                    return
                                self._vc_native_payload_cache.pop(expected_key, None)
                                binding2 = getattr(expected_client, "_binding", None)
                                stop = getattr(binding2, "stop", None)
                                if callable(stop):
                                    try:
                                        await stop(int(chat_id))
                                    except Exception:
                                        pass

                            expiry_task = asyncio.create_task(
                                _expire_payload(),
                                name=f"vc-native-expire:{chat_id}:{key[0]}",
                            )
                            self._track_owned_task(
                                expiry_task, self._vc_metadata_tasks
                            )
                logger.info(
                    "vc_join_metadata_warm_ready chat_id=%s assistant=%s elapsed_ms=%s "
                    "pytgcalls_input_call=%s cache_seeded=%s unmute_call_ref=%s "
                    "native_payload=%s joined=0",
                    chat_id,
                    key[0],
                    int((time.perf_counter() - started) * 1000),
                    int(bool(pytgcalls_hit)),
                    int(bool(cache_seeded)),
                    int(call_ref is not None),
                    int(native_ready),
                )
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.debug(
                    "vc_join_metadata_warm_failed chat_id=%s assistant=%s error=%s",
                    chat_id, key[0], type(ex).__name__,
                )

        try:
            task = asyncio.create_task(_runner(), name=f"vc-meta-warm:{chat_id}:{key[0]}")
        except RuntimeError:
            return None
        setattr(task, "_anonx_vc_meta_key", key)
        self._track_owned_task(task, self._vc_metadata_tasks)
        logger.info(
            "vc_join_metadata_warm_started chat_id=%s assistant=%s joined=0",
            chat_id, key[0],
        )
        return task

    def _pop_vc_native_payload(self, call_client, chat_id: int):
        key = self._vc_metadata_key(call_client, chat_id)
        item = self._vc_native_payload_cache.get(key)
        if item is None:
            return None
        expiry, payload = item
        if expiry <= time.monotonic():
            # Leave the entry for its owned expiry task, which also stops the
            # unjoined native binding. Popping it here would orphan that call.
            return None
        self._vc_native_payload_cache.pop(key, None)
        return payload

    async def _play_with_prepared_native_payload(
        self, call_client, *, chat_id: int, stream, payload
    ) -> bool:
        """Connect an already-created local NTgCalls payload with a raw stream.

        The metadata warmer has already fetched and cached InputGroupCall.  Use
        that proof directly instead of repeating PyTgCalls' get_call() miss path
        (GetFull* + GetGroupCall) on the critical path.  The MTProto
        JoinGroupCall is still sent only here, after source-ready.
        """
        try:
            from pytgcalls.methods.utilities.stream_params import StreamParams
        except Exception:
            logger.info(
                "vc_native_payload_handoff chat_id=%s prepared=0 reason=stream_params_import",
                chat_id,
            )
            return False

        connect_call = getattr(call_client, "_connect_call", None)
        join_presentation = getattr(call_client, "_join_presentation", None)
        update_sources = getattr(call_client, "_update_sources", None)
        user_peer_cache = getattr(call_client, "_cache_user_peer", None)
        local_peer = getattr(call_client, "_cache_local_peer", None)
        if not all((
            callable(connect_call), callable(join_presentation),
            callable(update_sources), user_peer_cache is not None,
            local_peer is not None,
        )):
            logger.info(
                "vc_native_payload_handoff chat_id=%s prepared=0 reason=private_api_layout",
                chat_id,
            )
            return False

        media_description = await StreamParams.get_stream_params(stream)
        call_ref = self._cached_vc_call_ref(call_client, int(chat_id))
        if call_ref is None:
            # Compatibility fallback only. In the expected sub-5 path this is a
            # local cache hit seeded by _schedule_vc_metadata_warm().
            app_bridge = getattr(call_client, "_app", None)
            get_input_call = getattr(app_bridge, "get_input_call", None)
            if callable(get_input_call):
                try:
                    call_ref = await get_input_call(int(chat_id))
                except Exception:
                    call_ref = None
        if call_ref is None:
            logger.info(
                "vc_native_payload_handoff chat_id=%s prepared=0 reason=input_call_missing",
                chat_id,
            )
            return False

        cache_seeded = self._seed_pytgcalls_input_call_cache(
            call_client, int(chat_id), call_ref
        )
        try:
            user_peer_cache.put(int(chat_id), local_peer)
        except Exception:
            logger.info(
                "vc_native_payload_handoff chat_id=%s prepared=0 reason=user_peer_cache",
                chat_id,
            )
            return False

        config_obj = types.GroupCallConfig(auto_start=False)
        await connect_call(
            int(chat_id), media_description, config_obj, payload
        )

        is_presentation = media_description.screen is not None
        if is_presentation:
            await join_presentation(int(chat_id), True)

        deferred_sources = bool(
            getattr(config, "DIRECT_VC_DEFER_SOURCE_REFRESH", True)
            and not is_presentation
            and media_description.camera is None
        )
        if deferred_sources:
            async def _refresh_sources() -> None:
                try:
                    await update_sources(int(chat_id))
                except Exception as ex:
                    logger.debug(
                        "Deferred VC source refresh skipped chat_id=%s error=%s",
                        chat_id, type(ex).__name__,
                    )

            task = asyncio.create_task(
                _refresh_sources(),
                name=f"vc-source-refresh:{chat_id}",
            )
            self._track_owned_task(task, self._vc_metadata_tasks)
        else:
            await join_presentation(int(chat_id), is_presentation)
            await update_sources(int(chat_id))

        logger.info(
            "vc_native_payload_handoff chat_id=%s prepared=1 cache_seeded=%s "
            "deferred_sources=%s",
            chat_id, int(bool(cache_seeded)), int(bool(deferred_sources)),
        )
        return True

    async def _overlap_required_unmute(
        self, call_client, chat_id: int
    ) -> bool:
        """Try required self-unmute while the post-join RTC connect finishes.

        This task is created immediately before the actual play/connect call;
        v3.4.9 command preconnect may do so before source readiness. Early
        attempts may race JoinGroupCall and are intentionally retried; no admin
        fallback is attempted here. The normal required-unmute path remains the
        correctness fallback after play().
        """
        if not bool(getattr(config, "DIRECT_VC_UNMUTE_OVERLAP", True)):
            return False
        assistant = getattr(call_client, "mtproto_client", None)
        call = self._cached_vc_call_ref(call_client, int(chat_id))
        if assistant is None or call is None:
            return False
        delay = max(
            0.1,
            float(
                getattr(config, "DIRECT_VC_UNMUTE_INITIAL_DELAY_MS", 300)
                or 300
            ) / 1000.0,
        )
        retry = max(
            0.05,
            float(getattr(config, "DIRECT_VC_UNMUTE_RETRY_MS", 100) or 100)
            / 1000.0,
        )
        attempts = max(
            1,
            min(8, int(getattr(config, "DIRECT_VC_UNMUTE_ATTEMPTS", 5) or 5)),
        )
        await asyncio.sleep(delay)
        started = time.perf_counter()
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                await assistant.invoke(
                    raw.functions.phone.EditGroupCallParticipant(
                        call=call,
                        participant=raw.types.InputPeerSelf(),
                        muted=False,
                    )
                )
                logger.info(
                    "vc_unmute_overlap_ready chat_id=%s attempt=%s elapsed_ms=%s",
                    chat_id, attempt, int((time.perf_counter() - started) * 1000),
                )
                return True
            except errors.FloodWait:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                last_error = type(ex).__name__
                if attempt < attempts:
                    await asyncio.sleep(retry)
        logger.debug(
            "vc_unmute_overlap_exhausted chat_id=%s attempts=%s last_error=%s",
            chat_id, attempts, last_error or "unknown",
        )
        return False

    async def has_active_group_call(self, chat_id: int) -> bool:
        """Return whether the bot can currently see a live voice/video chat.

        An absent group call is an expected user-facing condition, so it is
        returned as ``False``.  Access, transport, and Telegram API failures
        deliberately propagate: treating those as "no VC" would hide an
        operational problem and make a valid request look terminal.
        """
        try:
            await self._get_input_group_call(app, chat_id)
        except ValueError as ex:
            if str(ex) == "No active group call":
                return False
            raise
        return True

    @asynccontextmanager
    async def initial_playback_lock(self, chat_id: int):
        """Serialize first-play admission for one chat.

        This lock is intentionally independent from ``stop()``.  ``play_media``
        can call ``stop()`` while handling a late ``NoActiveGroupCall``; sharing
        the lock would deadlock that recovery path.
        """
        lock = self._initial_playback_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            yield

    async def acquire_initial_playback_lease(
        self, chat_id: int
    ) -> InitialPlaybackLease:
        """Acquire the same lock used by normal initial queue admission.

        The command layer can therefore begin a provisional VC connection
        without allowing a concurrent first request to overtake it. Ownership
        is transferred to ``stream_media`` and released after commit/rollback.
        """

        lock = self._initial_playback_locks.setdefault(chat_id, asyncio.Lock())
        await lock.acquire()
        return InitialPlaybackLease(chat_id, lock)

    async def begin_initial_direct_preconnect(
        self,
        *,
        chat_id: int,
        video: bool,
        trace=None,
        request_id: int = 0,
    ) -> dict:
        """Connect an authorized initial YouTube request before search finishes.

        Only a silent EXTERNAL capture source is installed. The eventual Track
        adopts this exact task/session/stream reservation, attaches its decoder,
        and never reconnects. Callers must hold ``InitialPlaybackLease``.
        """

        client = await db.get_assistant(chat_id)
        if not resource_manager.can_admit_stream(chat_id):
            raise StreamCapacityError(chat_id)
        slot = resource_manager.reserve_stream(chat_id)
        if not slot.admitted:
            raise StreamCapacityError(chat_id)
        profile = self.stream_profile.cached_or_default(chat_id)
        provisional_id = f"pending-{int(request_id or 0)}"
        provisional = Track(
            id=provisional_id,
            title="Pending YouTube search",
            video=bool(video),
            source="youtube_pending",
        )
        stream = session = task = None
        try:
            stream, session = await self._prepare_initial_direct_external_stream(
                profile,
                provisional,
                chat_id=chat_id,
                # /vplay leads with the resolved audio track on this same
                # capture, proves the first outgoing clock tick, then swaps in
                # raw A/V without reconnecting.
                placeholder_only=False,
            )
            session["trace"] = trace
            if trace:
                trace.set_meta(mode="new-direct-command-preconnect")
                trace.mark("vc_join_start")
            self._log_direct_startup_event(
                "pytgcalls_play_task_scheduled",
                chat_id=chat_id,
                media_id=provisional_id,
                evidence="authorized_command_preconnect",
                status="scheduled_before_search",
            )
            task = asyncio.create_task(
                self._play_with_startup_slot(
                    client,
                    chat_id=chat_id,
                    stream=stream,
                    unmute_mode="required",
                    reserved_slot=slot,
                    startup_media_id=provisional_id,
                    external_audio_session=session,
                ),
                name=f"direct-command-preconnect:{chat_id}:{request_id}",
            )
            self._track_owned_task(task, self._direct_external_audio_tasks)
            transport = {
                "chat_id": int(chat_id),
                "video": bool(video),
                "client": client,
                "profile": profile,
                "stream": stream,
                "session": session,
                "slot": slot,
                "task": task,
                "adopted": False,
                "closed": False,
                "request_id": int(request_id or 0),
            }
            logger.info(
                "direct_command_preconnect_started chat_id=%s request_id=%s "
                "video=%s source_ready=0 reconnect=0",
                chat_id,
                request_id,
                int(bool(video)),
            )
            return transport
        except BaseException:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if session is not None:
                await self._close_direct_external_audio(session)
            slot.release()
            raise

    async def cancel_initial_direct_preconnect(self, transport: dict | None) -> None:
        """Rollback a provisional connection that was not adopted by playback."""

        if not isinstance(transport, dict) or transport.get("closed"):
            return
        if transport.get("adopted"):
            return
        transport["closed"] = True
        task = transport.get("task")
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await self._close_direct_external_audio(transport.get("session"))
        client = transport.get("client")
        chat_id = int(transport.get("chat_id") or 0)
        if client is not None and chat_id:
            try:
                await client.leave_call(chat_id, close=False)
            except Exception:
                pass
        slot = transport.get("slot")
        if slot is not None:
            slot.release()
        logger.info(
            "direct_command_preconnect_rolled_back chat_id=%s request_id=%s",
            chat_id,
            transport.get("request_id", 0),
        )

    async def _ensure_assistant_unmuted(
        self,
        call_client,
        chat_id: int,
        *,
        propagate_floodwait: bool = False,
    ) -> bool:
        assistant = call_client.mtproto_client
        assistant_id = int(getattr(assistant, "id", 0) or 0)
        assistant_floodwait = None
        try:
            call = self._cached_vc_call_ref(call_client, chat_id)
            call_cache_hit = call is not None
            if call is None:
                call = await self._get_input_group_call(assistant, chat_id)
            await assistant.invoke(
                raw.functions.phone.EditGroupCallParticipant(
                    call=call,
                    participant=raw.types.InputPeerSelf(),
                    muted=False,
                )
            )
            logger.info(
                "Assistant VC self-unmute OK chat_id=%s assistant=%s call_ref_cache_hit=%s",
                chat_id,
                assistant_id,
                int(bool(call_cache_hit)),
            )
            return True
        except errors.FloodWait as ex:
            assistant_floodwait = ex
            logger.debug(
                "Assistant VC self-unmute FloodWait chat_id=%s assistant=%s wait=%s",
                chat_id,
                assistant_id,
                getattr(ex, "value", "?"),
            )
        except Exception as ex:
            logger.debug(
                "Assistant VC self-unmute unavailable chat_id=%s assistant=%s error=%s",
                chat_id,
                assistant_id,
                type(ex).__name__,
            )

        # An assistant cannot undo an admin force-mute itself. If the main bot
        # has Manage Voice Chats permission, let it unmute only that assistant.
        try:
            call = await self._get_input_group_call(app, chat_id)
            try:
                participant = await app.resolve_peer(assistant_id)
            except Exception:
                assistant_username = getattr(assistant, "username", None)
                if not assistant_username:
                    raise
                participant = await app.resolve_peer(assistant_username)
            await app.invoke(
                raw.functions.phone.EditGroupCallParticipant(
                    call=call,
                    participant=participant,
                    muted=False,
                )
            )
            logger.info(
                "Assistant VC admin-unmute OK chat_id=%s assistant=%s",
                chat_id,
                assistant_id,
            )
            return True
        except Exception as ex:
            logger.warning(
                "Assistant VC auto-unmute failed chat_id=%s assistant=%s error=%s",
                chat_id,
                assistant_id,
                type(ex).__name__,
            )
            if assistant_floodwait is not None and propagate_floodwait:
                raise assistant_floodwait
            return False

    def _schedule_assistant_unmute(self, call_client, chat_id: int) -> None:
        if self._shutting_down:
            return
        task = asyncio.create_task(
            self._ensure_assistant_unmuted(call_client, chat_id),
            name=f"vc-unmute:{chat_id}",
        )
        self._track_owned_task(task, self._vc_unmute_tasks)

    async def _complete_external_required_unmute(
        self,
        call_client,
        *,
        chat_id: int,
        media_id: str,
        session: dict,
        overlap_task: asyncio.Task | None,
        slot,
        play_started: float,
    ) -> None:
        """Confirm audibility without blocking real PCM/RTP submission."""
        started = time.perf_counter()
        overlap_unmuted = False
        try:
            if overlap_task is not None:
                try:
                    overlap_unmuted = bool(await overlap_task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    overlap_unmuted = False
            unmuted = overlap_unmuted or await self._ensure_assistant_unmuted(
                call_client, chat_id, propagate_floodwait=True
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if unmuted:
                stamp = self._event_timestamp()
                session["unmute_confirmed_ns"] = int(stamp["monotonic_ns"])
                session["unmute_confirmed"].set()
                self._log_direct_startup_event(
                    "vc_unmute_confirmed",
                    chat_id=chat_id,
                    media_id=media_id,
                    timestamp=stamp,
                    evidence="background_required_unmute",
                    status="confirmed",
                    detail=f"unmute_ms={elapsed_ms};overlap_wait_ms=0",
                )
                logger.info(
                    "vc_fast_attach timing chat_id=%s media_id=%s "
                    "unmute_ms=%s unmute_overlap=%s overlap_wait_ms=0 "
                    "unmute_blocked_audio_ms=0 total_background_ms=%s",
                    chat_id,
                    media_id,
                    elapsed_ms,
                    int(overlap_unmuted),
                    int((time.perf_counter() - play_started) * 1000),
                )
                return

            session["unmute_failed"].set()
            session["error"] = "assistant_unmute_failed"
            startup_gate.signal_fatal(chat_id, "assistant_unmute_failed")
            session["vplay_handoff_pending"] = False
            await self._close_direct_external_audio(session)
            try:
                await call_client.leave_call(chat_id, close=False)
            except Exception:
                pass
            slot.release()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            session["unmute_failed"].set()
            session["error"] = f"assistant_unmute_failed:{type(ex).__name__}"
            startup_gate.signal_fatal(chat_id, session["error"])
            session["vplay_handoff_pending"] = False
            await self._close_direct_external_audio(session)
            try:
                await call_client.leave_call(chat_id, close=False)
            except Exception:
                pass
            slot.release()
            logger.warning(
                "vc_background_required_unmute_failed chat_id=%s media_id=%s "
                "error=%s",
                chat_id,
                media_id,
                type(ex).__name__,
            )

    async def _play_with_startup_slot(
        self,
        client,
        *,
        chat_id: int,
        stream,
        unmute_mode: str = "background",
        reserved_slot=None,
        startup_media_id: str | None = None,
        external_audio_session: dict | None = None,
    ) -> None:
        # This wrapper only submits the stream to PyTgCalls; direct YouTube
        # success is proved separately by PlaybackStartupGate.
        #
        # It is also the *single* stream-admission point: direct, local and
        # cache playback all reach PyTgCalls through here, so all three obey
        # one capacity decision. The slot is reserved before the call starts
        # and handed straight back if the start fails, so a failed attempt
        # never leaks capacity. Reservation is idempotent per chat, so a
        # queue auto-next re-entering for a chat that already holds a slot
        # neither double-counts nor releases a live one.
        slot = reserved_slot or resource_manager.reserve_stream(chat_id)
        if not slot.admitted:
            raise StreamCapacityError(chat_id)
        prepared_payload = None
        prepared_native = False
        is_raw_stream = isinstance(stream, types.raw.Stream)
        if startup_media_id is not None and is_raw_stream:
            prepared_payload = self._pop_vc_native_payload(client, chat_id)
        play_started = time.perf_counter()
        unmute_overlap_task = None
        if (
            unmute_mode == "required"
            and is_raw_stream
            and bool(getattr(config, "DIRECT_VC_UNMUTE_OVERLAP", True))
        ):
            unmute_overlap_task = asyncio.create_task(
                self._overlap_required_unmute(client, int(chat_id)),
                name=f"vc-unmute-overlap:{chat_id}",
            )
            self._track_owned_task(unmute_overlap_task, self._vc_unmute_tasks)
        play_ms = -1
        if external_audio_session is not None:
            external_audio_session["trace"] = external_audio_session.get("trace")
            if bool(getattr(config, "DIRECT_EXTERNAL_JIT_FEED", True)):
                jit_task = asyncio.create_task(
                    self._jit_prime_external_capture(client, external_audio_session),
                    name=f"direct-external-jit:{chat_id}:{startup_media_id or ''}",
                )
                external_audio_session["jit_task"] = jit_task
                self._track_owned_task(jit_task, self._direct_external_audio_tasks)
        try:
            async with self._startup_semaphore:
                if startup_media_id is not None:
                    self._log_direct_startup_event(
                        "pytgcalls_play_before",
                        chat_id=chat_id,
                        media_id=startup_media_id,
                        status="calling",
                        evidence=(
                            "external_prebuffer_native" if external_audio_session is not None
                            else "prewarmed_native_raw" if prepared_payload is not None
                            else "raw_no_media_probe" if is_raw_stream
                            else "mediastream_check_stream"
                        ),
                    )
                binding_lock = self._vc_binding_locks.setdefault(
                    self._vc_metadata_key(client, chat_id), asyncio.Lock()
                )

                async def _submit_stream() -> None:
                    nonlocal prepared_native
                    if prepared_payload is not None:
                        prepared_native = await self._play_with_prepared_native_payload(
                            client, chat_id=chat_id, stream=stream, payload=prepared_payload
                        )
                        if not prepared_native:
                            binding = getattr(client, "_binding", None)
                            stop = getattr(binding, "stop", None)
                            if callable(stop):
                                try:
                                    await stop(int(chat_id))
                                except Exception:
                                    pass
                    if not prepared_native:
                        await client.play(
                            chat_id=chat_id,
                            stream=stream,
                            config=types.GroupCallConfig(auto_start=False),
                        )

                if bool(getattr(config, "DIRECT_STARTUP_V4", True)):
                    async with binding_lock:
                        await _submit_stream()
                else:
                    await _submit_stream()
                play_ms = int((time.perf_counter() - play_started) * 1000)
                if external_audio_session is not None:
                    connected_stamp = self._event_timestamp()
                    external_audio_session["connected_ns"] = int(connected_stamp["monotonic_ns"])
                    external_audio_session["connected"].set()
                    self._log_direct_startup_event(
                        "vc_connected_external_capture",
                        chat_id=chat_id,
                        media_id=startup_media_id or str(external_audio_session.get("media_id") or ""),
                        timestamp=connected_stamp,
                        evidence="pytgcalls_play_connect_returned",
                        status="connected",
                        detail=f"jit_capture={int(bool(external_audio_session.get('jit_kick_accepted')))}",
                    )
                    await self._activate_direct_external_audio(
                        client, external_audio_session
                    )
                if startup_media_id is not None:
                    self._log_direct_startup_event(
                        "pytgcalls_play_after",
                        chat_id=chat_id,
                        media_id=startup_media_id,
                        status="returned",
                        evidence=(
                            "external_prebuffer_native" if external_audio_session is not None
                            else "prewarmed_native_raw" if prepared_native
                            else "raw_no_media_probe" if is_raw_stream
                            else "mediastream_check_stream"
                        ),
                        detail=(
                            f"play_ms={play_ms} external_prebuffer="
                            f"{int(external_audio_session is not None)}"
                        ),
                    )
        except BaseException:
            if external_audio_session is not None:
                await self._close_direct_external_audio(external_audio_session)
            if unmute_overlap_task is not None and not unmute_overlap_task.done():
                unmute_overlap_task.cancel()
            if startup_media_id is not None:
                self._log_direct_startup_event(
                    "pytgcalls_play_after",
                    chat_id=chat_id,
                    media_id=startup_media_id,
                    status="raised",
                )
            slot.release()
            raise
        if (
            unmute_mode == "required"
            and external_audio_session is not None
            and bool(getattr(config, "DIRECT_STARTUP_V4", True))
        ):
            task = asyncio.create_task(
                self._complete_external_required_unmute(
                    client,
                    chat_id=int(chat_id),
                    media_id=str(startup_media_id or external_audio_session.get("media_id") or ""),
                    session=external_audio_session,
                    overlap_task=unmute_overlap_task,
                    slot=slot,
                    play_started=play_started,
                ),
                name=f"vc-required-unmute:{chat_id}:{startup_media_id or ''}",
            )
            external_audio_session["unmute_task"] = task
            self._track_owned_task(task, self._vc_unmute_tasks)
            logger.info(
                "vc_required_unmute_background chat_id=%s media_id=%s "
                "overlap_wait_ms=0 unmute_blocked_audio_ms=0",
                chat_id,
                startup_media_id or external_audio_session.get("media_id"),
            )
        elif unmute_mode == "required":
            unmute_started = time.perf_counter()
            overlap_unmuted = False
            overlap_ms = -1
            if unmute_overlap_task is not None:
                overlap_wait_started = time.perf_counter()
                try:
                    overlap_unmuted = bool(await unmute_overlap_task)
                except errors.FloodWait:
                    slot.release()
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    overlap_unmuted = False
                overlap_ms = int(
                    (time.perf_counter() - overlap_wait_started) * 1000
                )
            if overlap_unmuted:
                unmuted = True
            else:
                unmuted = await self._ensure_assistant_unmuted(
                    client, chat_id, propagate_floodwait=True
                )
            unmute_ms = int((time.perf_counter() - unmute_started) * 1000)
            if startup_media_id is not None:
                logger.info(
                    "vc_fast_attach timing chat_id=%s media_id=%s raw=%s "
                    "prepared_native=%s play_ms=%s unmute_ms=%s "
                    "unmute_overlap=%s overlap_wait_ms=%s external_prebuffer=%s "
                    "total_attach_ms=%s",
                    chat_id,
                    startup_media_id,
                    int(is_raw_stream),
                    int(prepared_native),
                    play_ms,
                    unmute_ms,
                    int(bool(overlap_unmuted)),
                    overlap_ms,
                    int(external_audio_session is not None),
                    int((time.perf_counter() - play_started) * 1000),
                )
            if not unmuted:
                try:
                    await client.leave_call(chat_id, close=False)
                except Exception as ex:
                    logger.debug(
                        "Empty VC cleanup after unmute failure skipped "
                        "chat_id=%s error=%s",
                        chat_id,
                        type(ex).__name__,
                    )
                slot.release()
                raise AssistantUnmuteError(
                    f"assistant_unmute_failed: chat_id={chat_id}"
                )
        elif unmute_mode == "background":
            self._schedule_assistant_unmute(client, chat_id)
        elif unmute_mode != "skip":
            slot.release()
            raise ValueError(f"unsupported unmute mode: {unmute_mode}")

    async def _discard_empty_prejoin(self, client, chat_id: int) -> None:
        try:
            await client.leave_call(chat_id, close=False)
        except Exception:
            pass
        resource_manager.unregister_stream(chat_id)

    async def _prepare_initial_direct_call(
        self,
        client,
        *,
        chat_id: int,
        message: Message,
        language: dict,
        trace=None,
    ):
        """Join an empty VC and require audible assistant state before attach."""
        retries = 0
        while True:
            try:
                await self._play_with_startup_slot(
                    client,
                    chat_id=chat_id,
                    stream=None,
                    unmute_mode="required",
                )
                if trace:
                    trace.mark("vc_ready")
                logger.info(
                    "Initial VC prejoin ready chat_id=%s assistant=%s",
                    chat_id,
                    getattr(getattr(client, "mtproto_client", None), "id", "?"),
                )
                return client
            except AssistantUnmuteError:
                raise
            except StreamCapacityError:
                raise
            except errors.FloodWait as fw:
                await self._discard_empty_prejoin(client, chat_id)
                tried = self._flood_tried.setdefault(chat_id, set())
                tried.add(db.assistant.get(chat_id, 1))
                logger.warning(
                    "Initial VC prejoin FloodWait %ss assistant=%s chat_id=%s; rotating",
                    fw.value,
                    db.assistant.get(chat_id, "?"),
                    chat_id,
                )
                if len(tried) >= len(userbot.clients):
                    wait_seconds = max(int(getattr(fw, "value", 0) or 0), 1)
                    self._flood_tried.pop(chat_id, None)
                    await asyncio.sleep(wait_seconds)
                else:
                    await db.rotate_assistant(chat_id)
                client = await db.get_assistant(chat_id)
            except (
                TimeoutError,
                exceptions.NoActiveGroupCall,
                ConnectionError,
                ConnectionNotFound,
                TelegramServerError,
                SignalingError,
            ) as ex:
                await self._discard_empty_prejoin(client, chat_id)
                retries += 1
                if retries >= 5:
                    raise InitialVoiceJoinError(
                        f"initial_vc_prejoin_exhausted:{type(ex).__name__}"
                    ) from ex
                await self._notify_join_status(
                    message,
                    language,
                    language.get(
                        "play_join_retry",
                        "Joining voice chat… retry {0}/5",
                    ).format(retries),
                )
                await asyncio.sleep(min(0.5 * retries, 2.0))

    @staticmethod
    def _package_version(*names: str) -> str:
        for name in names:
            try:
                return importlib_metadata.version(name)
            except Exception:
                continue
        return "unknown"

    @staticmethod
    def _callable_signature(func) -> str:
        try:
            return str(inspect.signature(func))
        except Exception:
            return "unknown"

    @staticmethod
    def _direct_url_host(url: str | None) -> str:
        if not url:
            return ""
        try:
            return urlparse(str(url)).netloc.lower()
        except Exception:
            return ""

    @staticmethod
    def _direct_url_has_pot(url: str | None) -> int:
        # A signed googlevideo URL without a GVS PO token still extracts fine
        # but 403s on fetch (see ERRORS.md "class=client_po"). Log presence
        # only, never the token value.
        if not url:
            return 0
        try:
            query = urlparse(str(url)).query
        except Exception:
            return 0
        return int(
            any(part.split("=", 1)[0] == "pot" for part in query.split("&") if part)
        )

    @staticmethod
    def _ffmpeg_header_blob(headers: dict | None) -> str:
        if not isinstance(headers, dict) or not headers:
            return ""
        pairs = []
        for key, value in headers.items():
            clean_key = str(key).replace("\r", "").replace("\n", "").strip()
            clean_value = str(value).replace("\r", "").replace("\n", "").strip()
            if clean_key and clean_value:
                pairs.append(f"{clean_key}: {clean_value}")
        return "\r\n".join(pairs) + ("\r\n" if pairs else "")

    @staticmethod
    def _direct_ffmpeg_parameters(source) -> str:
        # Must be shlex-quoted: PyTgCalls parses this string with shlex.split
        # (pytgcalls/ffmpeg.py:_get_stream_params), so a bare space-join splits
        # the User-Agent into ~12 stray argv tokens and ffmpeg sees UA
        # "Mozilla/5.0" plus garbage flags.
        params = [
            "-nostdin",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-rw_timeout",
            "5000000",
            "-user_agent",
            _BROWSER_UA,
        ]
        # One merged -headers blob only. ffmpeg's -headers is a single AVOption
        # string, so repeating the flag keeps just the last value.
        headers = TgCall._ffmpeg_header_blob(getattr(source, "headers", None))
        if headers:
            params.extend(["-headers", headers])
        # No -cookies here: ffmpeg's -cookies takes Set-Cookie *content*, not a
        # path, and the jar is scoped to youtube.com while signed media lives on
        # googlevideo.com, which never receives those cookies anyway.
        proxy = str(getattr(source, "proxy", "") or "").strip()
        if proxy.startswith(("http://", "https://")) and not any(
            ch.isspace() for ch in proxy
        ):
            params.extend(["-http_proxy", proxy])
        elif not proxy:
            # Leave from the address yt-dlp minted the URL from. Google binds a
            # signed URL to the requesting IP, so a dual-stack host that
            # extracts over IPv6 and fetches over IPv4 gets 403. -local_addr
            # reaches the socket through the https -> tls -> tcp option chain.
            params.extend(netbind.ffmpeg_local_addr_args())
        return shlex.join(params)

    @staticmethod
    def _cold_direct_input_args(source) -> list[str]:
        """Return the no-probe, low-buffer FFmpeg input options.

        The established direct path still uses ``MediaStream`` and its normal
        validation.  Only the already-resolved cold-start URL uses this list,
        which deliberately omits FFmpeg reconnect sleeps: a failed first HTTP
        read is handed to the existing local fallback instead.
        """
        inherited = shlex.split(TgCall._direct_ffmpeg_parameters(source))
        retry_options = {
            "-reconnect",
            "-reconnect_at_eof",
            "-reconnect_streamed",
            "-reconnect_delay_max",
        }
        transport: list[str] = []
        skip_value = False
        for value in inherited:
            if skip_value:
                skip_value = False
                continue
            if value in retry_options:
                skip_value = True
                continue
            if value == "-nostdin":
                continue
            transport.append(value)
        return [
            "-hide_banner",
            "-loglevel",
            "debug",
            "-nostdin",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-analyzeduration",
            "0",
            "-probesize",
            "32768",
            *transport,
        ]

    @staticmethod
    def _direct_event_path(chat_id: int) -> str:
        filename = (
            f"anonx-direct-{os.getpid()}-{int(chat_id)}-{uuid.uuid4().hex}.jsonl"
        )
        return str(Path(tempfile.gettempdir()) / "anonx-playback" / filename)

    @staticmethod
    def _raw_audio_parameters(audio):
        """Expand a StreamProfile audio value into raw ``AudioParameters``.

        ``StreamProfile`` keeps the ``AudioQuality`` enum, whose value is a
        ``(bitrate, channels)`` tuple; ``MediaStream.__init__`` expands it for
        every other path.  The raw cold path bypasses ``MediaStream``, so it has
        to run the same conversion before reading ``.bitrate``/``.channels`` and
        before ntgcalls type-checks ``raw.AudioStream``.
        """
        if isinstance(audio, types.AudioQuality):
            return types.raw.AudioParameters(*audio.value)
        return audio

    @staticmethod
    def _raw_video_parameters(video):
        """Expand a StreamProfile video value into raw ``VideoParameters``.

        ``adjust_by_height=False`` mirrors ``MediaStream``, which never rescales
        an explicitly selected ``VideoQuality``.
        """
        if isinstance(video, types.VideoQuality):
            return types.raw.VideoParameters(
                *video.value,
                adjust_by_height=False,
            )
        return video

    @staticmethod
    def _same_executable(left: str | None, right: str | None) -> bool:
        if not left or not right:
            return False
        try:
            return os.path.samefile(left, right)
        except (OSError, ValueError):
            return os.path.realpath(left) == os.path.realpath(right)

    @staticmethod
    def _boost_shell_safe_prefix(
        executable_abs: str,
    ) -> tuple[tuple[str, ...], str]:
        """Return argv prefix safe for NTgCalls' Boost.Process ShellReader.

        NTgCalls 2.1.0 uses ``bp::shell(command).exe()``. Boost.Process then
        resolves argv[0] through PATH. Passing an absolute executable as argv[0]
        is therefore needlessly dependent on Boost.Filesystem path-join semantics.

        Prefer a PATH-resolved ``env`` launcher and pass the real executable as
        an *argument*: ``env -- /absolute/program ...``. GNU/coreutils ``env``
        performs the final exec using that exact absolute path, while Boost only
        has to resolve the simple, already-verified ``env`` token.  If ``env`` is
        unavailable, pin the executable directory to the front of PATH and use
        its basename, matching PyTgCalls' proven MediaStream launcher contract.
        """
        executable_abs = os.path.abspath(str(executable_abs))
        env_abs = shutil.which("env")
        if env_abs and os.path.isfile(env_abs) and os.access(env_abs, os.X_OK):
            env_token = os.path.basename(env_abs) or "env"
            resolved_env = shutil.which(env_token)
            if TgCall._same_executable(resolved_env, env_abs):
                return (env_token, "--", executable_abs), "env_absolute_exec"

        token = os.path.basename(executable_abs)
        directory = os.path.dirname(executable_abs)
        current = shutil.which(token)
        if not TgCall._same_executable(current, executable_abs):
            current_path = os.environ.get("PATH", "")
            pieces = [part for part in current_path.split(os.pathsep) if part]
            pieces = [
                part
                for part in pieces
                if os.path.realpath(part) != os.path.realpath(directory)
            ]
            os.environ["PATH"] = os.pathsep.join([directory, *pieces])
            current = shutil.which(token)
        if not TgCall._same_executable(current, executable_abs):
            raise FileNotFoundError(
                f"Boost-safe launcher could not resolve {token!r} to selected executable"
            )
        return (token,), "path_pinned_basename"

    def _prepare_raw_ffmpeg_launcher(self) -> tuple[tuple[str, ...], str, str]:
        cached = self._raw_ffmpeg_launcher
        if cached is not None:
            return cached

        requested = str(
            getattr(config, "FFMPEG_BINARY", "ffmpeg") or "ffmpeg"
        ).strip()
        if os.path.sep in requested:
            candidate = os.path.abspath(os.path.expanduser(requested))
            ffmpeg_abs = candidate if os.path.exists(candidate) else None
        else:
            ffmpeg_abs = shutil.which(requested)
        if not ffmpeg_abs:
            raise FileNotFoundError(f"FFmpeg executable not found: {requested}")
        ffmpeg_abs = os.path.abspath(ffmpeg_abs)
        if not os.path.isfile(ffmpeg_abs) or not os.access(ffmpeg_abs, os.X_OK):
            raise PermissionError(f"FFmpeg is not executable: {ffmpeg_abs}")

        timeout = max(
            1.0,
            float(
                getattr(
                    config,
                    "DIRECT_RAW_LAUNCH_PROBE_TIMEOUT_SEC",
                    3.0,
                )
                or 3.0
            ),
        )

        def probe(prefix: tuple[str, ...]) -> tuple[str, int]:
            launcher_abs = shutil.which(prefix[0]) if prefix else None
            if (
                not launcher_abs
                or not os.path.isfile(launcher_abs)
                or not os.access(launcher_abs, os.X_OK)
            ):
                raise FileNotFoundError(
                    "Boost launcher token is not executable: "
                    f"{prefix[0] if prefix else ''}"
                )
            probe_started = time.perf_counter()
            subprocess.run(
                [launcher_abs, *prefix[1:], "-version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=timeout,
            )
            return launcher_abs, int(
                (time.perf_counter() - probe_started) * 1000
            )

        prefix, mode = self._boost_shell_safe_prefix(ffmpeg_abs)
        try:
            launcher_abs, probe_ms = probe(prefix)
        except (OSError, subprocess.SubprocessError) as primary_ex:
            # Some minimal ``env`` implementations do not accept ``--``.
            # Pin the selected FFmpeg directory to PATH and let Boost resolve
            # the basename instead of abandoning the raw fast path entirely.
            if mode != "env_absolute_exec":
                raise
            directory = os.path.dirname(ffmpeg_abs)
            token = os.path.basename(ffmpeg_abs)
            current_path = os.environ.get("PATH", "")
            pieces = [part for part in current_path.split(os.pathsep) if part]
            pieces = [
                part
                for part in pieces
                if os.path.realpath(part) != os.path.realpath(directory)
            ]
            os.environ["PATH"] = os.pathsep.join([directory, *pieces])
            resolved = shutil.which(token)
            if not self._same_executable(resolved, ffmpeg_abs):
                raise primary_ex
            prefix = (token,)
            mode = "path_pinned_basename_after_env_probe_failure"
            launcher_abs, probe_ms = probe(prefix)

        self._raw_ffmpeg_launcher_probe_ms = probe_ms
        self._raw_ffmpeg_launcher = (prefix, ffmpeg_abs, mode)
        logger.info(
            "raw_audio_launcher_ready mode=%s launcher_token=%s ffmpeg_abs=%s "
            "launcher_abs=%s exec_probe_ms=%s boost_path_safe=1",
            mode,
            prefix[0] if prefix else "",
            ffmpeg_abs,
            launcher_abs,
            self._raw_ffmpeg_launcher_probe_ms,
        )
        return self._raw_ffmpeg_launcher

    @staticmethod
    def _raw_shell_command(argv: list[str]) -> str:
        """Serialize trusted argv for Boost wordexp without shell operators."""
        clean: list[str] = []
        for item in argv:
            value = str(item)
            if "\x00" in value:
                raise ValueError("NUL byte in raw media command")
            clean.append(value)
        # PyTgCalls list_to_cmd() is shlex.join() on POSIX. Keep the same
        # quoting contract explicitly so URLs containing &, ?, = and header
        # values containing spaces remain one argv item after Boost wordexp.
        return list_to_cmd(clean)

    def _build_initial_direct_raw_stream(
        self,
        source,
        profile,
        media,
        *,
        chat_id: int,
    ):
        """Build a raw ntgcalls stream without MediaStream.check_stream().

        PyTgCalls 2.2.11 otherwise performs a remote ffprobe and a separate
        ``ffmpeg -h full`` cleanup pass inside ``play()``.  NTgCalls ShellReader
        also resolves argv[0] through Boost.Process PATH lookup, so this path
        uses a Boost-safe launcher token that execs the prevalidated absolute
        FFmpeg binary. The resolver has
        already supplied the exact signed media URL, so the initial cold path
        can install the FFmpeg shell source directly and let the asynchronous
        startup/fallback observers judge the result.
        """
        url = str(getattr(source, "url", "") or "")
        video_url = str(getattr(source, "video_url", "") or "") or url
        event_path = self._direct_event_path(chat_id)
        launcher_prefix, ffmpeg_abs, launcher_mode = (
            self._prepare_raw_ffmpeg_launcher()
        )
        logger.debug(
            "raw_audio_stream_command_ready chat_id=%s launcher_mode=%s "
            "launcher_token=%s ffmpeg_abs=%s",
            chat_id,
            launcher_mode,
            launcher_prefix[0] if launcher_prefix else "",
            ffmpeg_abs,
        )
        observer = Path(__file__).with_name("ffmpeg_observer.py")
        input_args = self._cold_direct_input_args(source)
        audio = self._raw_audio_parameters(profile.audio_parameters)
        audio_frame_bytes = max(
            1,
            int(audio.bitrate) * int(audio.channels) * 2 // 50,
        )
        audio_command = [
            *launcher_prefix,
            *input_args,
            "-i",
            url,
            "-map",
            "0:a:0",
            "-vn",
            "-f",
            "s16le",
            "-ac",
            str(audio.channels),
            "-ar",
            str(audio.bitrate),
            "-flush_packets",
            "1",
            "pipe:1",
        ]
        # The observer is an optional telemetry wrapper.  If it cannot run, the
        # shell child dies before ffmpeg is ever exec'd and ntgcalls reports only
        # "ShellError" — so verify it up front and stream unobserved rather than
        # trading working playback for progress events.
        observed = (
            bool(getattr(config, "DIRECT_RAW_OBSERVER", False))
            and observer.is_file()
        )
        if observed:
            try:
                Path(event_path).parent.mkdir(parents=True, exist_ok=True)
            except OSError as ex:
                logger.warning(
                    "Direct playback event dir unavailable (%s); "
                    "streaming without the ffmpeg observer.",
                    ex,
                )
                observed = False
        elif bool(getattr(config, "DIRECT_RAW_OBSERVER", False)):
            logger.warning(
                "ffmpeg observer missing at %s; streaming without it.", observer
            )
        if observed:
            python_abs = os.path.abspath(sys.executable)
            python_prefix, _ = self._boost_shell_safe_prefix(python_abs)
            observed_audio_command = [
                *python_prefix,
                str(observer),
                "--event-file",
                event_path,
                "--chat-id",
                str(int(chat_id)),
                "--media-id",
                str(getattr(media, "id", "") or ""),
                "--kind",
                "audio",
                "--frame-bytes",
                str(audio_frame_bytes),
                "--",
                *audio_command,
            ]
        else:
            observed_audio_command = audio_command
            event_path = ""
        microphone = types.raw.AudioStream(
            MediaSource.SHELL,
            self._raw_shell_command(observed_audio_command),
            audio,
        )
        camera = None
        if bool(getattr(media, "video", False)):
            video = self._raw_video_parameters(profile.video_parameters)
            video_command = [
                *launcher_prefix,
                *input_args,
                "-i",
                video_url,
                "-map",
                "0:v:0",
                "-an",
                "-f",
                "rawvideo",
                "-r",
                str(video.frame_rate),
                "-pix_fmt",
                "yuv420p",
                "-vf",
                f"scale={video.width}:{video.height}",
                "-flush_packets",
                "1",
                "pipe:1",
            ]
            camera = types.raw.VideoStream(
                MediaSource.SHELL,
                self._raw_shell_command(video_command),
                video,
            )
            if video_url != url:
                logger.info(
                    "direct_video_adaptive_pair_raw chat_id=%s media_id=%s "
                    "audio_host=%s video_host=%s audio_format_id=%s video_format_id=%s",
                    chat_id,
                    str(getattr(media, "id", "") or ""),
                    self._direct_url_host(url),
                    self._direct_url_host(video_url),
                    str(getattr(source, "format_id", "") or "?"),
                    str(getattr(source, "video_format_id", "") or "?"),
                )
        return types.raw.Stream(microphone=microphone, camera=camera), event_path

    async def _prime_direct_external_audio(self, session: dict) -> None:
        """Decode a short PCM runway; first frame is the only startup gate."""
        process = session.get("process")
        if process is None or process.stdout is None:
            session["error"] = "decoder_not_started"
            session["first_frame_ready"].set()
            session["ready"].set()
            return
        stdout = process.stdout
        frame_bytes = int(session["frame_bytes"])
        target_frames = int(session["target_frames"])
        try:
            for index in range(target_frames):
                frame = await stdout.readexactly(frame_bytes)
                session["frames"].append(frame)
                if index == 0:
                    session["first_frame_ms"] = int(
                        (time.perf_counter() - session["started"]) * 1000
                    )
                    session["first_frame_ready"].set()
            session["prime_ms"] = int(
                (time.perf_counter() - session["started"]) * 1000
            )
        except asyncio.CancelledError:
            session["error"] = "cancelled"
            raise
        except Exception as ex:
            session["error"] = f"{type(ex).__name__}:{str(ex)[:160]}"
        finally:
            session["first_frame_ready"].set()
            session["ready"].set()

    async def _close_direct_external_audio(self, session: dict | None) -> None:
        if not session or session.get("closed"):
            return
        session["closed"] = True
        chat_id = int(session.get("chat_id") or 0)
        if chat_id and self._direct_external_audio_sessions.get(chat_id) is session:
            self._direct_external_audio_sessions.pop(chat_id, None)
        current = asyncio.current_task()
        for key in (
            "prime_task",
            "jit_task",
            "pump_task",
            "unmute_task",
            "vplay_close_watchdog",
        ):
            task = session.get(key)
            if task is not None and task is not current and not task.done():
                task.cancel()
        process = session.get("process")
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=0.35)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.2)
                except Exception:
                    pass

    async def _prepare_initial_direct_external_stream(
        self,
        profile,
        media,
        *,
        chat_id: int,
        placeholder_only: bool = False,
    ):
        """Install an EXTERNAL audio capture source before URL resolution.

        The stream itself has no network dependency.  For /play the decoder is
        attached to this same session when the winning direct URL arrives. For
        /vplay it is an audio-only placeholder that connects the call early and
        is replaced by the final raw A/V sources without reconnecting.
        """
        _prefix, ffmpeg_abs, _mode = self._prepare_raw_ffmpeg_launcher()
        audio = self._raw_audio_parameters(profile.audio_parameters)
        frame_bytes = max(1, int(audio.bitrate) * int(audio.channels) * 2 // 100)
        target_frames = max(2, min(50, int(
            getattr(config, "DIRECT_EXTERNAL_PREBUFFER_FRAMES", 4) or 4
        )))
        started = time.perf_counter()
        session = {
            "chat_id": int(chat_id),
            "media_id": str(getattr(media, "id", "") or ""),
            "process": None,
            "ffmpeg_abs": ffmpeg_abs,
            "frame_bytes": frame_bytes,
            "target_frames": target_frames,
            "frames": deque(),
            "ready": asyncio.Event(),
            "first_frame_ready": asyncio.Event(),
            "connected": asyncio.Event(),
            "first_frame_accepted": asyncio.Event(),
            "unmute_confirmed": asyncio.Event(),
            "unmute_failed": asyncio.Event(),
            "send_lock": asyncio.Lock(),
            "error": "",
            "closed": False,
            "started": started,
            "prime_ms": -1,
            "first_frame_ms": -1,
            "first_frame_accepted_ns": 0,
            "unmute_confirmed_ns": 0,
            "real_pcm_clock_baseline": None,
            "connected_ns": 0,
            "activated": False,
            "jit_kick_accepted": False,
            "placeholder_only": bool(placeholder_only),
            "early_connect": True,
            "sample_rate": int(audio.bitrate),
            "channels": int(audio.channels),
            "trace": None,
        }
        microphone = types.raw.AudioStream(MediaSource.EXTERNAL, "", audio)
        previous = self._direct_external_audio_sessions.get(int(chat_id))
        if previous is not None and previous is not session and not previous.get("closed"):
            await self._close_direct_external_audio(previous)
        self._direct_external_audio_sessions[int(chat_id)] = session
        logger.info(
            "direct_external_connect_source_prepared chat_id=%s media_id=%s "
            "frame_bytes=%s target_frames=%s url_ready=0 placeholder_only=%s",
            chat_id, session["media_id"], frame_bytes, target_frames,
            int(bool(placeholder_only)),
        )
        return types.raw.Stream(microphone=microphone, camera=None), session

    async def _start_initial_direct_external_decoder(self, source, session: dict) -> None:
        """Attach the winning direct URL to an already-connected EXTERNAL source."""
        if session.get("closed"):
            raise RuntimeError("external_session_closed")
        if session.get("process") is not None:
            return
        url = str(getattr(source, "url", "") or "")
        if not url.startswith(("http://", "https://")):
            raise ValueError("external_prebuffer_missing_url")
        ffmpeg_abs = str(session.get("ffmpeg_abs") or self._prepare_raw_ffmpeg_launcher()[1])
        argv = [
            ffmpeg_abs,
            *self._cold_direct_input_args(source),
            "-i", url,
            "-map", "0:a:0",
            "-vn",
            "-f", "s16le",
            "-ac", str(session["channels"]),
            "-ar", str(session["sample_rate"]),
            "-flush_packets", "1",
            "pipe:1",
        ]
        session["started"] = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        session["process"] = process
        prime_task = asyncio.create_task(
            self._prime_direct_external_audio(session),
            name=f"direct-pcm-prime:{session['chat_id']}:{session['media_id']}",
        )
        session["prime_task"] = prime_task
        self._track_owned_task(prime_task, self._direct_external_audio_tasks)
        logger.info(
            "direct_external_prebuffer_started chat_id=%s media_id=%s "
            "frame_bytes=%s target_frames=%s ffmpeg_abs=%s early_connect=%s",
            session["chat_id"], session["media_id"], session["frame_bytes"],
            session["target_frames"], ffmpeg_abs, int(bool(session.get("early_connect"))),
        )

    async def _build_initial_direct_external_stream(
        self,
        source,
        profile,
        media,
        *,
        chat_id: int,
    ):
        """Compatibility wrapper: prepare EXTERNAL capture and start decoder."""
        if bool(getattr(media, "video", False)):
            raise ValueError("external_prebuffer_audio_only")
        stream, session = await self._prepare_initial_direct_external_stream(
            profile, media, chat_id=chat_id, placeholder_only=False
        )
        await self._start_initial_direct_external_decoder(source, session)
        return stream, session

    async def _jit_prime_external_capture(self, client, session: dict) -> None:
        """Warm NTgCalls EXTERNAL capture while the call is still connecting."""
        if not bool(getattr(config, "DIRECT_EXTERNAL_JIT_FEED", True)):
            return
        binding = getattr(client, "_binding", None)
        send = getattr(binding, "send_external_frame", None)
        if not callable(send):
            return
        retry = max(0.005, min(0.05, float(
            getattr(config, "DIRECT_EXTERNAL_JIT_RETRY_MS", 10) or 10
        ) / 1000.0))
        silence = bytes(int(session["frame_bytes"]))
        while (
            not self._shutting_down
            and not session.get("closed")
            and not session["connected"].is_set()
        ):
            try:
                async with session["send_lock"]:
                    await send(
                        int(session["chat_id"]),
                        StreamDevice.MICROPHONE,
                        silence,
                        FrameData(int(time.time() * 1000), 0, 0, 0),
                    )
                if not session.get("jit_kick_accepted"):
                    session["jit_kick_accepted"] = True
                    logger.info(
                        "direct_external_jit_capture_ready chat_id=%s media_id=%s "
                        "first_frame_ms=%s connect_overlap=1",
                        session["chat_id"], session["media_id"],
                        session.get("first_frame_ms", -1),
                    )
                    self._log_direct_startup_event(
                        "external_capture_jit_ready",
                        chat_id=int(session["chat_id"]),
                        media_id=str(session["media_id"]),
                        evidence="ntgcalls_external_silence_frame_accepted",
                        status="accepted_pre_connect",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(retry)

    async def _pump_direct_external_audio(self, client, session: dict) -> None:
        binding = getattr(client, "_binding", None)
        send = getattr(binding, "send_external_frame", None)
        if not callable(send):
            session["error"] = "send_external_frame_unavailable"
            await self._close_direct_external_audio(session)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.010
        media_id = str(session.get("media_id") or "")
        try:
            while not self._shutting_down and not session.get("closed"):
                current = queue.get_current(int(session["chat_id"]))
                if str(getattr(current, "id", "") or "") != media_id:
                    break
                if session["frames"]:
                    frame = session["frames"].popleft()
                else:
                    frame = await session["process"].stdout.readexactly(
                        int(session["frame_bytes"])
                    )
                delay = deadline - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                async with session["send_lock"]:
                    await send(
                        int(session["chat_id"]),
                        StreamDevice.MICROPHONE,
                        frame,
                        FrameData(int(time.time() * 1000), 0, 0, 0),
                    )
                deadline = max(deadline + 0.010, loop.time())
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, EOFError):
            current = queue.get_current(int(session["chat_id"]))
            if (
                not self._shutting_down
                and str(getattr(current, "id", "") or "") == media_id
            ):
                chat_id = int(session["chat_id"])
                logger.info(
                    "direct_external_prebuffer_eof chat_id=%s media_id=%s",
                    chat_id, media_id,
                )
                if startup_gate.in_gate_window(chat_id):
                    startup_gate.signal_fatal(chat_id, "external_audio_eof_in_gate")
                else:
                    try:
                        if not await self._try_direct_local_failover(chat_id):
                            await self.play_next(chat_id)
                    except Exception as ex:
                        logger.debug(
                            "external audio EOF recovery failed chat_id=%s error=%s",
                            chat_id, type(ex).__name__,
                        )
        except (ConnectionNotFound, ConnectionError) as ex:
            chat_id = int(session.get("chat_id") or 0)
            current = queue.get_current(chat_id) if chat_id else None
            expected_shutdown = bool(
                self._shutting_down
                or session.get("closed")
                or chat_id in self._stopped_chats
                or str(getattr(current, "id", "") or "") != media_id
            )
            if not expected_shutdown and chat_id:
                try:
                    expected_shutdown = not await self.has_active_group_call(chat_id)
                except Exception:
                    expected_shutdown = False
            log = logger.debug if expected_shutdown else logger.warning
            log(
                "direct_external_prebuffer_pump_connection_end chat_id=%s media_id=%s "
                "error=%s expected_shutdown=%s",
                chat_id, media_id, type(ex).__name__, int(expected_shutdown),
            )
        except Exception as ex:
            logger.warning(
                "direct_external_prebuffer_pump_failed chat_id=%s media_id=%s error=%s",
                session.get("chat_id"), media_id, type(ex).__name__,
            )
        finally:
            await self._close_direct_external_audio(session)

    async def _activate_direct_external_audio(self, client, session: dict) -> None:
        """Send first real 10ms PCM as soon as VC is connected."""
        if bool(session.get("placeholder_only")):
            logger.info(
                "direct_external_placeholder_connected chat_id=%s media_id=%s "
                "jit_capture=%s awaiting_source_swap=1",
                session["chat_id"], session["media_id"],
                int(bool(session.get("jit_kick_accepted"))),
            )
            return
        timeout = (
            float(getattr(config, "DIRECT_EXTERNAL_EARLY_CONNECT_READY_TIMEOUT_SEC", 4.0) or 4.0)
            if session.get("early_connect")
            else float(getattr(config, "DIRECT_EXTERNAL_PREBUFFER_READY_TIMEOUT_SEC", 0.35) or 0.35)
        )
        timeout = max(0.05, min(6.0, timeout))
        try:
            await asyncio.wait_for(session["first_frame_ready"].wait(), timeout=timeout)
        except asyncio.TimeoutError as ex:
            raise RuntimeError("external_prebuffer_not_ready") from ex
        if session.get("error") and not session["frames"]:
            raise RuntimeError(f"external_prebuffer_prime_failed:{session['error']}")
        if not session["frames"]:
            raise RuntimeError("external_prebuffer_empty")
        binding = getattr(client, "_binding", None)
        send = getattr(binding, "send_external_frame", None)
        if not callable(send):
            raise RuntimeError("send_external_frame_unavailable")
        first = session["frames"][0]
        send_started = time.perf_counter()
        retry_deadline = time.perf_counter() + (
            int(getattr(config, "DIRECT_EXTERNAL_REAL_FRAME_RETRY_MS", 200) or 200)
            / 1000.0
        )
        while True:
            try:
                async with session["send_lock"]:
                    # Serialize the clock sample and first real frame against
                    # the JIT silence feeder. No keepalive frame can advance the
                    # clock between this baseline and real PCM submission.
                    if session.get("real_pcm_clock_baseline") is None:
                        try:
                            session["real_pcm_clock_baseline"] = max(
                                0,
                                int(await client.time(int(session["chat_id"]))),
                            )
                        except Exception:
                            session["real_pcm_clock_baseline"] = 0
                    await send(
                        int(session["chat_id"]),
                        StreamDevice.MICROPHONE,
                        first,
                        FrameData(int(time.time() * 1000), 0, 0, 0),
                    )
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                if time.perf_counter() >= retry_deadline:
                    raise
                await asyncio.sleep(0.005)
        session["frames"].popleft()
        session["activated"] = True
        accepted_stamp = self._event_timestamp()
        session["first_frame_accepted_ns"] = int(accepted_stamp["monotonic_ns"])
        session["first_frame_accepted"].set()
        startup_trace = session.get("trace")
        if startup_trace is not None:
            startup_trace.mark("first_external_frame")
        first_ms = int((time.perf_counter() - send_started) * 1000)
        connect_to_real_ms = int(max(0, (
            session["first_frame_accepted_ns"]
            - int(session.get("connected_ns") or session["first_frame_accepted_ns"])
        ) / 1_000_000))
        logger.info(
            "direct_external_first_real_frame_sent chat_id=%s media_id=%s "
            "first_frame_ms=%s prime_ms=%s first_send_ms=%s "
            "connect_to_real_ms=%s jit_capture=%s buffered_frames=%s",
            session["chat_id"], session["media_id"],
            session.get("first_frame_ms", -1), session.get("prime_ms", -1),
            first_ms, connect_to_real_ms, int(bool(session.get("jit_kick_accepted"))),
            len(session["frames"]),
        )
        self._log_direct_startup_event(
            "first_external_audio_frame_accepted",
            chat_id=int(session["chat_id"]),
            media_id=str(session.get("media_id") or ""),
            timestamp=accepted_stamp,
            evidence="ntgcalls_send_external_frame_returned",
            bytes=len(first),
            status="accepted",
            detail=f"connect_to_real_ms={connect_to_real_ms}",
        )
        pump_task = asyncio.create_task(
            self._pump_direct_external_audio(client, session),
            name=f"direct-pcm-pump:{session['chat_id']}:{session['media_id']}",
        )
        session["pump_task"] = pump_task
        self._track_owned_task(pump_task, self._direct_external_audio_tasks)

    async def _wait_direct_external_packet_clock(
        self,
        client,
        *,
        chat_id: int,
        media_id: str,
        timeout: float = 0.40,
    ) -> bool:
        """Give /vplay's audio lead time to produce its first outgoing tick."""

        deadline = time.perf_counter() + max(0.05, min(0.60, float(timeout)))
        while time.perf_counter() < deadline:
            try:
                if int(await client.time(chat_id)) > 0:
                    logger.info(
                        "vplay_audio_lead_packet_ready chat_id=%s media_id=%s "
                        "reconnect=0",
                        chat_id,
                        media_id,
                    )
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.01)
        logger.info(
            "vplay_audio_lead_packet_timeout chat_id=%s media_id=%s "
            "timeout_ms=%s reconnect=0",
            chat_id,
            media_id,
            int(timeout * 1000),
        )
        return False

    @staticmethod
    def _event_timestamp() -> dict[str, int | str]:
        return {
            "wall": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "wall_time_ns": time.time_ns(),
            "monotonic_ns": time.perf_counter_ns(),
        }

    @staticmethod
    def _log_direct_startup_event(
        event: str,
        *,
        chat_id: int,
        media_id: str,
        timestamp: dict | None = None,
        **fields,
    ) -> None:
        stamp = timestamp or TgCall._event_timestamp()
        logger.info(
            "direct_startup_event event=%s wall=%s wall_time_ns=%s "
            "monotonic_ns=%s chat_id=%s media_id=%s evidence=%s bytes=%s "
            "outgoing_clock=%s status=%s pid=%s return_code=%s detail=%s",
            event,
            stamp.get("wall", ""),
            stamp.get("wall_time_ns", ""),
            stamp.get("monotonic_ns", ""),
            chat_id,
            media_id,
            fields.get("evidence", ""),
            fields.get("bytes", ""),
            fields.get("outgoing_clock", ""),
            fields.get("status", ""),
            fields.get("pid", ""),
            fields.get("return_code", ""),
            str(fields.get("detail", "") or "")[:180],
        )

    @staticmethod
    def _no_audio_source_error(ex: BaseException) -> bool:
        # PyTgCalls/ntgcalls may wrap NoAudioSourceFound under ShellError.
        seen: set[int] = set()
        current: BaseException | None = ex
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            name = type(current).__name__.casefold()
            msg = str(current or "").casefold()
            if "noaudiosourcefound" in name or "no audio source" in msg:
                return True
            current = getattr(current, "__cause__", None) or getattr(
                current, "__context__", None
            )
        return False

    @staticmethod
    def _sanitize_probe_text(text: str, url: str | None) -> str:
        clean = str(text or "").replace("\r", " ").replace("\n", " ")
        if url:
            clean = clean.replace(str(url), "[direct_url]")
        return clean[:180]

    def _direct_runtime_diag(self, client, stream=None, source=None) -> dict:
        return {
            "pytgcalls_version": self._package_version("py-tgcalls", "pytgcalls"),
            "ntgcalls_version": self._package_version("ntgcalls"),
            "play_method": self._callable_signature(getattr(client, "play", None)),
            "input_type": (
                f"{type(stream).__module__}.{type(stream).__name__}"
                if stream is not None
                else ""
            ),
            "url_host": str(
                getattr(source, "host", None)
                or self._direct_url_host(getattr(source, "url", None))
                or ""
            ),
            "audio_format": str(getattr(source, "audio_format", "") or ""),
            "url_present": int(bool(getattr(source, "url", None))),
            "pot_in_url": self._direct_url_has_pot(getattr(source, "url", None)),
        }

    async def _probe_direct_audio_open(
        self,
        source,
        *,
        timeout: float = 6.0,
    ) -> tuple[bool, str]:
        url = str(getattr(source, "url", "") or "")
        if not url.startswith(("http://", "https://")):
            return False, "missing_direct_url"

        headers = self._ffmpeg_header_blob(getattr(source, "headers", None))
        proxy = str(getattr(source, "proxy", "") or "").strip()

        def _run_probe() -> tuple[bool, str]:
            ffprobe = shutil.which("ffprobe")
            if ffprobe:
                cmd = [ffprobe, "-v", "error"]
                if headers:
                    cmd.extend(["-headers", headers])
                if proxy.startswith(("http://", "https://")):
                    cmd.extend(["-http_proxy", proxy])
                elif not proxy:
                    cmd.extend(netbind.ffmpeg_local_addr_args())
                cmd.extend(
                    [
                        "-user_agent",
                        _BROWSER_UA,
                    ]
                )
                cmd.extend(
                    [
                        "-rw_timeout",
                        "5000000",
                        "-select_streams",
                        "a:0",
                        "-show_entries",
                        "stream=codec_name,codec_type",
                        "-of",
                        "json",
                        url,
                    ]
                )
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=max(2.0, float(timeout)),
                    )
                except Exception as ex:
                    return False, f"ffprobe_error:{type(ex).__name__}"
                output = f"{result.stdout} {result.stderr}"
                if result.returncode == 0 and "audio" in output:
                    return True, "ffprobe_audio_open_ok"
                return False, "ffprobe_audio_open_fail:" + self._sanitize_probe_text(
                    output, url
                )

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                return False, "ffmpeg_missing"
            cmd = [ffmpeg, "-hide_banner", "-v", "error"]
            if headers:
                cmd.extend(["-headers", headers])
            if proxy.startswith(("http://", "https://")):
                cmd.extend(["-http_proxy", proxy])
            elif not proxy:
                cmd.extend(netbind.ffmpeg_local_addr_args())
            cmd.extend(
                [
                    "-user_agent",
                    _BROWSER_UA,
                ]
            )
            cmd.extend(
                [
                    "-rw_timeout",
                    "5000000",
                    "-i",
                    url,
                    "-map",
                    "0:a:0",
                    "-t",
                    "0.1",
                    "-f",
                    "null",
                    "-",
                ]
            )
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=max(2.0, float(timeout)),
                )
            except Exception as ex:
                return False, f"ffmpeg_error:{type(ex).__name__}"
            output = f"{result.stdout} {result.stderr}"
            if result.returncode == 0:
                return True, "ffmpeg_audio_open_ok"
            return False, "ffmpeg_audio_open_fail:" + self._sanitize_probe_text(
                output, url
            )

        return await asyncio.to_thread(_run_probe)

    def _build_direct_media_stream(self, source, profile, media):
        kwargs = {
            "media_path": str(getattr(source, "url", "") or ""),
            "audio_parameters": profile.audio_parameters,
            "video_parameters": profile.video_parameters,
            "audio_flags": types.MediaStream.Flags.REQUIRED,
            "video_flags": (
                types.MediaStream.Flags.AUTO_DETECT
                if getattr(media, "video", False)
                else types.MediaStream.Flags.IGNORE
            ),
        }
        supported = set()
        try:
            supported = set(inspect.signature(types.MediaStream).parameters)
        except Exception:
            supported = set(kwargs)
        if "ffmpeg_parameters" in supported:
            # Headers ride inside ffmpeg_parameters as one -headers blob.
            # MediaStream(headers=...) emits a repeated -headers flag per dict
            # entry (pytgcalls/ffmpeg.py:build_command) and ffmpeg keeps only
            # the last, silently dropping the User-Agent.
            kwargs["ffmpeg_parameters"] = self._direct_ffmpeg_parameters(source)
        elif "headers" in supported and getattr(source, "headers", None):
            kwargs["headers"] = dict(source.headers)
        return types.MediaStream(**kwargs)

    def _log_direct_playback_diag(
        self,
        event: str,
        *,
        chat_id: int,
        media,
        client,
        source,
        stream=None,
        **fields,
    ) -> None:
        diag = self._direct_runtime_diag(client, stream=stream, source=source)
        logger.info(
            "%s chat_id=%s media_id=%s video=%s direct_stream_resolved=%s "
            "direct_audio_source_created=%s direct_player_start=%s "
            "pytgcalls_stream_started=%s telegram_audio_packets_sending=%s "
            "vc_audio_audible_gate_ok=%s local_download_started=%s "
            "playback_source=%s pytgcalls_version=%s ntgcalls_version=%s "
            "play_method=%s input_type=%s url_host=%s audio_format=%s "
            "url_present=%s ffmpeg_input_open=%s reason=%s detail=%s pot_in_url=%s",
            event,
            chat_id,
            getattr(media, "id", None),
            int(bool(getattr(media, "video", False))),
            fields.get("direct_stream_resolved", int(bool(getattr(source, "url", None)))),
            fields.get("direct_audio_source_created", int(stream is not None)),
            fields.get("direct_player_start", ""),
            fields.get("pytgcalls_stream_started", 0),
            fields.get("telegram_audio_packets_sending", 0),
            fields.get("vc_audio_audible_gate_ok", 0),
            fields.get("local_download_started", 0),
            fields.get("playback_source", ""),
            diag["pytgcalls_version"],
            diag["ntgcalls_version"],
            diag["play_method"],
            diag["input_type"],
            diag["url_host"],
            diag["audio_format"],
            diag["url_present"],
            fields.get("ffmpeg_input_open", ""),
            fields.get("reason", ""),
            self._sanitize_probe_text(
                str(fields.get("detail", "") or ""),
                getattr(source, "url", None),
            ),
            diag["pot_in_url"],
        )

    async def _notify_join_status(
        self, message: Message, _lang: dict, text: str
    ) -> None:
        try:
            await utils.edit_text(message, text, reply_markup=None, ignore_stale=True)
        except Exception:
            pass

    async def _notify_stream_busy(
        self, chat_id: int, message: Message, _lang: dict, *, where: str
    ) -> None:
        """Tell the user every stream slot is taken, and log why.

        Capacity is derived at runtime, so the log carries the live scaling
        view (baseline / capacity / auto ceiling / active / reason) — that is
        what distinguishes "the VPS is genuinely saturated" from "the ceiling
        is mis-derived".
        """
        try:
            scaling = resource_manager.stream_scaling()
        except Exception:
            scaling = {}
        logger.warning(
            "stream admission refused at=%s chat_id=%s scaling=%s",
            where, chat_id, scaling,
        )
        await self._notify_join_status(
            message,
            _lang,
            _lang.get(
                "play_stream_busy",
                "All stream slots are busy right now. Please try again shortly.",
            ),
        )

    def _prefetch_task(self, chat_id: int):
        return self.prefetch_manager.task(chat_id)

    def _normalize_autoplay_key(self, value: str | None) -> str:
        if not isinstance(value, str):
            return ""
        text = value.casefold()
        text = re.sub(r"[​-‍⁠﻿]", "", text)
        text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
        return re.sub(r"\s+", " ", text).strip()

    def _title_key(self, media: Media | Track | None) -> str:
        raw = self._normalize_autoplay_key(getattr(media, "title", None))
        if not raw:
            return ""
        tokens = []
        for token in raw.split():
            if token in self.AUTOPLAY_TITLE_KEY_STOPWORDS:
                continue
            if token.isascii() and len(token) <= 1:
                continue
            if token.isdigit():
                continue
            if token not in tokens:
                tokens.append(token)
        if not tokens:
            return raw
        return " ".join(tokens[:8])

    def _artist_key(self, media: Media | Track | None) -> str:
        raw = self._normalize_autoplay_key(getattr(media, "channel_name", None))
        if not raw:
            return ""
        chunks = [part for part in raw.split() if part not in self.AUTOPLAY_ARTIST_KEY_STOPWORDS]
        return " ".join(chunks) or raw

    def _remember_autoplay(self, chat_id: int, track: Media | Track | None) -> None:
        if not track:
            return

        track_id = getattr(track, "id", None)
        if track_id:
            self.autoplay_recent_ids[chat_id].append(track_id)

        title_key = self._title_key(track)
        if title_key:
            self.autoplay_recent_titles[chat_id].append(title_key)

        artist_key = self._artist_key(track)
        if artist_key:
            self.autoplay_recent_artists[chat_id].append(artist_key)
            if self.autoplay_last_artist.get(chat_id) == artist_key:
                self.autoplay_artist_streak[chat_id] = self.autoplay_artist_streak.get(chat_id, 0) + 1
            else:
                self.autoplay_artist_streak[chat_id] = 1
            self.autoplay_last_artist[chat_id] = artist_key
        else:
            self.autoplay_artist_streak[chat_id] = 0
            self.autoplay_last_artist.pop(chat_id, None)

    def _autoplay_context(self, chat_id: int, current: Media | Track | None) -> dict:
        recent_titles = set(self.autoplay_recent_titles.get(chat_id, ()))
        current_title = self._title_key(current)
        if current_title:
            recent_titles.add(current_title)

        recent_artists = list(self.autoplay_recent_artists.get(chat_id, ()))
        current_artist = self._artist_key(current)
        if current_artist and (not recent_artists or recent_artists[-1] != current_artist):
            recent_artists.append(current_artist)

        streak = self.autoplay_artist_streak.get(chat_id, 0)
        if not streak and current_artist:
            streak = 1

        return {
            "recent_title_keys": recent_titles,
            "recent_artist_keys": recent_artists,
            "current_artist_streak": streak,
            "max_same_artist_streak": self.autoplay_max_artist_streak,
            "required_overlap_min": config.AUTOPLAY_REQUIRED_OVERLAP_MIN,
            "same_artist_penalty": config.AUTOPLAY_SAME_ARTIST_PENALTY,
            "repeat_artist_streak_penalty": config.AUTOPLAY_REPEAT_ARTIST_STREAK_PENALTY,
            "recent_title_penalty": config.AUTOPLAY_RECENT_TITLE_PENALTY,
            "seed_exact_title_penalty": config.AUTOPLAY_SEED_EXACT_TITLE_PENALTY,
        }

    def _autoplay_excludes(self, chat_id: int, current: Media | Track | None) -> set[str]:
        excludes = set()
        if current and getattr(current, "id", None):
            excludes.add(current.id)
        for item in queue.get_queue(chat_id):
            item_id = getattr(item, "id", None)
            if item_id:
                excludes.add(item_id)
        excludes.update(self.autoplay_recent_ids.get(chat_id, ()))
        return excludes

    async def _prefetch_next(self, chat_id: int) -> None:
        # The exact-140 promotion runs only after the current track is audible,
        # so it can never delay VC join. Seed the queued-next direct URL while
        # the current song is playing; a later /play/skip can then promote the
        # cached 140 with only a cheap freshness probe.
        try:
            next_media = queue.get_next(chat_id, check=True)
            next_id = str(getattr(next_media, "id", "") or "").strip() if next_media else ""
            next_video = bool(getattr(next_media, "video", False)) if next_media else False
            next_source = str(getattr(next_media, "source", "") or "").lower() if next_media else ""
            if (
                len(next_id) == 11
                and not next_video
                and next_source not in {
                    "telegram_remote", "telegram_local", "tiktok_remote", "tiktok_local",
                    "facebook_remote", "facebook_local", "soundcloud",
                    "soundcloud_remote", "soundcloud_local",
                }
            ):
                yt.warm_audio140_source(next_id)
        except Exception as ex:
            logger.debug("queued background 140 kickoff skipped chat_id=%s: %s", chat_id, ex)

        client = await db.get_assistant(chat_id)
        profile = self.stream_profile.select(chat_id, client)
        await self.prefetch_manager.start_next(chat_id, quality_tier=profile.download_tier)

    async def pause(self, chat_id: int) -> bool:
        client = await db.get_assistant(chat_id)
        await db.playing(chat_id, paused=True)
        return await client.pause(chat_id)

    async def resume(self, chat_id: int) -> bool:
        client = await db.get_assistant(chat_id)
        await db.playing(chat_id, paused=False)
        return await client.resume(chat_id)

    async def _delete_status_message(
        self, chat_id: int, message_id: int, *, reason: str = ""
    ) -> None:
        """Delete a bot status card (queued / searching / old now-playing)."""
        mid = int(message_id or 0)
        if not mid:
            return
        try:
            await app.delete_messages(chat_id, mid)
            logger.info(
                "Deleted play status message chat_id=%s msg_id=%s reason=%s",
                chat_id,
                mid,
                reason or "cleanup",
            )
            return
        except Exception as ex:
            logger.debug(
                "app.delete status msg failed chat=%s msg=%s: %s", chat_id, mid, ex
            )
        try:
            from AnonX_3 import bot_api

            await bot_api.delete_message(chat_id, mid)
            logger.info(
                "Deleted play status (bot_api) chat_id=%s msg_id=%s reason=%s",
                chat_id,
                mid,
                reason or "cleanup",
            )
        except Exception as ex:
            logger.debug(
                "bot_api delete status msg failed chat=%s msg=%s: %s", chat_id, mid, ex
            )

    async def _delete_now_playing(self, chat_id: int, media=None) -> None:
        """Delete the bot-owned playback card before its queue state is discarded."""
        media = media or queue.get_current(chat_id)
        message_id = int(getattr(media, "message_id", 0) or 0)
        status_id = int(getattr(media, "status_message_id", 0) or 0)
        if not message_id and not status_id:
            return

        # Clear first so timer/duplicate stream-end updates cannot target a stale card.
        if media is not None:
            media.message_id = 0
            try:
                media.status_message_id = 0
            except Exception:
                pass
        for mid in {message_id, status_id}:
            if mid:
                await self._delete_status_message(
                    chat_id, mid, reason="now_playing_cleanup"
                )

    def _play_next_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._play_next_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._play_next_locks[chat_id] = lock
        return lock

    def _pick_ready_local(
        self, media, quality_tier: str | None = None
    ) -> str | None:
        """Return a complete local file path if parallel download already finished."""
        if not media:
            return None
        mid = str(getattr(media, "id", "") or "")
        video = bool(getattr(media, "video", False))
        min_b = 512 * 1024 if video else 64 * 1024
        candidates: list[str] = []
        for cand in (
            getattr(media, "file_path", None),
            getattr(media, "local_path", None),
        ):
            if cand and cand not in candidates:
                candidates.append(str(cand))
        if mid:
            # The cache resolver knows all quality-tier filenames, CDN READY
            # artifacts, and safe media extensions.  Check it before the
            # legacy fixed candidate list so a resource-tier change never
            # reopens yt-dlp for an already-complete file.
            try:
                ready = yt._local_ready_path(
                    mid, video=video, quality_tier=quality_tier
                )
                if yt.is_complete_media_file(ready, min_bytes=min_b):
                    return ready
            except Exception:
                pass
            for cand in (
                yt.get_download_filename(mid, video=video, quality_tier=quality_tier),
                yt.get_download_filename(mid, video=video, quality_tier=None),
                f"downloads/{mid}.webm",
                f"downloads/{mid}.mp4",
                f"downloads/{mid}.m4a",
                f"cache/{mid}.webm",
                f"cache/{mid}.mp4",
                f"cache/{mid}.m4a",
            ):
                if cand and cand not in candidates:
                    candidates.append(cand)
        for path in candidates:
            if yt.is_complete_media_file(path, min_bytes=min_b):
                return path
        return None

    async def _await_parallel_local(
        self,
        chat_id: int,
        media,
        *,
        quality_tier: str | None = None,
        ping: float | None = None,
        progress_message=None,
        progress_lang: dict | None = None,
        timeout: float | None = None,
    ) -> str | None:
        """Join the actual parallel download task; no polling or fixed delay."""
        ready = self._pick_ready_local(media, quality_tier)
        if ready:
            return ready
        try:
            path = await self.prefetch_manager.await_current_cache_or_download(
                chat_id,
                media,
                quality_tier=quality_tier,
                ping=ping,
                progress_message=progress_message,
                progress_lang=progress_lang,
            )
            min_b = 512 * 1024 if getattr(media, "video", False) else 64 * 1024
            if yt.is_complete_media_file(path, min_bytes=min_b):
                return path
        except Exception as ex:
            logger.debug("await parallel local failed chat_id=%s: %s", chat_id, ex)
        return self._pick_ready_local(media, quality_tier)

    async def _watch_parallel_local_for_failover(
        self, chat_id: int, media, quality_tier: str | None
    ) -> None:
        """As parallel download completes, feed DirectWatchdog local_path."""
        try:
            task = self.prefetch_manager.current_task(chat_id, media)
            if task is not None:
                await asyncio.shield(task)
            path = self._pick_ready_local(media, quality_tier)
            if path:
                direct_watchdog.update_local(chat_id, path)
                if hasattr(media, "local_path"):
                    media.local_path = path
                logger.info(
                    "youtube_direct_background_cache_ready chat_id=%s "
                    "media_id=%s video=%s path=%s playback_source=%s",
                    chat_id,
                    getattr(media, "id", None),
                    int(bool(getattr(media, "video", False))),
                    path,
                    getattr(media, "playback_source", None) or "direct",
                )
            else:
                logger.info(
                    "youtube_direct_background_cache_incomplete chat_id=%s "
                    "media_id=%s video=%s playback_source=%s",
                    chat_id,
                    getattr(media, "id", None),
                    int(bool(getattr(media, "video", False))),
                    getattr(media, "playback_source", None) or "direct",
                )
        except Exception:
            pass

    async def _start_youtube_direct_background_cache(
        self,
        chat_id: int,
        media,
        quality_tier: str | None,
    ) -> None:
        """Start one silent local/cache owner after direct playback is audible."""
        ready = self._pick_ready_local(media, quality_tier)
        if ready:
            media.local_path = ready
            direct_watchdog.update_local(chat_id, ready)
            logger.info(
                "youtube_direct_background_cache_ready chat_id=%s media_id=%s "
                "video=%s path=%s playback_source=%s reason=already_ready",
                chat_id,
                getattr(media, "id", None),
                int(bool(getattr(media, "video", False))),
                ready,
                getattr(media, "playback_source", None) or "direct",
            )
            return

        try:
            owner = copy.copy(media)
        except Exception:
            owner = media
        for attr in PrefetchManager._DOWNLOAD_PROGRESS_ATTRS:
            try:
                setattr(owner, attr, None)
            except Exception:
                pass
        setattr(owner, "_direct_background_cache", True)

        await self.prefetch_manager.start_current_cache(
            chat_id,
            owner,
            quality_tier=quality_tier,
            force=True,
            immediate=True,
            local_only=True,
        )
        task = self.prefetch_manager.current_task(chat_id, owner)
        if task is None:
            ready = self._pick_ready_local(owner, quality_tier)
            if ready:
                media.local_path = ready
                direct_watchdog.update_local(chat_id, ready)
                logger.info(
                    "youtube_direct_background_cache_ready chat_id=%s media_id=%s "
                    "video=%s path=%s playback_source=%s reason=sync_ready",
                    chat_id,
                    getattr(media, "id", None),
                    int(bool(getattr(media, "video", False))),
                    ready,
                    getattr(media, "playback_source", None) or "direct",
                )
            else:
                logger.info(
                    "youtube_direct_background_cache_skipped chat_id=%s media_id=%s "
                    "video=%s playback_source=%s reason=no_owner",
                    chat_id,
                    getattr(media, "id", None),
                    int(bool(getattr(media, "video", False))),
                    getattr(media, "playback_source", None) or "direct",
                )
            return

        logger.info(
            "youtube_direct_background_cache_started chat_id=%s media_id=%s "
            "video=%s playback_source=%s duplicate_guard=current_cache",
            chat_id,
            getattr(media, "id", None),
            int(bool(getattr(media, "video", False))),
            getattr(media, "playback_source", None) or "direct",
        )
        if self._shutting_down:
            return
        watcher = asyncio.create_task(
            self._watch_parallel_local_for_failover(
                chat_id,
                media,
                quality_tier,
            ),
            name=f"direct-background-cache:{chat_id}:{getattr(media, 'id', '')}",
        )
        self._track_owned_task(watcher, self._direct_cache_tasks)
        try:
            setattr(media, "_direct_background_cache_task", watcher)
        except Exception:
            pass

    @staticmethod
    def _hydrate_direct_metadata(media) -> None:
        if getattr(media, "duration_sec", None) or str(
            getattr(media, "title", "")
        ) not in ("", "YouTube Video", "Unknown"):
            return
        resolved = yt._track_from_direct_metadata(
            str(getattr(media, "id", "")),
            getattr(media, "message_id", 0) or 0,
            video=bool(getattr(media, "video", False)),
        )
        if resolved is None:
            return
        media.title = getattr(resolved, "title", None) or media.title
        media.duration = getattr(resolved, "duration", None) or media.duration
        media.duration_sec = getattr(resolved, "duration_sec", 0) or getattr(
            media, "duration_sec", 0
        )
        media.channel_name = getattr(resolved, "channel_name", None) or getattr(
            media, "channel_name", ""
        )
        media.thumbnail = getattr(resolved, "thumbnail", None) or getattr(
            media, "thumbnail", ""
        )

    async def _run_direct_diagnostics(
        self,
        chat_id: int,
        media,
        direct_source,
    ) -> None:
        probe_ok, probe_reason = await probe_direct_url(
            getattr(direct_source, "url", None)
        )
        ffmpeg_ok = True
        ffmpeg_reason = "probe_disabled"
        if getattr(config, "DIRECT_AUDIO_PROBE", False):
            ffmpeg_ok, ffmpeg_reason = await self._probe_direct_audio_open(
                direct_source
            )
        logger.info(
            "youtube_direct_background_diagnostics chat_id=%s media_id=%s "
            "url_ok=%s url_reason=%s ffmpeg_ok=%s ffmpeg_reason=%s",
            chat_id,
            getattr(media, "id", None),
            int(bool(probe_ok)),
            probe_reason,
            int(bool(ffmpeg_ok)),
            ffmpeg_reason,
        )

    async def _run_direct_post_start_background(
        self,
        chat_id: int,
        message: Message,
        media,
        client,
        direct_source,
        quality_tier: str | None,
    ) -> None:
        """Run display/cache/profile/diagnostic work after stream acceptance."""
        try:
            self._hydrate_direct_metadata(media)
        except Exception as ex:
            logger.debug(
                "Direct metadata hydration skipped chat_id=%s error=%s",
                chat_id,
                type(ex).__name__,
            )

        try:
            refreshed = await asyncio.to_thread(
                self.stream_profile.select, chat_id, client
            )
            logger.info(
                "Playback stream profile refreshed in background chat_id=%s "
                "media_id=%s tier=%s reason=%s",
                chat_id,
                getattr(media, "id", None),
                refreshed.tier,
                refreshed.reason,
            )
        except Exception as ex:
            logger.debug(
                "Background stream profile refresh skipped chat_id=%s error=%s",
                chat_id,
                type(ex).__name__,
            )

        if (
            not self._shutting_down
            and config.THUMB_GEN
            and getattr(media, "_now_playing_thumb_task", None) is None
        ):
            media._now_playing_thumb_task = asyncio.create_task(
                thumb.generate(media, quality_tier=getattr(media, "stream_tier", "normal")),
                name=f"now-playing-thumb:{chat_id}",
            )
            self._track_owned_task(
                media._now_playing_thumb_task,
                self._thumbnail_tasks,
            )

        jobs = (
            self._start_youtube_direct_background_cache(
                chat_id,
                media,
                quality_tier,
            ),
            self._prefetch_next(chat_id),
            self._run_direct_diagnostics(
                chat_id,
                media,
                direct_source,
            ),
            update_now_playing(chat_id, message, media),
        )
        results = await asyncio.gather(*jobs, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.debug(
                    "Direct post-start background job failed chat_id=%s error=%s",
                    chat_id,
                    type(result).__name__,
                )

    def _schedule_direct_post_start_background(
        self,
        chat_id: int,
        message: Message,
        media,
        client,
        direct_source,
        quality_tier: str | None,
    ) -> None:
        if self._shutting_down:
            return
        task = asyncio.create_task(
            self._run_direct_post_start_background(
                chat_id,
                message,
                media,
                client,
                direct_source,
                quality_tier,
            ),
            name=f"direct-post-start:{chat_id}:{getattr(media, 'id', '')}",
        )
        self._track_post_start_task(chat_id, task)

    def _track_post_start_task(self, chat_id: int, task: asyncio.Task) -> None:
        self._track_owned_task(task, self._post_start_tasks)
        bucket = self._post_start_by_chat.setdefault(chat_id, set())
        bucket.add(task)

        def _cleanup(done: asyncio.Task) -> None:
            owned = self._post_start_by_chat.get(chat_id)
            if owned is None:
                return
            owned.discard(done)
            if not owned:
                self._post_start_by_chat.pop(chat_id, None)

        task.add_done_callback(_cleanup)

    def _cancel_post_start_tasks(self, chat_id: int) -> None:
        tasks = self._post_start_by_chat.pop(chat_id, set())
        current = asyncio.current_task()
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()

    def _schedule_profile_refresh(self, chat_id: int, media, client) -> None:
        """Refresh live resource inputs after a prejoined local fallback starts."""
        if self._shutting_down:
            return

        async def _refresh() -> None:
            try:
                refreshed = await asyncio.to_thread(
                    self.stream_profile.select, chat_id, client
                )
                logger.info(
                    "Playback stream profile refreshed after attach chat_id=%s "
                    "media_id=%s tier=%s reason=%s",
                    chat_id,
                    getattr(media, "id", None),
                    refreshed.tier,
                    refreshed.reason,
                )
            except Exception as ex:
                logger.debug(
                    "Post-attach stream profile refresh skipped chat_id=%s error=%s",
                    chat_id,
                    type(ex).__name__,
                )

        task = asyncio.create_task(
            _refresh(),
            name=f"post-attach-profile:{chat_id}:{getattr(media, 'id', '')}",
        )
        self._track_post_start_task(chat_id, task)

    async def _observe_initial_direct_media(
        self,
        *,
        chat_id: int,
        message: Message,
        media,
        client,
        direct_source,
        quality_tier: str | None,
        event_path: str,
        attached_event: asyncio.Event,
        play_state: dict,
        trace=None,
    ) -> None:
        """Observe subprocess/media milestones without gating playback."""
        media_id = str(getattr(media, "id", "") or "")
        seen_lines = 0
        decoded_seen = False
        packet_seen = False
        background_scheduled = False
        missed_target_logged = False
        started = time.perf_counter()
        # An empty path means the ffmpeg observer was skipped (missing helper or
        # unwritable event dir).  Playback still runs, so fall through to the
        # ntgcalls outgoing clock instead of waiting 30s for events that the
        # unwrapped ffmpeg will never emit.
        observed = bool(event_path)
        event_file = Path(event_path) if observed else None
        try:
            while time.perf_counter() - started < 30.0:
                if play_state.get("observer_done"):
                    return
                if event_file is not None and event_file.exists():
                    try:
                        lines = event_file.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()
                    except OSError:
                        lines = []
                    for line in lines[seen_lines:]:
                        try:
                            payload = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if str(payload.get("media_id", "")) != media_id:
                            continue
                        event = str(payload.get("event", "") or "")
                        stamp = {
                            "wall": payload.get("wall", ""),
                            "wall_time_ns": payload.get("wall_time_ns", ""),
                            "monotonic_ns": payload.get("monotonic_ns", ""),
                        }
                        self._log_direct_startup_event(
                            event,
                            chat_id=chat_id,
                            media_id=media_id,
                            timestamp=stamp,
                            evidence=payload.get("evidence", ""),
                            bytes=payload.get("bytes", ""),
                            pid=payload.get("pid", ""),
                            return_code=payload.get("return_code", ""),
                            detail=payload.get("detail", ""),
                        )
                        if trace and event == "ffmpeg_spawned":
                            trace.mark("ffmpeg_spawned")
                        elif trace and event == "raw_url_first_bytes":
                            trace.mark("raw_url_first_bytes")
                        elif event == "first_decoded_audio_frame":
                            decoded_seen = True
                            if trace:
                                trace.mark("first_decoded_audio_frame")
                        elif event in {
                            "ffmpeg_exited",
                            "ffmpeg_observer_failed",
                        } and play_state.get("status") == "failed":
                            if trace:
                                trace.finish("direct_failed")
                            return
                    seen_lines = max(seen_lines, len(lines))

                if (
                    (decoded_seen or not observed)
                    and play_state.get("status") != "failed"
                    and not packet_seen
                ):
                    try:
                        outgoing_time = int(await client.time(chat_id))
                    except Exception:
                        outgoing_time = 0
                    if outgoing_time > 0:
                        packet_seen = True
                        self._log_direct_startup_event(
                            "first_telegram_audio_packet_sent",
                            chat_id=chat_id,
                            media_id=media_id,
                            evidence="ntgcalls_outgoing_clock_advanced",
                            outgoing_clock=outgoing_time,
                            status="observed",
                        )
                        if trace:
                            trace.mark("first_telegram_audio_packet")
                            trace.mark("audible")
                            trace.mark("voice_started")
                        current = queue.get_current(chat_id)
                        if (
                            not background_scheduled
                            and str(getattr(current, "id", "") or "") == media_id
                        ):
                            background_scheduled = True
                            self._schedule_direct_post_start_background(
                                chat_id,
                                message,
                                media,
                                client,
                                direct_source,
                                quality_tier,
                            )
                            if trace:
                                trace.mark("background_queued")
                                trace.finish("ready")

                if packet_seen and attached_event.is_set():
                    return

                elapsed = time.perf_counter() - started
                if elapsed >= 12.0 and not missed_target_logged and not packet_seen:
                    missed_target_logged = True
                    self._log_direct_startup_event(
                        "first_packet_target_missed",
                        chat_id=chat_id,
                        media_id=media_id,
                        evidence="local_12s_deadline",
                        status=play_state.get("status", "pending"),
                    )
                poll_ms = max(
                    10,
                    min(
                        100,
                        int(
                            getattr(config, "DIRECT_FIRST_PACKET_POLL_MS", 20)
                            or 20
                        ),
                    ),
                )
                await asyncio.sleep(poll_ms / 1000.0)

            self._log_direct_startup_event(
                "startup_observer_timeout",
                chat_id=chat_id,
                media_id=media_id,
                evidence="local_30s_deadline",
                status=play_state.get("status", "pending"),
            )
            if trace:
                trace.finish("startup_observer_timeout")
        finally:
            try:
                if event_file is not None:
                    event_file.unlink(missing_ok=True)
            except OSError:
                # Windows keeps the helper's event file locked while FFmpeg
                # is alive; the helper removes it on process exit instead.
                pass

    async def _recover_initial_direct_with_mediastream(
        self,
        *,
        chat_id: int,
        message: Message,
        media,
        client,
        direct_source,
        language: dict,
        profile,
        quality_tier: str | None,
        attached_event: asyncio.Event,
        play_state: dict,
        trace=None,
    ) -> bool:
        """Retry initial YouTube direct with MediaStream after raw shell failure."""
        media_id = str(getattr(media, "id", "") or "")
        if trace:
            trace.mark("mediastream_recovery_start")
        try:
            media_stream = self._build_direct_media_stream(direct_source, profile, media)
        except Exception as ex:
            self._log_direct_playback_diag(
                "youtube_direct_mediastream_recovery_failed",
                chat_id=chat_id,
                media=media,
                client=client,
                source=direct_source,
                stream=None,
                direct_stream_resolved=True,
                direct_audio_source_created=False,
                direct_player_start="not_attempted",
                pytgcalls_stream_started=False,
                telegram_audio_packets_sending=False,
                vc_audio_audible_gate_ok=False,
                local_download_started=False,
                playback_source="mediastream_direct",
                ffmpeg_input_open="mediastream_build_failed",
                reason=type(ex).__name__,
                detail=str(ex),
            )
            return False

        self._log_direct_playback_diag(
            "youtube_direct_mediastream_recovery_created",
            chat_id=chat_id,
            media=media,
            client=client,
            source=direct_source,
            stream=media_stream,
            direct_stream_resolved=True,
            direct_audio_source_created=True,
            direct_player_start="pending",
            pytgcalls_stream_started=False,
            telegram_audio_packets_sending=False,
            vc_audio_audible_gate_ok=False,
            local_download_started=False,
            playback_source="mediastream_direct",
            ffmpeg_input_open="pytgcalls_check_stream",
            reason="raw_shell_recovery",
        )

        try:
            gate = await startup_gate.confirm_direct_start(
                chat_id,
                play_coro=self._play_with_startup_slot(
                    client,
                    chat_id=chat_id,
                    stream=media_stream,
                    unmute_mode="skip",
                ),
                on_started=lambda: trace.mark("audible") if trace else None,
            )
        except StreamCapacityError:
            play_state["status"] = "capacity_refused"
            play_state["observer_done"] = True
            attached_event.set()
            try:
                startup_gate.signal_fatal(chat_id, "stream_capacity_reached")
            except Exception:
                pass
            await self._notify_stream_busy(
                chat_id, message, language, where="direct_mediastream_recovery"
            )
            return True
        except Exception as ex:
            self._log_direct_playback_diag(
                "youtube_direct_mediastream_recovery_failed",
                chat_id=chat_id,
                media=media,
                client=client,
                source=direct_source,
                stream=media_stream,
                direct_stream_resolved=True,
                direct_audio_source_created=True,
                direct_player_start=(
                    "no_audio_source_found"
                    if self._no_audio_source_error(ex)
                    else "failed"
                ),
                pytgcalls_stream_started=False,
                telegram_audio_packets_sending=False,
                vc_audio_audible_gate_ok=False,
                local_download_started=False,
                playback_source="mediastream_direct",
                ffmpeg_input_open="pytgcalls_check_stream",
                reason=type(ex).__name__,
                detail=str(ex),
            )
            return False

        if not gate.ok:
            self._log_direct_playback_diag(
                "youtube_direct_mediastream_recovery_failed",
                chat_id=chat_id,
                media=media,
                client=client,
                source=direct_source,
                stream=media_stream,
                direct_stream_resolved=True,
                direct_audio_source_created=True,
                direct_player_start="failed",
                pytgcalls_stream_started=False,
                telegram_audio_packets_sending=False,
                vc_audio_audible_gate_ok=False,
                local_download_started=False,
                playback_source="mediastream_direct",
                ffmpeg_input_open="pytgcalls_check_stream",
                reason=gate.reason or "startup_gate_failed",
            )
            return False

        play_state["status"] = "recovered_mediastream"
        play_state["observer_done"] = True
        attached_event.set()
        if trace:
            trace.mark("stream_attached")
            trace.mark("voice_started")
        media.playback_source = "mediastream_direct"
        media.stream_url = str(getattr(direct_source, "url", "") or "")
        media.stream_tier = getattr(profile, "tier", None)
        media._cache_quality_tier = quality_tier
        media.source = getattr(media, "source", None) or "youtube_remote"
        self._log_direct_startup_event(
            "pytgcalls_stream_attached",
            chat_id=chat_id,
            media_id=media_id,
            evidence="mediastream_recovery_after_raw_shell_error",
            status="attached",
        )
        self._log_direct_playback_diag(
            "youtube_direct_mediastream_recovery_started",
            chat_id=chat_id,
            media=media,
            client=client,
            source=direct_source,
            stream=media_stream,
            direct_stream_resolved=True,
            direct_audio_source_created=True,
            direct_player_start="ok",
            pytgcalls_stream_started=True,
            telegram_audio_packets_sending=True,
            vc_audio_audible_gate_ok=True,
            local_download_started=False,
            playback_source="mediastream_direct",
            ffmpeg_input_open="ok",
            reason=gate.reason or "proved",
        )
        self._schedule_direct_post_start_background(
            chat_id,
            message,
            media,
            client,
            direct_source,
            quality_tier,
        )
        if trace:
            trace.mark("background_queued")
            trace.finish("ready")
        return True

    async def _monitor_initial_direct_play(
        self,
        *,
        chat_id: int,
        message: Message,
        media,
        client,
        direct_source,
        direct_stream,
        language: dict,
        profile,
        quality_tier: str | None,
        play_coro,
        attached_event: asyncio.Event,
        play_state: dict,
        playback_source: str = "raw_direct",
        trace=None,
    ) -> None:
        """Observe the detached ``client.play`` task and own its fallback."""
        media_id = str(getattr(media, "id", "") or "")
        try:
            await play_coro
        except asyncio.CancelledError:
            play_state["status"] = "cancelled"
            attached_event.set()
            raise
        except Exception as ex:
            # ShellError means ntgcalls never got a working shell process, so the
            # raw command itself is unusable in this environment (missing helper,
            # unparsable command, fd exhaustion).  Retrying it per track only
            # repeats the wasted join; the MediaStream path still works.
            shell_error = isinstance(ex, ShellError)
            raw_shell_error = shell_error and playback_source == "raw_direct"
            if raw_shell_error:
                if not self._raw_direct_disabled_reason:
                    self._raw_direct_disabled_reason = (
                        self._sanitize_probe_text(
                            str(ex), getattr(direct_source, "url", None)
                        )
                        or "ShellError"
                    )
                    logger.warning(
                        "Raw cold-start path disabled for this process after "
                        "ShellError; retrying MediaStream direct start. "
                        "chat_id=%s detail=%s",
                        chat_id,
                        self._raw_direct_disabled_reason,
                    )
                if trace:
                    trace.mark("raw_direct_failed")
                self._log_direct_playback_diag(
                    "youtube_raw_direct_startup_failed",
                    chat_id=chat_id,
                    media=media,
                    client=client,
                    source=direct_source,
                    stream=direct_stream,
                    direct_stream_resolved=True,
                    direct_audio_source_created=True,
                    direct_player_start=(
                        "no_audio_source_found"
                        if self._no_audio_source_error(ex)
                        else "failed"
                    ),
                    pytgcalls_stream_started=False,
                    telegram_audio_packets_sending=False,
                    vc_audio_audible_gate_ok=False,
                    local_download_started=False,
                    playback_source="raw_direct",
                    ffmpeg_input_open="raw_no_probe",
                    reason=type(ex).__name__,
                    detail=str(ex),
                )
                recovered = await self._recover_initial_direct_with_mediastream(
                    chat_id=chat_id,
                    message=message,
                    media=media,
                    client=client,
                    direct_source=direct_source,
                    language=language,
                    profile=profile,
                    quality_tier=quality_tier,
                    attached_event=attached_event,
                    play_state=play_state,
                    trace=trace,
                )
                if recovered:
                    return

            if playback_source in {"external_prebuffer", "external_early_connect"}:
                if trace:
                    trace.mark("external_prebuffer_failed")
                logger.warning(
                    "External prebuffer direct start failed; retrying supported "
                    "MediaStream source. chat_id=%s media_id=%s error=%s",
                    chat_id, media_id, type(ex).__name__,
                )
                recovered = await self._recover_initial_direct_with_mediastream(
                    chat_id=chat_id,
                    message=message,
                    media=media,
                    client=client,
                    direct_source=direct_source,
                    language=language,
                    profile=profile,
                    quality_tier=quality_tier,
                    attached_event=attached_event,
                    play_state=play_state,
                    trace=trace,
                )
                if recovered:
                    return

            play_state["status"] = "failed"
            play_state["observer_done"] = True
            attached_event.set()
            if trace:
                trace.finish("direct_failed")
            startup_gate.mark_direct_start_failed(
                chat_id, f"play_exception:{type(ex).__name__}"
            )
            self._log_direct_playback_diag(
                "youtube_direct_startup_failed",
                chat_id=chat_id,
                media=media,
                client=client,
                source=direct_source,
                stream=direct_stream,
                direct_stream_resolved=True,
                direct_audio_source_created=True,
                direct_player_start=(
                    "no_audio_source_found"
                    if self._no_audio_source_error(ex)
                    else "failed"
                ),
                pytgcalls_stream_started=False,
                telegram_audio_packets_sending=False,
                vc_audio_audible_gate_ok=False,
                local_download_started=False,
                playback_source=playback_source,
                ffmpeg_input_open=(
                    "raw_no_probe"
                    if playback_source == "raw_direct"
                    else "pytgcalls_check_stream"
                ),
                reason=type(ex).__name__,
                detail=str(ex),
            )
            logger.info(
                "Detached direct start failed; background fallback owns recovery "
                "chat_id=%s media_id=%s error=%s detail=%s",
                chat_id,
                media_id,
                type(ex).__name__,
                self._sanitize_probe_text(
                    str(ex), getattr(direct_source, "url", None)
                ),
            )
            if config.YOUTUBE_DIRECT_STREAM_ONLY and not bool(
                getattr(media, "video", False)
            ):
                tpl = await db.get_custom_text_for_chat(
                    chat_id, "error_no_file", language["error_no_file"]
                )
                await utils.edit_formatted(
                    message,
                    tpl,
                    config.SUPPORT_CHAT,
                    template_key="error_no_file",
                )
                startup_gate.end(chat_id)
                await self.play_next(chat_id)
                return
            try:
                if await self._try_direct_local_failover(
                    chat_id, startup_failure=True
                ):
                    return
            except Exception as fallback_ex:
                logger.warning(
                    "Detached direct fallback failed chat_id=%s media_id=%s error=%s",
                    chat_id,
                    media_id,
                    type(fallback_ex).__name__,
                )
            direct_watchdog.disarm(chat_id)
            await self.play_next(chat_id)
            return

        play_state["status"] = "attached"
        startup_gate.mark_direct_attached(chat_id)
        attached_event.set()
        if trace:
            trace.mark("stream_attached")
        self._log_direct_startup_event(
            "pytgcalls_stream_attached",
            chat_id=chat_id,
            media_id=media_id,
            evidence="client_play_returned_after_set_stream_sources",
            status="attached",
        )
        self._log_direct_playback_diag(
            "youtube_direct_stream_attached",
            chat_id=chat_id,
            media=media,
            client=client,
            source=direct_source,
            stream=direct_stream,
            direct_stream_resolved=True,
            direct_audio_source_created=True,
            direct_player_start="attached",
            pytgcalls_stream_started=True,
            telegram_audio_packets_sending=False,
            vc_audio_audible_gate_ok=False,
            local_download_started=False,
            playback_source=playback_source,
            ffmpeg_input_open=(
                "raw_no_probe"
                if playback_source == "raw_direct"
                else "pytgcalls_check_stream"
            ),
            reason="pytgcalls_play_returned",
        )
        self._schedule_direct_startup_proof(chat_id, media)

    def _cancel_startup_proof(self, chat_id: int) -> None:
        task = self._startup_proof_tasks.pop(chat_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _schedule_direct_startup_proof(self, chat_id: int, media) -> None:
        self._cancel_startup_proof(chat_id)
        if self._shutting_down:
            return
        media_id = str(getattr(media, "id", "") or "")

        async def _monitor() -> None:
            result = await startup_gate.monitor_direct_proof(chat_id)
            if result.ok or not result.fatal:
                return
            current = queue.get_current(chat_id)
            if str(getattr(current, "id", "") or "") != media_id:
                return
            logger.info(
                "Async direct startup proof requested failover chat_id=%s "
                "media_id=%s reason=%s",
                chat_id,
                media_id,
                result.reason,
            )
            try:
                if await self._try_direct_local_failover(chat_id):
                    return
            except Exception as ex:
                logger.warning(
                    "Async direct proof failover failed chat_id=%s error=%s",
                    chat_id,
                    ex,
                )
            direct_watchdog.disarm(chat_id)
            await self.play_next(chat_id)

        task = asyncio.create_task(
            _monitor(),
            name=f"direct-startup-proof:{chat_id}:{media_id}",
        )
        self._startup_proof_tasks[chat_id] = task
        self._owned_tasks.add(task)

        def _cleanup(done: asyncio.Task) -> None:
            if self._startup_proof_tasks.get(chat_id) is done:
                self._startup_proof_tasks.pop(chat_id, None)
            self._owned_tasks.discard(done)

        task.add_done_callback(_cleanup)

    def _can_play_without_local_file(self, media) -> bool:
        """True when play_media can start via direct/remote URL (no local file yet)."""
        if not media:
            return False
        if getattr(media, "file_path", None) or getattr(media, "local_path", None):
            return True
        src = (getattr(media, "source", None) or "").strip().lower()
        mid = str(getattr(media, "id", "") or "")
        url = (getattr(media, "stream_url", None) or getattr(media, "url", None) or "")
        if src in {
            "tiktok_remote",
            "facebook_remote",
            "telegram_remote",
            "soundcloud",
            "soundcloud_remote",
            "cdn",
        }:
            return bool(url) or bool(mid)
        # YouTube 11-char id → direct stream path inside play_media
        if bool(getattr(config, "YOUTUBE_DIRECT_STREAM", True)) and len(mid) == 11:
            return True
        if str(url).startswith(("http://", "https://")):
            return True
        return False

    def _mark_playback_started(self, chat_id: int, media) -> None:
        """Record active media so residual StreamEnded from previous track is ignored."""
        self._stopped_chats.discard(chat_id)
        mid = str(getattr(media, "id", "") or "")
        self._active_media_id[chat_id] = mid
        # Ignore leftover end events from the previous track for a short window.
        self._stream_switch_until[chat_id] = time.time() + 3.0
        if mid and self._ending_media_id.get(chat_id) == mid:
            self._ending_media_id.pop(chat_id, None)

    def _play_next_lock_held(self, chat_id: int) -> bool:
        lock = self._play_next_locks.get(chat_id)
        return bool(lock and lock.locked())

    @staticmethod
    @staticmethod
    def _leave_already_complete(ex: BaseException) -> bool:
        name = type(ex).__name__.casefold()
        message = str(ex or "").casefold()
        return (
            name in {"connectionnotfound", "noactivegroupcall", "groupcallnotfound"}
            or "no active group call" in message
            or "not in a call" in message
            or "already left" in message
            or "connection not found" in message
            or "group call not found" in message
            or "call not found" in message
        )

    @staticmethod
    def _leave_is_terminal_gone(ex: BaseException) -> bool:
        """The VC is definitively gone — treat as a successful terminal outcome."""
        if TgCall._leave_already_complete(ex):
            return True
        name = type(ex).__name__.casefold()
        message = str(ex or "").casefold()
        return (
            name in {"groupcallforbidden"}
            or "group call has already ended" in message
            or "call has already ended" in message
            or "call already ended" in message
        )

    @staticmethod
    def _leave_error_is_transient(ex: BaseException) -> bool:
        if isinstance(
            ex,
            (
                asyncio.TimeoutError,
                TimeoutError,
                OSError,
                TelegramServerError,
                SignalingError,
                ConnectionError,
            ),
        ):
            return True
        message = str(ex or "").casefold()
        return any(
            marker in message
            for marker in (
                "timeout",
                "timed out",
                "temporarily",
                "connection reset",
                "connection aborted",
                "server error",
            )
        )

    async def _verify_chat_still_playable(
        self, chat_id: int, request_message_id: int = 0
    ) -> bool:
        """Return False when the VC is gone, chat was stopped, or a newer
        request superseded this one — so fallback/download work can abort
        without leaving a stale queue / current-track state behind.
        """
        if chat_id in self._stopped_chats:
            return False
        try:
            vc_alive = await self.has_active_group_call(chat_id)
        except Exception:
            vc_alive = False
        if not vc_alive:
            return False
        if request_message_id:
            from AnonX_3.helpers._play import _play_request_is_live
            if not _play_request_is_live(request_message_id):
                return False
        return True

    def _force_cleanup_chat(self, chat_id: int) -> None:
        """Idempotent per-chat state reset for ALL playback termination paths.

        Covers every piece of in-memory state that can keep a chat session
        stuck when the real Telegram VC has ended but the bot still holds
        stale current-track, queue, admission, race, fallback, watchdog,
        autoplay, stream-end, or lock state.
        """
        try:
            queue.clear(chat_id)
        except Exception:
            pass
        try:
            self.prefetch_manager.cancel(chat_id)
        except Exception:
            pass
        try:
            self._cancel_post_start_tasks(chat_id)
        except Exception:
            pass
        try:
            self._cancel_startup_proof(chat_id)
        except Exception:
            pass
        try:
            startup_gate.end(chat_id)
        except Exception:
            pass
        try:
            direct_watchdog.disarm(chat_id)
        except Exception:
            pass
        try:
            self.stream_profile.clear(chat_id)
        except Exception:
            pass
        # Playback depth / lock guard
        self._play_next_depth.pop(chat_id, None)
        for lock_dict in (
            self._initial_playback_locks,
            self._play_next_locks,
        ):
            try:
                lock_dict.pop(chat_id, None)
            except Exception:
                pass
        # Race and fallback state
        self._stream_end_at.pop(chat_id, None)
        self._stream_switch_until.pop(chat_id, None)
        self._active_media_id.pop(chat_id, None)
        self._ending_media_id.pop(chat_id, None)
        for key in tuple(self._vc_binding_locks):
            if key[1] == int(chat_id):
                self._vc_binding_locks.pop(key, None)
        # Autoplay memory
        self.autoplay_recent_ids.pop(chat_id, None)
        self.autoplay_recent_titles.pop(chat_id, None)
        self.autoplay_recent_artists.pop(chat_id, None)
        self.autoplay_artist_streak.pop(chat_id, None)
        self.autoplay_last_artist.pop(chat_id, None)
        # VC mute tasks
        try:
            for task in list(self._vc_unmute_tasks):
                if not task.done():
                    try:
                        task.cancel()
                    except Exception:
                        pass
        except Exception:
            pass

    async def stop(self, chat_id: int) -> None:
        """Full per-chat stop: local teardown + optional VC leave.

        ``_stopped_chats`` suppresses only the duplicate *Telegram RPC*
        (``leave_call``).  It must **never** skip local cleanup — a later
        failed request may have placed fresh queue / admission / fallback
        state in the same chat.
        """
        lock = self._stop_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            # ── LOCAL CLEANUP (always runs, even on duplicate stop) ──
            try:
                await self._delete_now_playing(chat_id)
            except Exception as ex:
                logger.debug(
                    "now-playing cleanup skipped chat_id=%s error=%s",
                    chat_id, type(ex).__name__,
                )
            try:
                await db.remove_call(chat_id)
            except Exception as ex:
                logger.warning(
                    "active-call cleanup failed chat_id=%s error=%s",
                    chat_id, type(ex).__name__,
                )
            try:
                await db.set_loop(chat_id, 0)
            except Exception as ex:
                logger.warning(
                    "loop cleanup failed chat_id=%s error=%s",
                    chat_id, type(ex).__name__,
                )
            try:
                resource_manager.unregister_stream(chat_id)
            except Exception as ex:
                logger.debug(
                    "resource cleanup skipped chat_id=%s error=%s",
                    chat_id, type(ex).__name__,
                )
            external_session = self._direct_external_audio_sessions.get(int(chat_id))
            if external_session is not None:
                try:
                    await self._close_direct_external_audio(external_session)
                except Exception as ex:
                    logger.debug(
                        "external capture cleanup skipped chat_id=%s error=%s",
                        chat_id, type(ex).__name__,
                    )
            # Unified deep cleanup: queue, prefetch, watchdog, gate,
            # autoplay, stream-end, locks, active/id state.
            self._force_cleanup_chat(chat_id)

            # ── VC LEAVE (skip RPC only when already succeeded) ──
            if chat_id in self._stopped_chats:
                logger.info(
                    "playback stop chat_id=%s leave_call=duplicate_skipped "
                    "(local_cleanup=done)",
                    chat_id,
                )
                return

            try:
                client = await db.get_assistant(chat_id)
            except Exception as ex:
                logger.warning(
                    "playback stop chat_id=%s leave_call=client_unavailable error=%s",
                    chat_id, type(ex).__name__,
                )
                return

            outcome = "ok"
            last_error: BaseException | None = None
            for attempt in range(1, 4):
                try:
                    await client.leave_call(chat_id, close=False)
                    break
                except Exception as ex:
                    if self._leave_already_complete(ex):
                        outcome = "already_left"
                        break
                    # NoActiveGroupCall / GroupcallForbidden are terminal
                    # success states — the VC is gone; our job is done.
                    if self._leave_is_terminal_gone(ex):
                        outcome = "vc_gone"
                        break
                    last_error = ex
                    if not self._leave_error_is_transient(ex) or attempt >= 3:
                        outcome = "failed"
                        break
                    logger.debug(
                        "leave_call transient retry %s/3 chat_id=%s error=%s",
                        attempt, chat_id, type(ex).__name__,
                    )
                    await asyncio.sleep(0.2 * attempt)

            # Only a successful/already-gone leave suppresses future RPCs.
            # A failed leave keeps the chat eligible for retry on next stop.
            if outcome != "failed":
                self._stopped_chats.add(chat_id)
            if outcome == "failed":
                logger.warning(
                    "playback stop chat_id=%s leave_call=failed error=%s detail=%s",
                    chat_id,
                    type(last_error).__name__ if last_error else "unknown",
                    str(last_error or "")[:160],
                )
            else:
                logger.info(
                    "playback stop chat_id=%s leave_call=%s",
                    chat_id, outcome,
                )

    async def play_media(
        self,
        chat_id: int,
        message: Message,
        media: Media | Track,
        seek_time: int = 0,
        trace=None,
        initial_start: bool = False,
        force_now_playing: bool = False,
    ) -> None:
        _skip_remote_sources = {
            "tiktok_remote",
            "facebook_remote",
            "telegram_remote",
            "telegram_local",
            "cdn",
            "cdn_local",
            "cache_local",
            "youtube_local",
            "soundcloud",
            "soundcloud_remote",
            "soundcloud_local",
            "facebook_remote",
        }
        # Assistant and locale lookup are cheap and independent.  The cold
        # YouTube path deliberately uses a cached/default stream profile here;
        # live CPU/RAM/ping sampling is refreshed only after PyTgCalls accepts
        # the stream, so profiling cannot delay first audio.
        client_task = db.get_assistant(chat_id)
        lang_task = lang.get_lang(chat_id)
        client, _lang = await asyncio.gather(client_task, lang_task)
        self._flood_tried.pop(chat_id, None)
        # Cheap pre-flight so a saturated box refuses before burning a resolve
        # or a download. This only *probes* — the binding reservation happens
        # in `_play_with_startup_slot`. Probing also registers the request as
        # stream demand, so a full baseline on a healthy VPS grows capacity
        # here rather than reporting "busy". A chat that already holds a slot
        # (queue auto-next, /skip) always passes.
        if not resource_manager.can_admit_stream(chat_id):
            await self._notify_stream_busy(
                chat_id, message, _lang, where="preflight"
            )
            return
        mid_str = str(getattr(media, "id", "") or "")
        direct_first_youtube = (
            bool(config.YOUTUBE_DIRECT_STREAM)
            and not seek_time
            and not getattr(media, "file_path", None)
            and len(mid_str) == 11
            and getattr(media, "source", None) not in _skip_remote_sources
        )
        # Keep the latency win (VC join + direct resolve in parallel) independent
        # from the experimental raw-shell transport.  The previous code disabled
        # *parallelism itself* after one raw ShellError, which made every later
        # first play serial even though the proven MediaStream path still worked.
        initial_parallel_direct = bool(initial_start and direct_first_youtube)
        raw_cold_path_enabled = bool(getattr(config, "DIRECT_RAW_COLD_PATH", False))
        prevalidated_raw_audio = bool(
            initial_parallel_direct
            and not bool(getattr(media, "video", False))
            and getattr(config, "DIRECT_PREVALIDATED_RAW_AUDIO", True)
        )
        prevalidated_raw_video = bool(
            initial_parallel_direct
            and bool(getattr(media, "video", False))
            and getattr(config, "DIRECT_PREVALIDATED_RAW_VIDEO", True)
        )
        use_raw_cold_path = bool(
            initial_parallel_direct
            and (prevalidated_raw_audio or prevalidated_raw_video or raw_cold_path_enabled)
            and not self._raw_direct_disabled_reason
        )
        if direct_first_youtube and not initial_parallel_direct:
            # The parallel path overlaps VC join with the yt-dlp resolve. When
            # it is skipped the two run back to back, so the reason has to be
            # visible — otherwise the extract time surfaces as a slow join.
            logger.info(
                "direct_parallel_skipped chat_id=%s initial_start=%s "
                "raw_direct_disabled=%s — VC join and direct resolve run serially",
                chat_id,
                int(bool(initial_start)),
                self._raw_direct_disabled_reason or "no",
            )
        if initial_parallel_direct:
            self._schedule_vc_metadata_warm(client, chat_id)
            logger.info(
                "direct_parallel_transport chat_id=%s media_id=%s mode=%s "
                "raw_disabled=%s metadata_prewarm=%s",
                chat_id,
                mid_str,
                ("external_audio" if prevalidated_raw_audio and use_raw_cold_path else
                 "video_early_raw" if prevalidated_raw_video and use_raw_cold_path else
                 "raw_shell" if use_raw_cold_path else "mediastream"),
                self._raw_direct_disabled_reason or "no",
                int(bool(getattr(config, "DIRECT_VC_METADATA_PREWARM", True))),
            )
        profile = (
            self.stream_profile.cached_or_default(chat_id)
            if initial_parallel_direct
            else self.stream_profile.select(chat_id, client)
        )
        if trace:
            try:
                active = len(getattr(db, "active_calls", []) or [])
            except Exception:
                active = -1
            trace.set_meta(
                video=bool(getattr(media, "video", False)),
                chat_load=active,
                tier=profile.tier,
            )
            trace.mark("profile")
        fallback_quality_tier = profile.download_tier
        if media.video and not fallback_quality_tier:
            fallback_quality_tier = "good"
        parallel_tier = fallback_quality_tier if media.video else None
        prejoined_client = None

        if direct_first_youtube:
            logger.info(
                "YouTube direct-first active; foreground cache/local admission "
                "deferred until direct proves, then background cache starts "
                "chat_id=%s media_id=%s video=%s",
                chat_id,
                getattr(media, "id", None),
                int(bool(getattr(media, "video", False))),
            )
        else:
            # Cache admission precedes every non-direct-first remote resolver.
            # YouTube direct-first deliberately defers local/cache until direct
            # is proven unavailable so local files cannot preempt remote VC.
            ready_local = self._pick_ready_local(media, parallel_tier)
            if not ready_local:
                try:
                    entry = await prepare_cache_hit(
                        media, quality_tier=parallel_tier
                    )
                    ready_local = getattr(entry, "local_path", None) if entry else None
                except Exception as ex:
                    logger.debug(
                        "cache-first admission skipped chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
            if ready_local:
                media.file_path = str(ready_local)
                media.local_path = str(ready_local)
                if getattr(media, "source", None) not in {
                    "telegram_local",
                    "tiktok_local",
                    "facebook_local",
                    "soundcloud_local",
                }:
                    media.source = "cache_local"
                logger.info(
                    "playback cache-first hit chat_id=%s media_id=%s path=%s",
                    chat_id,
                    getattr(media, "id", None),
                    ready_local,
                )

        # Recompute after cache admission: values calculated before this point
        # could otherwise still open the remote path for a freshly hydrated hit.
        can_try_direct = (
            bool(config.YOUTUBE_DIRECT_STREAM)
            and not seek_time
            and not media.file_path
            and bool(getattr(media, "id", None))
            and len(str(getattr(media, "id", ""))) == 11
            and getattr(media, "source", None) not in _skip_remote_sources
        )
        can_try_soundcloud = (
            not seek_time
            and not media.file_path
            and not bool(getattr(media, "video", False))
            and getattr(media, "source", None) in {"soundcloud", "soundcloud_remote"}
            and bool(getattr(media, "url", None))
        )
        # Thumbnail rendering can take several seconds.  Hide it under direct
        # resolution / voice join rather than starting it after audio begins.
        if (
            not self._shutting_down
            and config.THUMB_GEN
            and not initial_parallel_direct
            and getattr(media, "_now_playing_thumb_task", None) is None
        ):
            media._now_playing_thumb_task = asyncio.create_task(
                thumb.generate(media, quality_tier=profile.tier),
                name=f"now-playing-thumb:{chat_id}",
            )
            self._track_owned_task(
                media._now_playing_thumb_task,
                self._thumbnail_tasks,
            )

        # TikTok/Facebook/Telegram use the same hybrid contract as YouTube:
        # start the verified local safety-net before direct extraction/play.
        # Their service-level singleflight prevents duplicate downloads when
        # later fallback/CDN paths join the same work.
        external_source = getattr(media, "source", None)
        if not seek_time and not media.file_path:
            try:
                if external_source == "tiktok_remote":
                    await tiktok.start_current_cache(chat_id, media)
                elif external_source == "facebook_remote":
                    await facebook.start_current_cache(chat_id, media)
                elif external_source == "telegram_remote":
                    await tg.start_current_cache(chat_id, media)
                if external_source in {
                    "tiktok_remote",
                    "facebook_remote",
                    "telegram_remote",
                }:
                    logger.info(
                        "Parallel external local started source=%s chat_id=%s "
                        "media_id=%s video=%s",
                        external_source,
                        chat_id,
                        getattr(media, "id", None),
                        int(bool(getattr(media, "video", False))),
                    )
            except Exception as ex:
                logger.warning(
                    "Parallel external cache kickoff failed source=%s "
                    "chat_id=%s media_id=%s: %s",
                    external_source,
                    chat_id,
                    getattr(media, "id", None),
                    ex,
                )

        early_connect_task = None
        early_external_session = None
        early_external_stream = None
        early_reserved_slot = None
        command_preconnect = getattr(media, "_initial_direct_preconnect", None)
        early_overlap_enabled = bool(
            can_try_direct
            and initial_parallel_direct
            and use_raw_cold_path
            and (
                (not bool(getattr(media, "video", False)) and bool(getattr(config, "DIRECT_EXTERNAL_PREBUFFER_AUDIO", True)))
                or (bool(getattr(media, "video", False)) and bool(getattr(config, "DIRECT_VIDEO_VC_RESOLVER_OVERLAP", True)))
            )
        )
        if early_overlap_enabled and isinstance(command_preconnect, dict):
            preconnect_task = command_preconnect.get("task")
            preconnect_session = command_preconnect.get("session")
            preconnect_valid = bool(
                not command_preconnect.get("closed")
                and not command_preconnect.get("adopted")
                and int(command_preconnect.get("chat_id") or 0) == int(chat_id)
                and bool(command_preconnect.get("video"))
                == bool(getattr(media, "video", False))
                and command_preconnect.get("client") is client
                and preconnect_task is not None
                and preconnect_session is not None
                and not preconnect_session.get("closed")
            )
            if preconnect_valid and preconnect_task.done():
                try:
                    preconnect_task.result()
                except BaseException:
                    preconnect_valid = False
            if preconnect_valid:
                early_connect_task = preconnect_task
                early_external_session = preconnect_session
                early_external_stream = command_preconnect.get("stream")
                early_reserved_slot = command_preconnect.get("slot")
                early_external_session["media_id"] = mid_str
                early_external_session["trace"] = trace
                command_preconnect["adopted"] = True
                setattr(media, "_initial_direct_preconnect", None)
                logger.info(
                    "direct_command_preconnect_adopted chat_id=%s request_id=%s "
                    "media_id=%s video=%s connected=%s reconnect=0",
                    chat_id,
                    command_preconnect.get("request_id", 0),
                    mid_str,
                    int(bool(getattr(media, "video", False))),
                    int(bool(early_external_session["connected"].is_set())),
                )
            else:
                await self.cancel_initial_direct_preconnect(command_preconnect)

        if early_overlap_enabled and early_connect_task is None:
            try:
                early_reserved_slot = resource_manager.reserve_stream(chat_id)
                if not early_reserved_slot.admitted:
                    raise StreamCapacityError(chat_id)
                early_external_stream, early_external_session = (
                    await self._prepare_initial_direct_external_stream(
                        profile,
                        media,
                        chat_id=chat_id,
                        placeholder_only=False,
                    )
                )
                early_external_session["trace"] = trace
                if trace:
                    trace.set_meta(mode="new-direct-early-connect")
                    trace.mark("vc_join_start")
                self._log_direct_startup_event(
                    "pytgcalls_play_task_scheduled",
                    chat_id=chat_id,
                    media_id=mid_str,
                    evidence="resolver_overlapped_external_connect",
                    status="scheduled_early",
                )
                early_connect_task = asyncio.create_task(
                    self._play_with_startup_slot(
                        client,
                        chat_id=chat_id,
                        stream=early_external_stream,
                        unmute_mode="required",
                        reserved_slot=early_reserved_slot,
                        startup_media_id=mid_str,
                        external_audio_session=early_external_session,
                    ),
                    name=f"direct-early-connect:{chat_id}:{mid_str}",
                )
                self._track_owned_task(
                    early_connect_task, self._direct_external_audio_tasks
                )
                logger.info(
                    "direct_vc_resolver_overlap_started chat_id=%s media_id=%s "
                    "external=1 video_audio_lead=%s metadata_warm=1",
                    chat_id, mid_str, int(bool(getattr(media, "video", False))),
                )
            except StreamCapacityError:
                await self._notify_stream_busy(
                    chat_id, message, _lang, where="initial_direct_early_connect"
                )
                return
            except Exception as ex:
                logger.info(
                    "direct_vc_resolver_overlap_fallback chat_id=%s media_id=%s reason=%s",
                    chat_id, mid_str, type(ex).__name__,
                )
                if early_reserved_slot is not None:
                    early_reserved_slot.release()
                early_reserved_slot = None
                early_external_session = None
                early_external_stream = None
                early_connect_task = None

        if can_try_direct:
            remote_url = None
            direct_source = None
            try:
                if initial_parallel_direct:
                    # Late-join policy: resolve/build the audio source before the
                    # assistant enters the voice chat.  The actual client.play()
                    # call below performs VC join + stream attachment together,
                    # so users do not see an empty assistant sitting in VC while
                    # YouTube/POT resolution is still running.
                    if trace:
                        if not early_overlap_enabled:
                            trace.set_meta(mode="new-direct-late-join")
                        trace.mark("direct_resolve_start")
                    direct_source = await yt.resolve_direct_stream_source(
                        str(getattr(media, "id", "")),
                        video=bool(getattr(media, "video", False)),
                        quality_tier=parallel_tier,
                        prefer_remote=True,
                    )
                else:
                    if trace:
                        trace.mark("resolve")
                    direct_source = await yt.resolve_direct_stream_source(
                        str(getattr(media, "id", "")),
                        video=bool(getattr(media, "video", False)),
                        quality_tier=parallel_tier,
                        prefer_remote=True,
                    )
                    if trace:
                        # Distinct from the later `vc_join` mark, which lands
                        # only after this resolve returns. Without this the
                        # extract cost is invisible and reads as slow VC join.
                        trace.mark("direct_resolved")
                remote_url = getattr(direct_source, "url", None)
                local_hint = getattr(direct_source, "local_path", None)
                if local_hint:
                    media.local_path = local_hint
            except AssistantUnmuteError:
                await self._notify_join_status(
                    message,
                    _lang,
                    _lang.get(
                        "play_unmute_failed",
                        "Joined the voice chat, but the assistant could not be "
                        "unmuted. Playback was not started.",
                    ),
                )
                return
            except StreamCapacityError:
                await self._notify_stream_busy(
                    chat_id, message, _lang, where="initial_direct_prejoin"
                )
                return
            except InitialVoiceJoinError:
                await self._notify_join_status(
                    message,
                    _lang,
                    _lang.get(
                        "play_join_failed",
                        "Could not join the voice chat. Skipping to the next track.",
                    ),
                )
                return
            except Exception as ex:
                # In late-join mode the VC has not been touched yet when
                # direct resolution fails, so this is a resolver miss rather
                # than a join failure. The existing local/download recovery
                # path may proceed without cleaning up an empty prejoin.
                logger.warning(
                    "Direct stream resolve failed for chat_id=%s media_id=%s: %s",
                    chat_id,
                    getattr(media, "id", None),
                    ex,
                )
                remote_url = None

            if remote_url:
                # Cold initial playback performs only the non-network SSRF/URL
                # boundary here. HTTP and ffmpeg probes are post-attach
                # diagnostics; established/legacy paths retain their behavior.
                if initial_parallel_direct:
                    probe_ok, probe_reason = validate_direct_url(remote_url)
                else:
                    probe_ok, probe_reason = await probe_direct_url(remote_url)
                if not probe_ok:
                    self._log_direct_playback_diag(
                        "youtube_direct_probe_failed",
                        chat_id=chat_id,
                        media=media,
                        client=client,
                        source=direct_source,
                        direct_stream_resolved=True,
                        direct_audio_source_created=0,
                        direct_player_start="not_attempted",
                        local_download_started=0,
                        reason=probe_reason,
                    )
                    logger.info(
                        "Direct URL probe hard-fail chat_id=%s media_id=%s reason=%s "
                        "(falling back to local acquisition)",
                        chat_id,
                        getattr(media, "id", None),
                        probe_reason,
                    )
                    remote_url = None
                elif str(probe_reason).startswith("soft_"):
                    logger.info(
                        "Direct URL probe soft-pass chat_id=%s media_id=%s reason=%s "
                        "video=%s — try direct immediately",
                        chat_id,
                        getattr(media, "id", None),
                        probe_reason,
                        int(bool(getattr(media, "video", False))),
                    )
                else:
                    logger.info(
                        "Direct start chat_id=%s media_id=%s probe=%s video=%s "
                        "(no current local download)",
                        chat_id,
                        getattr(media, "id", None),
                        probe_reason,
                        int(bool(getattr(media, "video", False))),
                    )

            if remote_url:
                # Default (DIRECT_AUDIO_PROBE off): skip the redundant local
                # ffmpeg pre-open — PyTgCalls opens the same URL when it starts
                # the stream, so this probe only adds pre-audio latency. The
                # startup gate + deferred local fallback recover a dead URL.
                if initial_parallel_direct:
                    ffmpeg_ok, ffmpeg_reason = True, "probe_deferred"
                elif getattr(config, "DIRECT_AUDIO_PROBE", False):
                    ffmpeg_ok, ffmpeg_reason = await self._probe_direct_audio_open(
                        direct_source
                    )
                else:
                    ffmpeg_ok, ffmpeg_reason = True, "probe_skipped"
                if not ffmpeg_ok:
                    self._log_direct_playback_diag(
                        "youtube_direct_ffmpeg_probe_failed",
                        chat_id=chat_id,
                        media=media,
                        client=client,
                        source=direct_source,
                        direct_stream_resolved=True,
                        direct_audio_source_created=0,
                        direct_player_start="not_attempted",
                        local_download_started=0,
                        ffmpeg_input_open=ffmpeg_reason,
                        reason=ffmpeg_reason,
                    )
                    remote_url = None
                else:
                    direct_event_path = None
                    external_audio_session = early_external_session
                    direct_playback_source = (
                        "raw_direct" if use_raw_cold_path else "mediastream_direct"
                    )
                    if use_raw_cold_path:
                        is_video_direct = bool(getattr(media, "video", False))
                        if (
                            not is_video_direct
                            and early_external_session is not None
                            and early_external_stream is not None
                        ):
                            try:
                                await self._start_initial_direct_external_decoder(
                                    direct_source, early_external_session
                                )
                                direct_stream = early_external_stream
                                external_audio_session = early_external_session
                                direct_playback_source = "external_early_connect"
                            except Exception as ex:
                                logger.info(
                                    "direct_external_decoder_start_fallback chat_id=%s "
                                    "media_id=%s reason=%s",
                                    chat_id, getattr(media, "id", None), type(ex).__name__,
                                )
                                if early_connect_task is not None:
                                    if not early_connect_task.done():
                                        early_connect_task.cancel()
                                    await asyncio.gather(
                                        early_connect_task, return_exceptions=True
                                    )
                                await self._close_direct_external_audio(
                                    early_external_session
                                )
                                try:
                                    await client.leave_call(chat_id, close=False)
                                except Exception:
                                    pass
                                if early_reserved_slot is not None:
                                    early_reserved_slot.release()
                                early_reserved_slot = None
                                early_external_stream = None
                                early_connect_task = None
                                early_external_session = None
                                external_audio_session = None
                                try:
                                    direct_stream, direct_event_path = (
                                        self._build_initial_direct_raw_stream(
                                            direct_source, profile, media, chat_id=chat_id
                                        )
                                    )
                                    direct_playback_source = "raw_direct"
                                except Exception:
                                    use_raw_cold_path = False
                                    direct_playback_source = "mediastream_direct"
                                    direct_stream = self._build_direct_media_stream(
                                        direct_source, profile, media
                                    )
                        elif not is_video_direct:
                            external_enabled = bool(
                                initial_parallel_direct
                                and getattr(config, "DIRECT_EXTERNAL_PREBUFFER_AUDIO", True)
                            )
                            if external_enabled:
                                try:
                                    direct_stream, external_audio_session = (
                                        await self._build_initial_direct_external_stream(
                                            direct_source, profile, media, chat_id=chat_id
                                        )
                                    )
                                    direct_playback_source = "external_prebuffer"
                                except Exception as ex:
                                    logger.info(
                                        "direct_external_prebuffer_fallback chat_id=%s media_id=%s "
                                        "reason=%s",
                                        chat_id, getattr(media, "id", None), type(ex).__name__,
                                    )
                                    external_audio_session = None
                            if external_audio_session is None:
                                try:
                                    direct_stream, direct_event_path = (
                                        self._build_initial_direct_raw_stream(
                                            direct_source,
                                            profile,
                                            media,
                                            chat_id=chat_id,
                                        )
                                    )
                                except Exception as ex:
                                    self._raw_direct_disabled_reason = (
                                        f"launcher_build:{type(ex).__name__}:{str(ex)[:120]}"
                                    )
                                    logger.warning(
                                        "Raw direct launcher build failed before VC join; "
                                        "using MediaStream fallback. chat_id=%s reason=%s",
                                        chat_id, self._raw_direct_disabled_reason,
                                    )
                                    use_raw_cold_path = False
                                    direct_playback_source = "mediastream_direct"
                                    direct_event_path = None
                                    direct_stream = self._build_direct_media_stream(
                                        direct_source, profile, media
                                    )
                        else:
                            # /vplay: feed the resolved audio through the early
                            # EXTERNAL capture first. This produces an audible
                            # packet while the same connected call is prepared
                            # for its raw A/V source swap.
                            if (
                                early_external_session is not None
                                and early_connect_task is not None
                            ):
                                await self._start_initial_direct_external_decoder(
                                    direct_source,
                                    early_external_session,
                                )
                            direct_stream, direct_event_path = (
                                self._build_initial_direct_raw_stream(
                                    direct_source,
                                    profile,
                                    media,
                                    chat_id=chat_id,
                                )
                            )
                            direct_playback_source = (
                                "raw_direct_existing_call"
                                if early_connect_task is not None
                                else "raw_direct"
                            )
                    else:
                        # Stable default: PyTgCalls' supported MediaStream path.
                        # It was already the successful recovery path in production,
                        # and keeping it inside the same parallel startup transaction
                        # avoids the 5s raw-shell failure tax without serializing VC join.
                        direct_stream = self._build_direct_media_stream(
                            direct_source, profile, media
                        )
                    self._log_direct_playback_diag(
                        "youtube_direct_audio_source_created",
                        chat_id=chat_id,
                        media=media,
                        client=client,
                        source=direct_source,
                        stream=direct_stream,
                        direct_stream_resolved=True,
                        direct_audio_source_created=True,
                        direct_player_start="pending",
                        local_download_started=False,
                        playback_source=direct_playback_source,
                        ffmpeg_input_open=ffmpeg_reason,
                        reason=(
                            "external_prebuffer_created"
                            if external_audio_session is not None
                            else "raw_stream_created"
                            if use_raw_cold_path
                            else "stable_mediastream_created"
                        ),
                    )


            # If the speculative VC connect won its race but the resolver or
            # direct-source validation failed, tear the placeholder down before
            # entering local acquisition. This prevents a silent connected call
            # and stream-slot leak on failed cold starts.
            if not remote_url and early_connect_task is not None:
                if not early_connect_task.done():
                    early_connect_task.cancel()
                await asyncio.gather(early_connect_task, return_exceptions=True)
                if early_external_session is not None:
                    await self._close_direct_external_audio(early_external_session)
                try:
                    await client.leave_call(chat_id, close=False)
                except Exception:
                    pass
                if early_reserved_slot is not None:
                    early_reserved_slot.release()
                early_connect_task = None
                early_external_session = None
                early_external_stream = None
                early_reserved_slot = None

            if remote_url:
                if trace:
                    if not initial_parallel_direct:
                        trace.set_meta(mode="new-direct")
                        trace.mark("vc_join")
                if initial_parallel_direct:
                    self._cancel_startup_proof(chat_id)
                startup_gate.begin(
                    chat_id, media, quality_tier=profile.download_tier
                )
                # The gate converts any play exception into `ok=False`, which
                # normally means "fall back to local". A capacity refusal is
                # different: nothing was submitted and no download should be
                # started for it, so surface it explicitly instead of letting
                # it read as a direct-playback failure.
                capacity_refused = False
                # Early-connect owns the exact stream reservation from the
                # moment the EXTERNAL placeholder is submitted. Reuse that
                # reservation for the final source swap instead of allocating a
                # second critical-path slot for the same chat.
                reserved_direct_slot = early_reserved_slot

                async def _direct_play() -> None:
                    nonlocal capacity_refused
                    try:
                        if early_connect_task is not None:
                            # /play: the early task owns connect + real PCM
                            # activation; the decoder was attached as soon as the
                            # winning direct URL resolved. Nothing else should be
                            # submitted to PyTgCalls here.
                            await early_connect_task
                            if not bool(getattr(media, "video", False)):
                                return

                            # Audio is already live on the EXTERNAL microphone.
                            # Keep that exact source running and attach only the
                            # camera in an owned post-start task. Neither RTP nor
                            # truthful audible proof waits for this source swap.
                            async def _attach_vplay_video() -> None:
                                swap_started = time.perf_counter()
                                media_id = str(getattr(media, "id", "") or "")
                                try:
                                    self._log_direct_startup_event(
                                        "vplay_source_swap_before",
                                        chat_id=chat_id,
                                        media_id=media_id,
                                        evidence="external_audio_live_camera_background",
                                        status="calling",
                                    )
                                    await self._play_with_startup_slot(
                                        client,
                                        chat_id=chat_id,
                                        stream=direct_stream,
                                        unmute_mode="skip",
                                        reserved_slot=reserved_direct_slot,
                                        startup_media_id=media_id,
                                    )
                                    swap_ms = int(
                                        (time.perf_counter() - swap_started) * 1000
                                    )
                                    self._log_direct_startup_event(
                                        "vplay_source_swap_after",
                                        chat_id=chat_id,
                                        media_id=media_id,
                                        evidence="camera_attached_audio_continuous",
                                        status="attached",
                                        detail=f"swap_ms={swap_ms};audio_wait_ms=0",
                                    )
                                    logger.info(
                                        "direct_video_background_source_swap chat_id=%s "
                                        "media_id=%s raw=%s swap_ms=%s reconnect=0 "
                                        "audio_wait_ms=0",
                                        chat_id,
                                        media_id,
                                        int(isinstance(direct_stream, types.raw.Stream)),
                                        swap_ms,
                                    )
                                except asyncio.CancelledError:
                                    raise
                                except Exception as ex:
                                    logger.warning(
                                        "direct_video_background_source_swap_failed "
                                        "chat_id=%s media_id=%s error=%s "
                                        "audio_continues=1",
                                        chat_id,
                                        media_id,
                                        type(ex).__name__,
                                    )

                            video_task = asyncio.create_task(
                                _attach_vplay_video(),
                                name=(
                                    f"vplay-video-attach:{chat_id}:"
                                    f"{getattr(media, 'id', '')}"
                                ),
                            )
                            self._track_post_start_task(chat_id, video_task)
                            return

                        if initial_parallel_direct and trace:
                            trace.mark("vc_join_start")
                        await self._play_with_startup_slot(
                            client,
                            chat_id=chat_id,
                            stream=direct_stream,
                            unmute_mode=(
                                "required" if initial_parallel_direct else "background"
                            ),
                            reserved_slot=reserved_direct_slot,
                            startup_media_id=(
                                str(getattr(media, "id", "") or "")
                                if initial_parallel_direct
                                else None
                            ),
                            external_audio_session=(
                                external_audio_session if initial_parallel_direct else None
                            ),
                        )
                    except StreamCapacityError:
                        capacity_refused = True
                        raise

                try:
                    if initial_parallel_direct:
                        # Direct source is ready; VC is intentionally still
                        # untouched. Reserve synchronously so capacity failure
                        # remains an immediate command result, then client.play()
                        # performs join + stream attachment in one operation.
                        # let PyTgCalls/ntgcalls attach in an owned task. The stable
                        # MediaStream default uses the supported PyTgCalls path; the
                        # optional raw shell path remains available for experiments.
                        # Real outgoing-packet milestones are observed independently.
                        if reserved_direct_slot is None:
                            reserved_direct_slot = resource_manager.reserve_stream(
                                chat_id
                            )
                        if not reserved_direct_slot.admitted:
                            capacity_refused = True
                            raise StreamCapacityError(chat_id)
                        startup_gate.mark_direct_dispatched(chat_id)
                        media.stream_tier = profile.tier
                        media._cache_quality_tier = parallel_tier
                        media.stream_url = remote_url
                        media.playback_source = direct_playback_source
                        media.source = (
                            getattr(media, "source", None) or "youtube_remote"
                        )
                        direct_watchdog.arm(
                            chat_id,
                            media,
                            source="youtube_remote",
                            local_path=None,
                        )
                        media.time = 1
                        self._mark_playback_started(chat_id, media)
                        attached_event = asyncio.Event()
                        play_state = {"status": "scheduled"}
                        self._log_direct_startup_event(
                            "pytgcalls_play_task_scheduled",
                            chat_id=chat_id,
                            media_id=str(getattr(media, "id", "") or ""),
                            evidence="asyncio_owned_task",
                            status="scheduled",
                        )
                        play_task = asyncio.create_task(
                            self._monitor_initial_direct_play(
                                chat_id=chat_id,
                                message=message,
                                media=media,
                                client=client,
                                direct_source=direct_source,
                                direct_stream=direct_stream,
                                language=_lang,
                                profile=profile,
                                quality_tier=parallel_tier,
                                play_coro=_direct_play(),
                                playback_source=direct_playback_source,
                                attached_event=attached_event,
                                play_state=play_state,
                                trace=trace,
                            ),
                            name=(
                                f"direct-play:{chat_id}:"
                                f"{getattr(media, 'id', '')}"
                            ),
                        )
                        self._track_post_start_task(chat_id, play_task)
                        observer_task = asyncio.create_task(
                            self._observe_initial_direct_media(
                                chat_id=chat_id,
                                message=message,
                                media=media,
                                client=client,
                                direct_source=direct_source,
                                quality_tier=parallel_tier,
                                event_path=str(direct_event_path or ""),
                                attached_event=attached_event,
                                play_state=play_state,
                                trace=trace,
                            ),
                            name=(
                                f"direct-media-observer:{chat_id}:"
                                f"{getattr(media, 'id', '')}"
                            ),
                        )
                        self._track_post_start_task(chat_id, observer_task)
                        try:
                            await db.add_call(chat_id)
                        except BaseException:
                            # A bookkeeping failure must not leave a detached
                            # media submission running while the outer path
                            # falls back.  Cancellation releases the reserved
                            # slot through _play_with_startup_slot.
                            play_task.cancel()
                            observer_task.cancel()
                            await asyncio.gather(
                                play_task,
                                observer_task,
                                return_exceptions=True,
                            )
                            self._cancel_startup_proof(chat_id)
                            if play_state.get("status") == "attached":
                                try:
                                    await client.leave_call(chat_id, close=False)
                                except Exception as cleanup_ex:
                                    logger.debug(
                                        "Direct attach cleanup after call-state "
                                        "failure skipped chat_id=%s error=%s",
                                        chat_id,
                                        type(cleanup_ex).__name__,
                                    )
                            reserved_direct_slot.release()
                            direct_watchdog.disarm(chat_id)
                            startup_gate.end(chat_id)
                            raise
                        self._log_direct_playback_diag(
                            "youtube_direct_playback_dispatched",
                            chat_id=chat_id,
                            media=media,
                            client=client,
                            source=direct_source,
                            stream=direct_stream,
                            direct_stream_resolved=True,
                            direct_audio_source_created=True,
                            direct_player_start="task_scheduled",
                            pytgcalls_stream_started=False,
                            telegram_audio_packets_sending=False,
                            vc_audio_audible_gate_ok=False,
                            local_download_started=False,
                            playback_source=direct_playback_source,
                            ffmpeg_input_open=(
                                "raw_no_probe"
                                if use_raw_cold_path
                                else "pytgcalls_check_stream"
                            ),
                            reason=(
                                "external_prebuffer_play_task_dispatched"
                                if external_audio_session is not None
                                else "raw_play_task_dispatched"
                                if use_raw_cold_path
                                else "stable_mediastream_play_task_dispatched"
                            ),
                        )
                        if trace:
                            trace.mark("play_task_scheduled")
                        return

                    # Resolved URL alone is not success — gate on VC play start.
                    gate = await startup_gate.confirm_direct_start(
                        chat_id,
                        play_coro=_direct_play(),
                        on_started=lambda: trace.mark("audible") if trace else None,
                    )
                    if capacity_refused:
                        raise StreamCapacityError(chat_id)
                    if not gate.ok:
                        raise RuntimeError(gate.reason or "direct_startup_gate_failed")
                    if trace:
                        trace.mark("voice_started")
                    media.stream_tier = profile.tier
                    media._cache_quality_tier = parallel_tier
                    media.stream_url = remote_url
                    media.playback_source = direct_playback_source
                    media.source = getattr(media, "source", None) or "youtube_remote"
                    # Populate title/duration from the direct stream extractor
                    # metadata cache when the search phase returned a stub Track.
                    if (
                        not getattr(media, "duration_sec", None)
                        and str(getattr(media, "title", "")) in ("", "YouTube Video", "Unknown")
                    ):
                        resolved_meta = yt._track_from_direct_metadata(
                            str(getattr(media, "id", "")),
                            getattr(media, "message_id", 0) or 0,
                            video=bool(getattr(media, "video", False)),
                        )
                        if resolved_meta is not None:
                            media.title = getattr(resolved_meta, "title", None) or media.title
                            media.duration = getattr(resolved_meta, "duration", None) or media.duration
                            media.duration_sec = getattr(resolved_meta, "duration_sec", 0) or getattr(media, "duration_sec", 0)
                            media.channel_name = getattr(resolved_meta, "channel_name", None) or getattr(media, "channel_name", "")
                            media.thumbnail = getattr(resolved_meta, "thumbnail", None) or getattr(media, "thumbnail", "")
                    direct_watchdog.arm(
                        chat_id,
                        media,
                        source="youtube_remote",
                        local_path=None,
                    )
                    # Audible gate already passed. Give queued-next exact 140
                    # first priority (via _prefetch_next below); only spend the
                    # slow promotion on the current track when there is no next
                    # item to prepare. This keeps short queues promotion-ready.
                    next_waiting = queue.get_next(chat_id, check=True)
                    if not bool(getattr(media, "video", False)) and next_waiting is None:
                        try:
                            yt.warm_audio140_source(str(getattr(media, "id", "") or ""))
                        except Exception as ex:
                            logger.debug(
                                "current background 140 kickoff skipped chat_id=%s media_id=%s: %s",
                                chat_id, getattr(media, "id", None), ex,
                            )
                    await self._start_youtube_direct_background_cache(
                        chat_id,
                        media,
                        parallel_tier,
                    )
                    await self._prefetch_next(chat_id)
                    media.time = 1
                    await db.add_call(chat_id)
                    self._mark_playback_started(chat_id, media)
                    self._log_direct_playback_diag(
                        "youtube_direct_playback_started",
                        chat_id=chat_id,
                        media=media,
                        client=client,
                        source=direct_source,
                        stream=direct_stream,
                        direct_stream_resolved=True,
                        direct_audio_source_created=True,
                        direct_player_start="ok",
                        pytgcalls_stream_started=True,
                        telegram_audio_packets_sending=True,
                        vc_audio_audible_gate_ok=True,
                        local_download_started=False,
                        playback_source=direct_playback_source,
                        ffmpeg_input_open="ok",
                        reason="observable_gate_ok",
                    )
                    if trace:
                        trace.mark("thumb_queued")
                    await update_now_playing(chat_id, message, media)
                    if trace:
                        trace.mark("np_updated")
                        trace.finish("ready")
                    return
                except StreamCapacityError:
                    if locals().get("external_audio_session") is not None:
                        await self._close_direct_external_audio(external_audio_session)
                    # Nothing was submitted to PyTgCalls, so this is not a
                    # startup failure: do not race to local and do not start a
                    # download for a stream that cannot be admitted.
                    try:
                        startup_gate.signal_fatal(chat_id, "stream_capacity_reached")
                    except Exception:
                        pass
                    await self._notify_stream_busy(
                        chat_id, message, _lang, where="direct_startup"
                    )
                    return
                except Exception as ex:
                    if locals().get("external_audio_session") is not None:
                        await self._close_direct_external_audio(external_audio_session)
                    try:
                        startup_gate.signal_fatal(chat_id, str(ex))
                    except Exception:
                        pass
                    self._log_direct_playback_diag(
                        "youtube_direct_startup_failed",
                        chat_id=chat_id,
                        media=media,
                        client=client,
                        source=direct_source,
                        stream=locals().get("direct_stream"),
                        direct_stream_resolved=True,
                        direct_audio_source_created=True,
                        direct_player_start=(
                            "no_audio_source_found"
                            if self._no_audio_source_error(ex)
                            else "failed"
                        ),
                        pytgcalls_stream_started=False,
                        telegram_audio_packets_sending=False,
                        vc_audio_audible_gate_ok=False,
                        local_download_started=False,
                        playback_source="",
                        ffmpeg_input_open="precheck_ok",
                        reason=type(ex).__name__,
                    )
                    logger.info(
                        "Direct stream startup failed; race→local "
                        "chat_id=%s media_id=%s video=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        int(bool(getattr(media, "video", False))),
                        ex,
                    )
                    local_now = self._pick_ready_local(media, parallel_tier)
                    race = decide_race(
                        direct_ok=False,
                        local_path=local_now,
                        local_pending=True,
                        video=bool(getattr(media, "video", False)),
                    )
                    if race.decision == RaceDecision.SWITCH_LOCAL and race.play_path:
                        media.file_path = race.play_path
                        media.local_path = race.play_path
                        media.source = "youtube_local"
                        logger.info(
                            "Race SWITCH_LOCAL chat_id=%s path=%s",
                            chat_id,
                            race.play_path,
                        )
                    elif race.decision == RaceDecision.WAIT_LOCAL:
                        # The consolidated fallback below performs the one
                        # bounded join.  Waiting here as well used to charge
                        # the local-download timeout twice.
                        logger.info(
                            "Race WAIT_LOCAL deferred to consolidated fallback "
                            "chat_id=%s media_id=%s",
                            chat_id,
                            getattr(media, "id", None),
                        )
                    if media.video:
                        fallback_quality_tier = "poor"
                    if config.YOUTUBE_DIRECT_STREAM_ONLY and not media.video:
                        tpl = await db.get_custom_text_for_chat(
                            chat_id, "error_no_file", _lang["error_no_file"]
                        )
                        await utils.edit_formatted(
                            message,
                            tpl,
                            config.SUPPORT_CHAT,
                            template_key="error_no_file",
                        )
                        startup_gate.end(chat_id)
                        return await self.play_next(chat_id)
                finally:
                    # Keep refcount only while active remote direct; release if
                    # falling through to local play path below.
                    url = str(getattr(media, "stream_url", "") or "")
                    if not url.startswith(("http://", "https://")):
                        startup_gate.end(chat_id)
            else:
                logger.info(
                    "Direct stream unavailable; race→local download "
                    "chat_id=%s media_id=%s video=%s",
                    chat_id,
                    getattr(media, "id", None),
                    int(bool(getattr(media, "video", False))),
                )
                local_now = self._pick_ready_local(media, parallel_tier)
                race = decide_race(
                    direct_ok=None,
                    local_path=local_now,
                    local_pending=True,
                    video=bool(getattr(media, "video", False)),
                )
                if race.decision == RaceDecision.SWITCH_LOCAL and race.play_path:
                    media.file_path = race.play_path
                    media.local_path = race.play_path
                    media.source = "youtube_local"
                elif race.decision == RaceDecision.WAIT_LOCAL:
                    logger.info(
                        "Unavailable direct WAIT_LOCAL deferred to consolidated "
                        "fallback chat_id=%s media_id=%s",
                        chat_id,
                        getattr(media, "id", None),
                    )

        # SoundCloud fallback track: try direct stream then local download
        if can_try_soundcloud:
            try:
                from AnonX_3.core.resolver.soundcloud import soundcloud

                remote_url, _ = await soundcloud.resolve_direct_stream(
                    str(getattr(media, "url", ""))
                )
                if remote_url:
                    direct_stream = types.MediaStream(
                        media_path=remote_url,
                        audio_parameters=profile.audio_parameters,
                        video_parameters=profile.video_parameters,
                        audio_flags=types.MediaStream.Flags.REQUIRED,
                        video_flags=(
                            types.MediaStream.Flags.AUTO_DETECT
                            if media.video
                            else types.MediaStream.Flags.IGNORE
                        ),
                    )
                    try:
                        await self._play_with_startup_slot(
                            client, chat_id=chat_id, stream=direct_stream
                        )
                        media.stream_tier = profile.tier
                        media.stream_url = remote_url
                        media.source = "soundcloud_remote"
                        await self._prefetch_next(chat_id)
                        media.time = 1
                        await db.add_call(chat_id)
                        self._mark_playback_started(chat_id, media)
                        await update_now_playing(chat_id, message, media)
                        if trace:
                            trace.mark("voice_started")
                            trace.finish("ready")
                        return
                    except StreamCapacityError:
                        # Refused before submission — do not fall through to a
                        # SoundCloud download for a stream we cannot admit.
                        await self._notify_stream_busy(
                            chat_id, message, _lang, where="soundcloud_direct"
                        )
                        return
                    except Exception as ex:
                        logger.info(
                            "SoundCloud direct failed; downloading chat_id=%s: %s",
                            chat_id,
                            ex,
                        )
                local_sc = await soundcloud.download(
                    str(getattr(media, "url", "")),
                    media_id=str(getattr(media, "id", "sc")),
                    video=bool(getattr(media, "video", False)),
                )
                if local_sc:
                    media.file_path = local_sc
                    media.local_path = local_sc
                    media.source = "soundcloud_local"
            except Exception as ex:
                logger.warning(
                    "SoundCloud playback path failed chat_id=%s: %s", chat_id, ex
                )

        can_try_tiktok_direct = (
            bool(config.TIKTOK_DIRECT_STREAM)
            and not seek_time
            and not media.file_path
            and getattr(media, "source", None) == "tiktok_remote"
            and bool(getattr(media, "url", None))
        )
        silent_direct_fallback = bool(
            can_try_direct or can_try_tiktok_direct or can_try_soundcloud
        )

        if can_try_tiktok_direct:
            if config.TIKTOK_DIRECT_CACHE_BG and allow_current_cache(profile.tier):
                try:
                    await tiktok.start_current_cache(chat_id, media)
                except Exception as ex:
                    logger.warning(
                        "TikTok parallel cache kickoff failed for chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
            remote_url = None
            try:
                remote_url, local_path = await tiktok.resolve_direct_stream(
                    url=str(getattr(media, "url", "")),
                    media_id=str(getattr(media, "id", "")),
                    video=bool(getattr(media, "video", False)),
                )
                if local_path:
                    media.local_path = local_path
            except Exception as ex:
                logger.warning(
                    "TikTok direct stream resolve failed for chat_id=%s media_id=%s: %s",
                    chat_id,
                    getattr(media, "id", None),
                    ex,
                )
                remote_url = None

            if remote_url:
                direct_stream = types.MediaStream(
                    media_path=remote_url,
                    audio_parameters=profile.audio_parameters,
                    video_parameters=profile.video_parameters,
                    audio_flags=types.MediaStream.Flags.REQUIRED,
                    video_flags=(
                        types.MediaStream.Flags.AUTO_DETECT
                        if media.video
                        else types.MediaStream.Flags.IGNORE
                    ),
                )
                try:
                    await self._play_with_startup_slot(
                        client, chat_id=chat_id, stream=direct_stream
                    )
                    if trace:
                        trace.mark("voice_started")
                    media.stream_tier = profile.tier
                    media.stream_url = remote_url
                    await self._prefetch_next(chat_id)
                    media.time = 1
                    await db.add_call(chat_id)
                    self._mark_playback_started(chat_id, media)
                    if trace:
                        trace.mark("thumb_queued")
                    await update_now_playing(chat_id, message, media)
                    if trace:
                        trace.mark("np_updated")
                        trace.finish("ready")
                    return
                except StreamCapacityError:
                    # Nothing was submitted to PyTgCalls, so this is not a
                    # startup failure: do not race to local and do not start a
                    # download for a stream that cannot be admitted.
                    try:
                        startup_gate.signal_fatal(chat_id, "stream_capacity_reached")
                    except Exception:
                        pass
                    await self._notify_stream_busy(
                        chat_id, message, _lang, where="direct_startup"
                    )
                    return
                except Exception as ex:
                    logger.warning(
                        "TikTok direct stream startup failed for chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    if config.TIKTOK_DIRECT_STREAM_ONLY:
                        tpl = await db.get_custom_text_for_chat(
                            chat_id, "error_no_file", _lang["error_no_file"]
                        )
                        await utils.edit_formatted(
                            message,
                            tpl,
                            config.SUPPORT_CHAT,
                            template_key="error_no_file",
                        )
                        return await self.play_next(chat_id)
                    local_path = await tiktok.await_current_cache_or_download(
                        chat_id,
                        media,
                        ping=profile.ping,
                        message_id=(
                            message.id
                            if media.video and not silent_direct_fallback
                            else None
                        ),
                    )
                    if local_path:
                        media.file_path = local_path
            elif not config.TIKTOK_DIRECT_STREAM_ONLY:
                local_path = await tiktok.await_current_cache_or_download(
                    chat_id,
                    media,
                    ping=profile.ping,
                    message_id=(
                        message.id
                        if media.video and not silent_direct_fallback
                        else None
                    ),
                )
                if local_path:
                    media.file_path = local_path

        # Facebook direct stream — same pattern as TikTok
        can_try_facebook_direct = (
            bool(config.FACEBOOK_DIRECT_STREAM)
            and not seek_time
            and not media.file_path
            and getattr(media, "source", None) == "facebook_remote"
            and bool(getattr(media, "url", None))
        )
        if can_try_facebook_direct:
            if config.FACEBOOK_DIRECT_CACHE_BG and allow_current_cache(profile.tier):
                try:
                    await facebook.start_current_cache(chat_id, media)
                except Exception as ex:
                    logger.warning(
                        "Facebook parallel cache kickoff failed for chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
            remote_url = None
            try:
                remote_url, local_path = await facebook.resolve_direct_stream(
                    url=str(getattr(media, "url", "")),
                    media_id=str(getattr(media, "id", "")),
                    video=bool(getattr(media, "video", False)),
                )
                if local_path:
                    media.local_path = local_path
            except Exception as ex:
                logger.warning(
                    "Facebook direct stream resolve failed for chat_id=%s media_id=%s: %s",
                    chat_id,
                    getattr(media, "id", None),
                    ex,
                )
                remote_url = None

            if remote_url:
                fb_stream = types.MediaStream(
                    media_path=remote_url,
                    audio_parameters=profile.audio_parameters,
                    video_parameters=profile.video_parameters,
                    audio_flags=types.MediaStream.Flags.REQUIRED,
                    video_flags=(
                        types.MediaStream.Flags.AUTO_DETECT
                        if media.video
                        else types.MediaStream.Flags.IGNORE
                    ),
                )
                try:
                    await self._play_with_startup_slot(
                        client, chat_id=chat_id, stream=fb_stream
                    )
                    if trace:
                        trace.mark("voice_started")
                    media.stream_tier = profile.tier
                    media.stream_url = remote_url
                    await self._prefetch_next(chat_id)
                    media.time = 1
                    await db.add_call(chat_id)
                    self._mark_playback_started(chat_id, media)
                    if trace:
                        trace.mark("thumb_queued")
                    await update_now_playing(chat_id, message, media)
                    if trace:
                        trace.mark("np_updated")
                        trace.finish("ready")
                    return
                except StreamCapacityError:
                    # Nothing was submitted to PyTgCalls, so this is not a
                    # startup failure: do not race to local and do not start a
                    # download for a stream that cannot be admitted.
                    try:
                        startup_gate.signal_fatal(chat_id, "stream_capacity_reached")
                    except Exception:
                        pass
                    await self._notify_stream_busy(
                        chat_id, message, _lang, where="direct_startup"
                    )
                    return
                except Exception as ex:
                    logger.warning(
                        "Facebook direct stream startup failed for chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    if config.FACEBOOK_DIRECT_STREAM_ONLY:
                        tpl = await db.get_custom_text_for_chat(
                            chat_id, "error_no_file", _lang["error_no_file"]
                        )
                        await utils.edit_formatted(
                            message, tpl, config.SUPPORT_CHAT, template_key="error_no_file"
                        )
                        return await self.play_next(chat_id)
                    local_path = await facebook.await_current_cache_or_download(
                        chat_id, media, ping=profile.ping,
                        message_id=message.id if media.video and not silent_direct_fallback else None,
                    )
                    if local_path:
                        media.file_path = local_path
            elif not config.FACEBOOK_DIRECT_STREAM_ONLY:
                local_path = await facebook.await_current_cache_or_download(
                    chat_id, media, ping=profile.ping,
                    message_id=message.id if media.video and not silent_direct_fallback else None,
                )
                if local_path:
                    media.file_path = local_path

        can_try_telegram_direct = (
            bool(getattr(config, "TELEGRAM_DIRECT_STREAM", True))
            and not seek_time
            and not media.file_path
            and getattr(media, "source", None) == "telegram_remote"
            and bool(getattr(media, "telegram_file_id", None))
        )
        if can_try_telegram_direct:
            if getattr(config, "TELEGRAM_DIRECT_CACHE_BG", True):
                try:
                    await tg.start_current_cache(chat_id, media)
                except Exception as ex:
                    logger.warning(
                        "Telegram parallel cache kickoff failed for chat_id=%s "
                        "media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
            remote_url = None
            local_path = None
            try:
                remote_url, local_path = await tg.resolve_direct_stream(media=media)
                if local_path:
                    media.local_path = local_path
            except Exception as ex:
                logger.warning(
                    "Telegram direct stream resolve failed for chat_id=%s media_id=%s: %s",
                    chat_id, getattr(media, "id", None), ex,
                )
                remote_url = None

            if remote_url:
                tg_stream = types.MediaStream(
                    media_path=remote_url,
                    audio_parameters=profile.audio_parameters,
                    video_parameters=profile.video_parameters,
                    audio_flags=types.MediaStream.Flags.REQUIRED,
                    video_flags=(
                        types.MediaStream.Flags.AUTO_DETECT
                        if media.video
                        else types.MediaStream.Flags.IGNORE
                    ),
                )
                try:
                    await self._play_with_startup_slot(
                        client, chat_id=chat_id, stream=tg_stream
                    )
                    if trace:
                        trace.mark("voice_started")
                    media.stream_tier = profile.tier
                    media.stream_url = remote_url
                    # Start background download for cache
                    if getattr(config, "TELEGRAM_DIRECT_CACHE_BG", True):
                        try:
                            await tg.start_current_cache(chat_id, media)
                        except Exception:
                            pass
                    await self._prefetch_next(chat_id)
                    media.time = 1
                    await db.add_call(chat_id)
                    self._mark_playback_started(chat_id, media)
                    if trace:
                        trace.mark("thumb_queued")
                    await update_now_playing(chat_id, message, media)
                    if trace:
                        trace.mark("np_updated")
                        trace.finish("ready")
                    return
                except StreamCapacityError:
                    # Nothing was submitted to PyTgCalls, so this is not a
                    # startup failure: do not race to local and do not start a
                    # download for a stream that cannot be admitted.
                    try:
                        startup_gate.signal_fatal(chat_id, "stream_capacity_reached")
                    except Exception:
                        pass
                    await self._notify_stream_busy(
                        chat_id, message, _lang, where="direct_startup"
                    )
                    return
                except Exception as ex:
                    logger.warning(
                        "Telegram direct stream startup failed for chat_id=%s media_id=%s: %s",
                        chat_id, getattr(media, "id", None), ex,
                    )

        # After YouTube direct miss/fail: start/join the deferred local fallback.
        download_progress_message = (
            getattr(media, "download_progress_message", None) or message
        )
        download_progress_lang = (
            getattr(media, "download_progress_lang", None) or _lang
        )
        if (
            can_try_direct
            and not media.file_path
            and not seek_time
            and getattr(media, "source", None) not in {"tiktok_remote", "facebook_remote", "telegram_remote"}
        ):
            try:
                parallel_tier = (
                    fallback_quality_tier if media.video else None
                )
                ready_path = self._pick_ready_local(media, parallel_tier)
                if ready_path:
                    media.file_path = ready_path
                    media.local_path = ready_path
                    media.source = "youtube_local"
                    logger.info(
                        "Auto-play local (complete file) chat_id=%s media_id=%s path=%s",
                        chat_id,
                        getattr(media, "id", None),
                        ready_path,
                    )
                else:
                    logger.info(
                        "Waiting for complete local file after direct fail "
                        "chat_id=%s media_id=%s video=%s",
                        chat_id,
                        getattr(media, "id", None),
                        int(bool(media.video)),
                    )
                    cached_local_path = await self._await_parallel_local(
                        chat_id,
                        media,
                        quality_tier=parallel_tier,
                        ping=profile.ping,
                        progress_message=download_progress_message,
                        progress_lang=download_progress_lang,
                    )
                    min_bytes = 512 * 1024 if media.video else 64 * 1024
                    if yt.is_complete_media_file(
                        cached_local_path, min_bytes=min_bytes
                    ):
                        media.file_path = cached_local_path
                        media.local_path = cached_local_path
                        media.source = "youtube_local"
                        logger.info(
                            "Auto-play local after direct fail chat_id=%s media_id=%s path=%s",
                            chat_id,
                            getattr(media, "id", None),
                            cached_local_path,
                        )
                    elif cached_local_path:
                        logger.warning(
                            "Local fallback incomplete (skipping partial) path=%s size=%s",
                            cached_local_path,
                            (
                                os.path.getsize(cached_local_path)
                                if os.path.isfile(cached_local_path)
                                else 0
                            ),
                        )
                        media.file_path = None
            except Exception as ex:
                logger.warning(
                    "Local fallback after direct failed chat_id=%s media_id=%s: %s",
                    chat_id,
                    getattr(media, "id", None),
                    ex,
                )

        # CDN pipeline:
        # - YouTube cold start: READY *hit only* (no download/publish wait).
        #   Parallel local already races into downloads/; full ensure_ready used
        #   to add ~8–30s "Downloading..." before first audio.
        # - Non-YouTube / miss local: full ensure_ready as before.
        if (
            bool(getattr(config, "CDN_ENABLED", False))
            and not media.file_path
            and getattr(media, "id", None)
            and getattr(media, "source", None)
            not in {"telegram_remote", "telegram_local", "youtube_local"}
        ):
            youtube_cold = (
                len(str(getattr(media, "id", "") or "")) == 11
                and getattr(media, "source", None)
                not in {
                    "tiktok_remote",
                    "soundcloud",
                    "soundcloud_remote",
                    "soundcloud_local",
                }
            )
            asset = None
            try:
                if youtube_cold:
                    try:
                        ready_hit = cdn._find_ready_file(
                            str(media.id),
                            bool(getattr(media, "video", False)),
                            fallback_quality_tier
                            if getattr(media, "video", False)
                            else None,
                        )
                    except Exception:
                        ready_hit = None
                    min_b = (
                        512 * 1024
                        if getattr(media, "video", False)
                        else 64 * 1024
                    )
                    if ready_hit and yt.is_complete_media_file(
                        str(ready_hit), min_bytes=min_b
                    ):
                        media.file_path = str(ready_hit)
                        media.local_path = str(ready_hit)
                        media.source = "cdn_local"
                        logger.info(
                            "CDN READY hit (instant) chat_id=%s media_id=%s path=%s",
                            chat_id,
                            getattr(media, "id", None),
                            ready_hit,
                        )
                    else:
                        logger.info(
                            "CDN cold skip ensure_ready (use local race) "
                            "chat_id=%s media_id=%s",
                            chat_id,
                            getattr(media, "id", None),
                        )
                else:
                    asset = await cdn.ensure_ready(
                        media,
                        quality_tier=fallback_quality_tier,
                        progress_message=(
                            message
                            if media.video and not silent_direct_fallback
                            else None
                        ),
                        progress_lang=(
                            _lang
                            if media.video and not silent_direct_fallback
                            else None
                        ),
                    )
            except Exception as ex:
                logger.warning(
                    "CDN path failed chat_id=%s media_id=%s: %s",
                    chat_id,
                    getattr(media, "id", None),
                    ex,
                )
                asset = None
            if asset and not media.file_path:
                media.local_path = asset.local_path
                # Seek needs a local file for ffmpeg -ss.
                if seek_time > 1:
                    media.file_path = asset.local_path
                    media.source = "cdn_local"
                else:
                    media.file_path = asset.play_url or asset.local_path
                    media.source = "cdn" if asset.play_url else "cdn_local"
                logger.info(
                    "CDN play path chat_id=%s media_id=%s path=%s",
                    chat_id,
                    getattr(media, "id", None),
                    media.file_path,
                )

        if (
            not media.file_path
            and getattr(media, "source", None)
            not in {
                "tiktok_remote",
                "facebook_remote",
                "telegram_remote",
                "telegram_local",
                "soundcloud",
                "soundcloud_remote",
                "soundcloud_local",
            }
        ):
            cached_local_path = await self.prefetch_manager.await_current_cache_or_download(
                chat_id,
                media,
                quality_tier=fallback_quality_tier,
                ping=profile.ping,
                progress_message=download_progress_message,
                progress_lang=download_progress_lang,
            )
            if cached_local_path:
                media.file_path = cached_local_path
        if not media.file_path and getattr(media, "source", None) == "tiktok_remote":
            media.file_path = await tiktok.await_current_cache_or_download(
                chat_id,
                media,
                ping=profile.ping,
                message_id=(
                    message.id
                    if media.video and not silent_direct_fallback
                    else None
                ),
            )
        if not media.file_path:
            if getattr(media, "source", None) == "tiktok_remote":
                media.file_path = await tiktok.download(
                    url=getattr(media, "url", ""),
                    media_id=str(getattr(media, "id", "")),
                    video=bool(getattr(media, "video", False)),
                    message_id=message.id,
                )
                if media.file_path and getattr(media, "source", None) == "tiktok_remote":
                    media.source = "tiktok_local"
            if getattr(media, "source", None) == "facebook_remote":
                media.file_path = await facebook.download(
                    url=getattr(media, "url", ""),
                    media_id=str(getattr(media, "id", "")),
                    video=bool(getattr(media, "video", False)),
                    message_id=(
                        message.id
                        if getattr(media, "video", False) and not silent_direct_fallback
                        else None
                    ),
                )
            elif getattr(media, "source", None) == "telegram_remote":
                # Fallback: download Telegram file to disk if direct stream failed
                media.file_path = await tg.ensure_local_file(media)
            elif getattr(media, "source", None) in {
                "soundcloud",
                "soundcloud_remote",
                "soundcloud_local",
            }:
                try:
                    from AnonX_3.core.resolver.soundcloud import soundcloud

                    media.file_path = await soundcloud.download(
                        str(getattr(media, "url", "") or ""),
                        media_id=str(getattr(media, "id", "sc")),
                        video=bool(media.video),
                    )
                except Exception as ex:
                    logger.warning("SoundCloud late download failed: %s", ex)
            else:
                if not getattr(media, "_cache_one_shot", False):
                    owner_tier = yt.resolve_download_quality_tier(
                        fallback_quality_tier,
                        video=bool(getattr(media, "video", False)),
                    )
                    setattr(media, "_cache_quality_tier", owner_tier)
                    setattr(media, "_cache_one_shot", True)
                    setattr(media, "_cache_one_shot_attempted", True)
                    media.file_path = await yt.download(
                        media.id,
                        video=media.video,
                        quality_tier=owner_tier,
                        message_id=getattr(download_progress_message, "id", None),
                        progress_message=download_progress_message,
                        progress_lang=download_progress_lang,
                        progress_media=media,
                        one_shot=True,
                        quality_tier_resolved=True,
                    )
                    setattr(media, "_cache_one_shot_complete", True)
                    if media.file_path:
                        setattr(media, "_cache_one_shot_succeeded", True)
                else:
                    logger.info(
                        "one-shot acquisition exhausted; no second yt-dlp call "
                        "chat_id=%s media_id=%s",
                        chat_id,
                        getattr(media, "id", None),
                    )
                # A specific upload may forbid external playback even though the
                # song exists elsewhere on YouTube. Prefer another upload before
                # crossing providers.
                if (
                    not media.file_path
                    and not getattr(media, "_cache_one_shot", False)
                    and not yt.auth_challenge_active()
                    and getattr(media, "source", None)
                    not in {
                        "soundcloud",
                        "soundcloud_remote",
                        "soundcloud_local",
                        "tiktok_remote",
                        "telegram_remote",
                    }
                ):
                    try:
                        alternate = await yt.alternate_track(
                            media,
                            m_id=getattr(media, "message_id", 0) or 0,
                            video=bool(media.video),
                        )
                        if alternate is not None:
                            # Keep catalog/UI provenance attached to the media
                            # actually being extracted.  Never store alternate
                            # bytes under the rejected upload's ID.
                            for attr in (
                                "original_query",
                                "normalized_query",
                                "request_source",
                                "_play_request_scope",
                            ):
                                value = getattr(media, attr, None)
                                if value not in (None, "", 0):
                                    setattr(alternate, attr, value)
                            alternate.download_progress_message = (
                                download_progress_message
                            )
                            alternate.download_progress_lang = download_progress_lang
                            alternate_tier = yt.resolve_download_quality_tier(
                                fallback_quality_tier,
                                video=bool(media.video),
                            )
                            alternate_path = await yt.download(
                                alternate.id,
                                video=bool(media.video),
                                quality_tier=alternate_tier,
                                message_id=getattr(
                                    download_progress_message, "id", None
                                ),
                                progress_message=download_progress_message,
                                progress_lang=download_progress_lang,
                                progress_media=alternate,
                                one_shot=True,
                                quality_tier_resolved=True,
                            )
                            if alternate_path:
                                logger.info(
                                    "YouTube alternate recovered chat_id=%s "
                                    "failed_id=%s alternate_id=%s",
                                    chat_id,
                                    media.id,
                                    alternate.id,
                                )
                                media.id = alternate.id
                                media.title = alternate.title
                                media.url = alternate.url
                                media.thumbnail = (
                                    getattr(alternate, "thumbnail", None)
                                    or media.thumbnail
                                )
                                media.duration = (
                                    getattr(alternate, "duration", None)
                                    or media.duration
                                )
                                media.duration_sec = (
                                    getattr(alternate, "duration_sec", 0)
                                    or getattr(media, "duration_sec", 0)
                                )
                                if hasattr(media, "channel_name"):
                                    media.channel_name = getattr(
                                        alternate, "channel_name", None
                                    )
                                media.file_path = alternate_path
                                media.local_path = alternate_path
                    except Exception as ex:
                        logger.warning("YouTube alternate fallback failed: %s", ex)

                # YouTube upload alternatives dead → scored SoundCloud fallback (once)
                if (
                    not media.file_path
                    and not bool(media.video)
                    and not getattr(media, "_fallback_tried", False)
                    and getattr(media, "source", None)
                    not in {
                        "soundcloud",
                        "soundcloud_remote",
                        "soundcloud_local",
                        "tiktok_remote",
                        "telegram_remote",
                    }
                ):
                    try:
                        from AnonX_3.core.resolver.fallback import find_fallback_track
                        from AnonX_3.core.resolver.soundcloud import soundcloud as sc_dl

                        media._fallback_tried = True  # type: ignore[attr-defined]
                        fb, meta = await find_fallback_track(
                            media=media,
                            message_id=getattr(media, "message_id", 0) or 0,
                            video=bool(media.video),
                            user=getattr(media, "user", None),
                        )
                        if fb and getattr(fb, "url", None):
                            logger.info(
                                "play_media YouTube fail → SoundCloud fallback "
                                "chat_id=%s score=%s title=%r",
                                chat_id,
                                (meta or {}).get("score"),
                                fb.title,
                            )
                            # Replace playable identity
                            media.id = fb.id
                            media.title = fb.title
                            media.url = fb.url
                            media.thumbnail = getattr(fb, "thumbnail", None) or media.thumbnail
                            media.duration = getattr(fb, "duration", None) or media.duration
                            media.duration_sec = getattr(fb, "duration_sec", 0) or getattr(
                                media, "duration_sec", 0
                            )
                            if hasattr(media, "channel_name"):
                                media.channel_name = getattr(fb, "channel_name", None)
                            media.source = "soundcloud"
                            media.file_path = await sc_dl.download(
                                str(fb.url),
                                media_id=str(fb.id),
                                video=bool(media.video),
                            )
                            if media.file_path:
                                media.local_path = media.file_path
                                media.source = "soundcloud_local"
                    except Exception as ex:
                        logger.warning(
                            "SoundCloud fallback after YouTube download fail: %s", ex
                        )
            if not media.file_path and media.video:
                if getattr(media, "source", None) == "tiktok_remote":
                    media.file_path = await tiktok.download(
                        url=getattr(media, "url", ""),
                        media_id=str(getattr(media, "id", "")),
                        video=True,
                        message_id=message.id if not silent_direct_fallback else None,
                    )
                    if media.file_path:
                        pass
                    else:
                        tpl = await db.get_custom_text_for_chat(
                            chat_id, "error_no_file", _lang["error_no_file"]
                        )
                        await utils.edit_formatted(
                            message, tpl, config.SUPPORT_CHAT, template_key="error_no_file"
                        )
                        return await self.play_next(chat_id)
                elif not getattr(media, "_cache_one_shot", False):
                    owner_tier = yt.resolve_download_quality_tier(
                        fallback_quality_tier, video=True
                    )
                    setattr(media, "_cache_quality_tier", owner_tier)
                    setattr(media, "_cache_one_shot", True)
                    setattr(media, "_cache_one_shot_attempted", True)
                    media.file_path = await yt.download(
                        media.id,
                        video=True,
                        quality_tier=owner_tier,
                        message_id=(
                            message.id if not silent_direct_fallback else None
                        ),
                        progress_message=(
                            message if not silent_direct_fallback else None
                        ),
                        progress_lang=_lang if not silent_direct_fallback else None,
                        progress_media=media,
                        one_shot=True,
                        quality_tier_resolved=True,
                    )
                    setattr(media, "_cache_one_shot_complete", True)
                    if media.file_path:
                        setattr(media, "_cache_one_shot_succeeded", True)
            if not media.file_path:
                error_key = (
                    "error_youtube_auth"
                    if yt.auth_challenge_for(getattr(media, "id", None))
                    else "error_no_file"
                )
                error_fallback = _lang.get(
                    error_key,
                    _lang["error_no_file"],
                )
                tpl = await db.get_custom_text_for_chat(
                    chat_id,
                    error_key,
                    error_fallback,
                )
                await utils.edit_formatted(
                    message,
                    tpl,
                    config.SUPPORT_CHAT,
                    template_key=error_key,
                )
                return await self.play_next(chat_id)

        # Local file already downloaded (Telegram / finished yt-dlp): do not re-enter
        # YouTube/CDN pipelines or leave the UI on "Processing file...".
        if media.file_path:
            path_ok = yt.is_complete_media_file(
                media.file_path,
                min_bytes=(128 * 1024 if media.video else 8 * 1024),
            )
            if not path_ok:
                settled = await yt.wait_media_file_ready(
                    media.file_path,
                    video=bool(media.video),
                    timeout=60.0 if media.video else 20.0,
                )
                if settled:
                    media.file_path = settled
                    media.local_path = settled
                    path_ok = True
            if path_ok:
                # Do not post "Download ready. Joining voice chat..." — go
                # straight to VC join, then Now Playing (user request).
                pass
            else:
                tpl = await db.get_custom_text_for_chat(
                    chat_id, "error_no_file", _lang["error_no_file"]
                )
                await utils.edit_formatted(
                    message, tpl, config.SUPPORT_CHAT, template_key="error_no_file"
                )
                return await self.play_next(chat_id)

        logger.info(
            "Playback stream profile chat_id=%s media_id=%s tier=%s reason=%s cpu=%.1f ping=%s download_tier=%s max_height=%s path=%s",
            chat_id,
            getattr(media, "id", None),
            profile.tier,
            profile.reason,
            profile.cpu,
            f"{profile.ping:.1f}" if profile.ping is not None else "n/a",
            profile.download_tier or "manual",
            profile.max_height or "env",
            getattr(media, "file_path", None),
        )

        # Protect active playback cache entry (local/CDN path)
        try:
            startup_gate.begin(chat_id, media, quality_tier=fallback_quality_tier)
        except Exception:
            pass
        if trace:
            trace.set_meta(mode="cache-hit")
        # Stream admission is not decided here. `_play_with_startup_slot`
        # reserves the slot for local/cache playback exactly as it does for
        # direct playback, and raises StreamCapacityError instead of starting
        # a call the box cannot carry.
        if not resource_manager.can_start_ffmpeg():
            logger.warning(
                "MAX_FFMPEG_CONCURRENT pressure chat_id=%s active=%s",
                chat_id,
                resource_manager.stats(),
            )
        resource_manager.note_ffmpeg(+1)
        stream = types.MediaStream(
            media_path=media.file_path,
            audio_parameters=profile.audio_parameters,
            video_parameters=profile.video_parameters,
            audio_flags=types.MediaStream.Flags.REQUIRED,
            video_flags=(
                types.MediaStream.Flags.AUTO_DETECT
                if media.video
                else types.MediaStream.Flags.IGNORE
            ),
            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
        )
        tried_telegram_local_fallback = False
        tried_tiktok_local_fallback = False
        tried_facebook_local_fallback = False
        tried_cdn_local_fallback = False
        tried_no_audio_redownload = False
        retries = 0
        try:
          while True:
            try:
                await self._play_with_startup_slot(
                    client,
                    chat_id=chat_id,
                    stream=stream,
                    unmute_mode=(
                        "skip" if prejoined_client is not None else "background"
                    ),
                )
                if trace:
                    trace.mark("voice_started")
                media.stream_tier = profile.tier
                media.stream_url = media.file_path
                await self._prefetch_next(chat_id)
                should_update_now_playing = force_now_playing or not seek_time
                if should_update_now_playing:
                    if not seek_time:
                        media.time = 1
                    await db.add_call(chat_id)
                    self._mark_playback_started(chat_id, media)
                    if prejoined_client is not None:
                        self._schedule_profile_refresh(chat_id, media, client)
                    if trace:
                        trace.mark("thumb_queued")
                    await update_now_playing(chat_id, message, media)
                    if trace:
                        trace.mark("np_updated")
                        trace.finish("ready")
                else:
                    self._mark_playback_started(chat_id, media)
                break
            except FileNotFoundError:
                tpl = await db.get_custom_text_for_chat(
                    chat_id, "error_no_file", _lang["error_no_file"]
                )
                await utils.edit_formatted(
                    message, tpl, config.SUPPORT_CHAT, template_key="error_no_file"
                )
                await self.play_next(chat_id)
                break
            except TimeoutError as ex:
                if (
                    getattr(media, "source", None) == "telegram_remote"
                    and not tried_telegram_local_fallback
                    and not config.TELEGRAM_DIRECT_STREAM_ONLY
                ):
                    tried_telegram_local_fallback = True
                    logger.warning(
                        "Telegram remote stream timeout; trying local fallback chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    local_path = await tg.await_current_cache_or_download(
                        chat_id,
                        media,
                        ping=profile.ping,
                    )
                    if local_path:
                        media.file_path = local_path
                        stream = types.MediaStream(
                            media_path=media.file_path,
                            audio_parameters=profile.audio_parameters,
                            video_parameters=profile.video_parameters,
                            audio_flags=types.MediaStream.Flags.REQUIRED,
                            video_flags=(
                                types.MediaStream.Flags.AUTO_DETECT
                                if media.video
                                else types.MediaStream.Flags.IGNORE
                            ),
                            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                        )
                        continue
                if (
                    getattr(media, "source", None) == "tiktok_remote"
                    and not tried_tiktok_local_fallback
                    and not config.TIKTOK_DIRECT_STREAM_ONLY
                ):
                    tried_tiktok_local_fallback = True
                    logger.warning(
                        "TikTok remote stream timeout; trying local fallback chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    await self._notify_join_status(
                        message,
                        _lang,
                        _lang.get(
                            "play_downloading",
                            "TikTok stream unavailable. Downloading local copy…",
                        ),
                    )
                    local_path = await tiktok.await_current_cache_or_download(
                        chat_id,
                        media,
                        ping=profile.ping,
                        message_id=message.id if media.video else None,
                    )
                    if local_path:
                        media.file_path = local_path
                        stream = types.MediaStream(
                            media_path=media.file_path,
                            audio_parameters=profile.audio_parameters,
                            video_parameters=profile.video_parameters,
                            audio_flags=types.MediaStream.Flags.REQUIRED,
                            video_flags=(
                                types.MediaStream.Flags.AUTO_DETECT
                                if media.video
                                else types.MediaStream.Flags.IGNORE
                            ),
                            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                        )
                        continue
                if (
                    getattr(media, "source", None) == "facebook_remote"
                    and not tried_facebook_local_fallback
                    and not config.FACEBOOK_DIRECT_STREAM_ONLY
                ):
                    tried_facebook_local_fallback = True
                    logger.warning(
                        "Facebook remote stream timeout; trying local fallback chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    local_path = await facebook.await_current_cache_or_download(
                        chat_id,
                        media,
                        ping=profile.ping,
                        message_id=message.id if media.video else None,
                    )
                    if local_path:
                        media.file_path = local_path
                        stream = types.MediaStream(
                            media_path=media.file_path,
                            audio_parameters=profile.audio_parameters,
                            video_parameters=profile.video_parameters,
                            audio_flags=types.MediaStream.Flags.REQUIRED,
                            video_flags=(
                                types.MediaStream.Flags.AUTO_DETECT
                                if media.video
                                else types.MediaStream.Flags.IGNORE
                            ),
                            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                        )
                        continue
                logger.warning(
                    "Group call join timed out for chat_id=%s media_id=%s: %s",
                    chat_id,
                    getattr(media, "id", None),
                    ex,
                )
                retries += 1
                if retries >= 5:
                    logger.warning(
                        "Group call join timeout exhausted (attempt %s/5) for chat_id=%s; skipping to next track",
                        retries,
                        chat_id,
                    )
                    await self._notify_join_status(
                        message,
                        _lang,
                        _lang.get(
                            "play_join_failed",
                            "Could not join the voice chat. Skipping to the next track.",
                        ),
                    )
                    return await self.play_next(chat_id)
                logger.warning(
                    "Group call join timeout (attempt %s/5) for chat_id=%s; retrying",
                    retries,
                    chat_id,
                )
                await self._notify_join_status(
                    message,
                    _lang,
                    _lang.get(
                        "play_join_retry",
                        "Joining voice chat… retry {0}/5",
                    ).format(retries),
                )
                await asyncio.sleep(min(0.5 * retries, 2.0))
                continue
            except exceptions.NoActiveGroupCall:
                await asyncio.sleep(2)
                try:
                    await self._play_with_startup_slot(
                        client,
                        chat_id=chat_id,
                        stream=stream,
                        unmute_mode=(
                            "skip" if prejoined_client is not None else "background"
                        ),
                    )
                    await self._prefetch_next(chat_id)
                    should_update_now_playing = force_now_playing or not seek_time
                    if should_update_now_playing:
                        if not seek_time:
                            media.time = 1
                        await db.add_call(chat_id)
                        self._mark_playback_started(chat_id, media)
                        if prejoined_client is not None:
                            self._schedule_profile_refresh(chat_id, media, client)
                        await update_now_playing(chat_id, message, media)
                    else:
                        self._mark_playback_started(chat_id, media)
                    break
                except exceptions.NoActiveGroupCall:
                    await self.stop(chat_id)
                    await utils.edit_text(message, _lang["error_no_call"], ignore_stale=True)
                    break
                except StreamCapacityError:
                    # Raised inside an except clause, so it would escape the
                    # whole try statement and bypass the loop's handler.
                    # Terminate the retry here instead.
                    await self._notify_stream_busy(
                        chat_id, message, _lang, where="rejoin_retry"
                    )
                    break
            except exceptions.NoVideoSourceFound:
                logger.warning(
                    "NoVideoSourceFound chat_id=%s media_id=%s source=%s",
                    chat_id,
                    getattr(media, "id", None),
                    getattr(media, "source", None),
                )
                try:
                    await utils.edit_text(
                        message,
                        _lang.get("error_no_video", _lang["error_no_file"]),
                        ignore_stale=True,
                    )
                except Exception:
                    pass
                await self.play_next(chat_id)
                break
            except exceptions.NoAudioSourceFound:
                if (
                    getattr(media, "source", None) == "tiktok_remote"
                    and not tried_tiktok_local_fallback
                    and not config.TIKTOK_DIRECT_STREAM_ONLY
                ):
                    tried_tiktok_local_fallback = True
                    local_path = await tiktok.await_current_cache_or_download(
                        chat_id,
                        media,
                        ping=profile.ping,
                        message_id=(
                            message.id
                            if media.video and not silent_direct_fallback
                            else None
                        ),
                    )
                    if local_path:
                        media.file_path = local_path
                        stream = types.MediaStream(
                            media_path=media.file_path,
                            audio_parameters=profile.audio_parameters,
                            video_parameters=profile.video_parameters,
                            audio_flags=types.MediaStream.Flags.REQUIRED,
                            video_flags=(
                                types.MediaStream.Flags.AUTO_DETECT
                                if media.video
                                else types.MediaStream.Flags.IGNORE
                            ),
                            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                        )
                        continue
                if (
                    getattr(media, "source", None) == "facebook_remote"
                    and not tried_facebook_local_fallback
                    and not config.FACEBOOK_DIRECT_STREAM_ONLY
                ):
                    tried_facebook_local_fallback = True
                    logger.warning(
                        "Facebook remote stream failed; trying local fallback chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    local_path = await facebook.await_current_cache_or_download(
                        chat_id,
                        media,
                        ping=profile.ping,
                        message_id=message.id if media.video else None,
                    )
                    if local_path:
                        media.file_path = local_path
                        stream = types.MediaStream(
                            media_path=media.file_path,
                            audio_parameters=profile.audio_parameters,
                            video_parameters=profile.video_parameters,
                            audio_flags=types.MediaStream.Flags.REQUIRED,
                            video_flags=(
                                types.MediaStream.Flags.AUTO_DETECT
                                if media.video
                                else types.MediaStream.Flags.IGNORE
                            ),
                            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                        )
                        continue
                # Local/YouTube file often fails as webm; re-download once (m4a path).
                if (
                    not tried_no_audio_redownload
                    and not media.video
                    and getattr(media, "id", None)
                    and len(str(media.id)) == 11
                    and not getattr(media, "_cache_one_shot", False)
                    and getattr(media, "source", None)
                    not in {"tiktok_remote", "facebook_remote", "telegram_remote", "soundcloud", "soundcloud_local"}
                ):
                    tried_no_audio_redownload = True
                    logger.warning(
                        "NoAudioSourceFound on local file; re-downloading chat_id=%s media_id=%s path=%s",
                        chat_id,
                        media.id,
                        getattr(media, "file_path", None),
                    )
                    await self._notify_join_status(
                        message,
                        _lang,
                        _lang.get(
                            "play_rebuffer",
                            "Audio format issue. Re-preparing track…",
                        ),
                    )
                    try:
                        # Drop bad cache file so download is forced
                        bad = getattr(media, "file_path", None)
                        if bad and os.path.isfile(bad):
                            try:
                                os.remove(bad)
                            except Exception:
                                pass
                        media.file_path = None
                        media.local_path = None
                        fresh = await yt.download(
                            str(media.id),
                            video=False,
                            quality_tier=fallback_quality_tier or "normal",
                        )
                        if fresh and yt.is_complete_media_file(fresh, min_bytes=8 * 1024):
                            media.file_path = fresh
                            media.source = "youtube_local"
                            stream = types.MediaStream(
                                media_path=media.file_path,
                                audio_parameters=profile.audio_parameters,
                                video_parameters=profile.video_parameters,
                                audio_flags=types.MediaStream.Flags.REQUIRED,
                                video_flags=types.MediaStream.Flags.IGNORE,
                                ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                            )
                            continue
                    except Exception as rex:
                        logger.warning("No-audio re-download failed: %s", rex)
                try:
                    await utils.edit_text(
                        message, _lang["error_no_audio"], ignore_stale=True
                    )
                except Exception:
                    pass
                await self.play_next(chat_id)
                break
            except (ConnectionError, ConnectionNotFound, TelegramServerError) as ex:
                if (
                    getattr(media, "source", None) == "telegram_remote"
                    and not tried_telegram_local_fallback
                    and not config.TELEGRAM_DIRECT_STREAM_ONLY
                ):
                    tried_telegram_local_fallback = True
                    logger.warning(
                        "Telegram remote stream failed; trying local fallback chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    local_path = await tg.await_current_cache_or_download(
                        chat_id,
                        media,
                        ping=profile.ping,
                    )
                    if local_path:
                        media.file_path = local_path
                        stream = types.MediaStream(
                            media_path=media.file_path,
                            audio_parameters=profile.audio_parameters,
                            video_parameters=profile.video_parameters,
                            audio_flags=types.MediaStream.Flags.REQUIRED,
                            video_flags=(
                                types.MediaStream.Flags.AUTO_DETECT
                                if media.video
                                else types.MediaStream.Flags.IGNORE
                            ),
                            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                        )
                        continue
                if (
                    getattr(media, "source", None) == "tiktok_remote"
                    and not tried_tiktok_local_fallback
                    and not config.TIKTOK_DIRECT_STREAM_ONLY
                ):
                    tried_tiktok_local_fallback = True
                    logger.warning(
                        "TikTok remote stream failed; trying local fallback chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    local_path = await tiktok.await_current_cache_or_download(
                        chat_id,
                        media,
                        ping=profile.ping,
                        message_id=(
                            message.id
                            if media.video and not silent_direct_fallback
                            else None
                        ),
                    )
                    if local_path:
                        media.file_path = local_path
                        stream = types.MediaStream(
                            media_path=media.file_path,
                            audio_parameters=profile.audio_parameters,
                            video_parameters=profile.video_parameters,
                            audio_flags=types.MediaStream.Flags.REQUIRED,
                            video_flags=(
                                types.MediaStream.Flags.AUTO_DETECT
                                if media.video
                                else types.MediaStream.Flags.IGNORE
                            ),
                            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                        )
                        continue
                if (
                    getattr(media, "source", None) == "cdn"
                    and not tried_cdn_local_fallback
                    and getattr(media, "local_path", None)
                ):
                    tried_cdn_local_fallback = True
                    local_path = media.local_path
                    logger.warning(
                        "CDN remote stream failed; trying local ready path chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    if local_path:
                        media.file_path = local_path
                        media.source = "cdn_local"
                        stream = types.MediaStream(
                            media_path=media.file_path,
                            audio_parameters=profile.audio_parameters,
                            video_parameters=profile.video_parameters,
                            audio_flags=types.MediaStream.Flags.REQUIRED,
                            video_flags=(
                                types.MediaStream.Flags.AUTO_DETECT
                                if media.video
                                else types.MediaStream.Flags.IGNORE
                            ),
                            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
                        )
                        continue
                retries += 1
                if retries >= 5:
                    logger.warning(
                        "VoIP connection exhausted (attempt %s/5) for chat_id=%s; skipping to next track",
                        retries,
                        chat_id,
                    )
                    await self._notify_join_status(
                        message,
                        _lang,
                        _lang.get(
                            "play_join_failed",
                            "Could not join the voice chat. Skipping to the next track.",
                        ),
                    )
                    return await self.play_next(chat_id)
                logger.warning(
                    "VoIP connection error (attempt %s/5) for chat_id=%s: %s",
                    retries,
                    chat_id,
                    ex,
                )
                await self._notify_join_status(
                    message,
                    _lang,
                    _lang.get(
                        "play_join_retry",
                        "Joining voice chat… retry {0}/5",
                    ).format(retries),
                )
                await asyncio.sleep(min(0.5 * retries, 2.0))
                continue
            except errors.FloodWait as fw:
                logger.warning(
                    "FloodWait %ss for assistant %s in chat_id=%s; rotating",
                    fw.value,
                    db.assistant.get(chat_id, "?"),
                    chat_id,
                )
                tried = self._flood_tried.setdefault(chat_id, set())
                tried.add(db.assistant.get(chat_id, 1))
                if len(tried) >= len(userbot.clients):
                    wait_seconds = max(int(getattr(fw, "value", 0) or 0), 1)
                    self._flood_tried.pop(chat_id, None)
                    logger.warning(
                        "All assistants hit FloodWait in chat_id=%s; retrying current track after %ss",
                        chat_id,
                        wait_seconds,
                    )
                    try:
                        tpl = await db.get_custom_text_for_chat(
                            chat_id,
                            "error_flood_wait",
                            _lang.get(
                                "error_flood_wait",
                                "<u><b>Assistant is rate-limited</b></u>\n\n"
                                "Telegram returned FloodWait. Retrying this track automatically in "
                                "<b>{0}s</b>. Please wait.",
                            ),
                        )
                        await utils.edit_formatted(
                            message,
                            tpl,
                            wait_seconds,
                            template_key="error_flood_wait",
                            ignore_stale=True,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(wait_seconds)
                    client = await db.get_assistant(chat_id)
                    continue
                await db.rotate_assistant(chat_id)
                client = await db.get_assistant(chat_id)
                continue
            except RTMPStreamingUnsupported:
                await self.stop(chat_id)
                await message.edit_text(_lang["error_rtmp"])
                break
            except (ShellError, SignalingError) as ex:
                text = str(ex).lower()
                mark_ffmpeg_fail()
                # ntgcalls often raises ShellError for "No audio source found"
                if "no audio source" in text or "audio source" in text:
                    logger.warning(
                        "ShellError no-audio chat_id=%s media_id=%s: %s",
                        chat_id,
                        getattr(media, "id", None),
                        ex,
                    )
                    if (
                        not tried_no_audio_redownload
                        and not media.video
                        and getattr(media, "id", None)
                        and len(str(media.id)) == 11
                        and not getattr(media, "_cache_one_shot", False)
                    ):
                        tried_no_audio_redownload = True
                        try:
                            bad = getattr(media, "file_path", None)
                            if bad and os.path.isfile(bad):
                                os.remove(bad)
                        except Exception:
                            pass
                        try:
                            fresh = await yt.download(
                                str(media.id),
                                video=False,
                                quality_tier=fallback_quality_tier or "normal",
                            )
                            if fresh and yt.is_complete_media_file(
                                fresh, min_bytes=8 * 1024
                            ):
                                media.file_path = fresh
                                stream = types.MediaStream(
                                    media_path=media.file_path,
                                    audio_parameters=profile.audio_parameters,
                                    video_parameters=profile.video_parameters,
                                    audio_flags=types.MediaStream.Flags.REQUIRED,
                                    video_flags=types.MediaStream.Flags.IGNORE,
                                    ffmpeg_parameters=(
                                        f"-ss {seek_time}" if seek_time > 1 else None
                                    ),
                                )
                                continue
                        except Exception as rex:
                            logger.warning("ShellError re-download failed: %s", rex)
                    try:
                        await utils.edit_text(
                            message, _lang["error_no_audio"], ignore_stale=True
                        )
                    except Exception:
                        pass
                    await self.play_next(chat_id)
                    break
                logger.warning(
                    "Shell/Signal error for chat_id=%s media_id=%s: %s",
                    chat_id, getattr(media, "id", "?"), ex,
                )
                # NEVER stop() before play_next — stop clears the whole queue
                # so remaining tracks die (first song ok, later songs gone).
                try:
                    await utils.edit_text(
                        message,
                        _lang["error_tg_server"]
                        if "error_tg_server" in _lang
                        else str(_lang["error_no_call"]),
                        ignore_stale=True,
                    )
                except Exception:
                    pass
                await self.play_next(chat_id)
                break
            except StreamCapacityError:
                # Admission refused — the call was never started, so this is
                # not a broken track. Leave the queue intact (no play_next:
                # advancing would silently drop the song) and tell the user to
                # retry once a slot frees up.
                await self._notify_stream_busy(
                    chat_id, message, _lang, where="play_loop"
                )
                break
            except Exception as ex:
                logger.error(
                    "Unhandled play_media error chat_id=%s media_id=%s: %s",
                    chat_id, getattr(media, "media_id", "?"), ex,
                    exc_info=True,
                )
                # Do not wipe queue — advance / leave only if empty.
                try:
                    await message.edit_text(_lang["error_tg_server"])
                except Exception:
                    pass
                await self.play_next(chat_id)
                break
        finally:
            resource_manager.note_ffmpeg(-1)


    async def replay(self, chat_id: int) -> None:
        if not await db.get_call(chat_id):
            return

        media = queue.get_current(chat_id)
        await self._delete_now_playing(chat_id, media)
        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_again"])
        media.message_id = msg.id
        await self.play_media(chat_id, msg, media)


    async def play_next(self, chat_id: int) -> None:
        """Advance queue / autoplay / stop. Serialized per chat (StreamEnded-safe)."""
        async with self._play_next_lock(chat_id):
            depth = int(self._play_next_depth.get(chat_id, 0) or 0)
            if depth > 12:
                logger.warning(
                    "play_next depth limit chat_id=%s — stopping to avoid loop",
                    chat_id,
                )
                self._play_next_depth[chat_id] = 0
                return await self.stop(chat_id)
            self._play_next_depth[chat_id] = depth + 1
            try:
                await self._play_next_body(chat_id)
            finally:
                self._play_next_depth[chat_id] = max(
                    0, int(self._play_next_depth.get(chat_id, 1) or 1) - 1
                )

    async def _play_next_body(self, chat_id: int) -> None:
        if loop := await db.get_loop(chat_id):
            await db.set_loop(chat_id, loop - 1)
            return await self.replay(chat_id)

        current = queue.get_current(chat_id)
        # Mark ending so delayed StreamEnded for this media is ignored
        if current is not None:
            self._ending_media_id[chat_id] = str(getattr(current, "id", "") or "")
        await self._delete_now_playing(chat_id, current)
        # Drop refcount for previous track before starting next
        try:
            self._cancel_post_start_tasks(chat_id)
            self._cancel_startup_proof(chat_id)
            startup_gate.end(chat_id)
        except Exception:
            pass
        direct_watchdog.disarm(chat_id)

        media = queue.get_next(chat_id)
        if not media:
            autoplay_enabled = await db.get_autoplay(chat_id)
            if autoplay_enabled and current is not None:
                aidj_mode = await db.get_aidj_mode(chat_id)
                try:
                    track = await asyncio.wait_for(
                        yt.autoplay_track(
                            current,
                            exclude_ids=self._autoplay_excludes(chat_id, current),
                            intent=aidj_mode,
                            **self._autoplay_context(chat_id, current),
                        ),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "autoplay timed out chat_id=%s seed=%s",
                        chat_id,
                        getattr(current, "id", None),
                    )
                    track = None
                    # Clear autoplay state so a timed-out recommendation
                    # is never re-served as the same failed seed.
                    self.autoplay_recent_ids.pop(chat_id, None)
                    self.autoplay_recent_titles.pop(chat_id, None)
                    self.autoplay_recent_artists.pop(chat_id, None)
                    self.autoplay_artist_streak.pop(chat_id, None)
                    self.autoplay_last_artist.pop(chat_id, None)
                except Exception as ex:
                    logger.warning("autoplay failed chat_id=%s: %s", chat_id, ex)
                    track = None
                if track:
                    track.video = bool(getattr(current, "video", False))
                    track.user = (
                        f"AI DJ ({aidj_mode})"
                        if aidj_mode != "similar"
                        else "Autoplay"
                    )
                    # Prefer remote/direct start — do not require pre-download
                    if not getattr(track, "source", None):
                        track.source = "youtube"
                    enrich_request(
                        track,
                        chat_id=chat_id,
                        query=getattr(track, "title", None),
                        request_source="aidj",
                        priority=20,
                    )
                    queue.add(chat_id, track)
                    metrics.inc("aidj_recommendation_queued")
                    self._remember_autoplay(chat_id, track)
                    media = queue.get_current(chat_id)
                if not media:
                    _lang = await lang.get_lang(chat_id)
                    tpl = await db.get_custom_text_for_chat(
                        chat_id,
                        "autoplay_no_match",
                        _lang.get(
                            "autoplay_no_match",
                            "No similar songs were found for autoplay, so playback has stopped.",
                        ),
                    )
                    try:
                        await utils.send_formatted(
                            chat_id,
                            tpl,
                            template_key="autoplay_no_match",
                        )
                    except Exception:
                        pass
            if not media:
                logger.info(
                    "queue empty → leave VC chat_id=%s autoplay=%s had_current=%s",
                    chat_id,
                    autoplay_enabled if autoplay_enabled is not None else False,
                    bool(current),
                )
                return await self.stop(chat_id)

        # Dynamic delete: remove this track's queue/status card when it starts.
        queued_card_id = int(
            getattr(media, "message_id", 0)
            or getattr(media, "status_message_id", 0)
            or 0
        )
        if queued_card_id:
            await self._delete_status_message(
                chat_id, queued_card_id, reason="track_starting_from_queue"
            )
            media.message_id = 0
            try:
                media.status_message_id = 0
            except Exception:
                pass

        _lang = await lang.get_lang(chat_id)
        tpl = await db.get_custom_text_for_chat(chat_id, "play_next", _lang["play_next"])
        if isinstance(tpl, dict):
            msg = await utils.send_message(
                chat_id, tpl["text"], entities=tpl.get("entities")
            )
        else:
            msg = await app.send_message(chat_id=chat_id, text=tpl)

        # Soft prefetch: prefer local when ready, but NEVER block next-track on
        # download failure when direct/remote play is possible (old bug: queue
        # songs required file_path → skip → empty → leave while first song used direct).
        if not getattr(media, "file_path", None):
            client = await db.get_assistant(chat_id)
            profile = self.stream_profile.select(chat_id, client)
            src = getattr(media, "source", None)
            try:
                if src == "tiktok_remote":
                    media.file_path = await tiktok.download(
                        url=getattr(media, "url", ""),
                        media_id=str(getattr(media, "id", "")),
                        video=bool(getattr(media, "video", False)),
                    )
                if src == "facebook_remote":
                    if config.FACEBOOK_DIRECT_CACHE_BG:
                        try:
                            await facebook.start_current_cache(chat_id, media)
                        except Exception as ex:
                            logger.warning(
                                "Facebook parallel cache kickoff failed for queued media chat_id=%s media_id=%s: %s",
                                chat_id,
                                getattr(media, "id", None),
                                ex,
                            )
                    # Short wait only — play_media can still direct/cache later
                    try:
                        media.file_path = await asyncio.wait_for(
                            facebook.await_current_cache_or_download(
                                chat_id,
                                media,
                                ping=profile.ping,
                                message_id=msg.id if media.video else None,
                            ),
                            timeout=4.0,
                        )
                    except Exception:
                        media.file_path = None
                elif src in {"soundcloud", "soundcloud_remote"}:
                    # SoundCloud needs URL resolve inside play_media; no forced download here.
                    pass
                elif src == "telegram_remote":
                    # Telegram files are downloaded via tg.ensure_local_file(),
                    # never through the YouTube/yt-dlp pipeline.
                    pass
                elif src is None or src in {
                    "youtube", "youtube_local", "youtube_remote",
                    "cdn", "cdn_local", "cache_local",
                }:
                    try:
                        await asyncio.wait_for(
                            self.prefetch_manager.join_or_download(
                                chat_id,
                                media,
                                quality_tier=profile.download_tier,
                            ),
                            timeout=4.0,
                        )
                    except Exception as ex:
                        logger.info(
                            "play_next prefetch soft-fail chat_id=%s media_id=%s: %s",
                            chat_id,
                            getattr(media, "id", None),
                            ex,
                        )

            except Exception as ex:
                logger.debug("play_next prep error: %s", ex)

            if not getattr(media, "file_path", None) and not self._can_play_without_local_file(
                media
            ):
                logger.warning(
                    "Skipping unplayable queued media chat_id=%s media_id=%s source=%s; trying next",
                    chat_id,
                    getattr(media, "id", None),
                    getattr(media, "source", None),
                )
                if msg and getattr(msg, "id", None):
                    await self._delete_status_message(
                        chat_id, msg.id, reason="unplayable_skip"
                    )
                # Already holding the per-chat lock — call body, not play_next.
                depth = int(self._play_next_depth.get(chat_id, 0) or 0)
                if depth > 12:
                    return await self.stop(chat_id)
                self._play_next_depth[chat_id] = depth + 1
                try:
                    return await self._play_next_body(chat_id)
                finally:
                    self._play_next_depth[chat_id] = max(
                        0, int(self._play_next_depth.get(chat_id, 1) or 1) - 1
                    )

        media.message_id = msg.id
        try:
            media.status_message_id = msg.id
        except Exception:
            pass
        logger.info(
            "play_next start chat_id=%s media_id=%s has_file=%s source=%s",
            chat_id,
            getattr(media, "id", None),
            bool(getattr(media, "file_path", None)),
            getattr(media, "source", None),
        )
        try:
            await self.play_media(chat_id, msg, media)
        except Exception as ex:
            logger.warning(
                "play_next play_media failed chat_id=%s media_id=%s: %s — advancing",
                chat_id,
                getattr(media, "id", None),
                ex,
            )
            # Clean up status message so it doesn't linger
            if msg and getattr(msg, "id", None):
                await self._delete_status_message(
                    chat_id, msg.id, reason="play_media_failed_advance"
                )
            # Remove failed track and try next
            queue.remove_current(chat_id)
            await self.play_next(chat_id)


    async def ping(self) -> float | None:
        pings = []
        for client in self.clients:
            try:
                ping = float(client.ping) if client.ping is not None else None
            except Exception:
                ping = None
            if ping and ping > 0:
                pings.append(ping)
        if not pings:
            return None
        return round(sum(pings) / len(pings), 3)


    async def _branch_b_cdn_publish(
        self, media, quality_tier: str | None = None, *, chat_id: int | None = None
    ) -> None:
        """Publish the current one-shot local result without reopening yt-dlp."""
        try:
            from AnonX_3.core.cdn import cdn

            owner_chat_id = int(chat_id or getattr(media, "chat_id", 0) or 0)
            owner = (
                self.prefetch_manager.current_task(owner_chat_id, media)
                if owner_chat_id
                else None
            )
            if owner is not None and not owner.done():
                # The direct VC start is already live.  Let its one physical
                # yt-dlp owner finish, then publish that verified file; moving
                # an in-progress destination into CDN would corrupt both paths.
                try:
                    await asyncio.shield(owner)
                except Exception:
                    pass

            quality_tier = getattr(media, "_cache_quality_tier", quality_tier)
            path = self._pick_ready_local(media, quality_tier)
            if path:
                media.local_path = path
            if not cdn.enabled:
                if path:
                    direct_watchdog.update_local(
                        owner_chat_id, path
                    )
                return

            # A failed/current one-shot is terminal for this request.  CDN may
            # publish an existing local result but must not revive extraction.
            if not path and getattr(media, "_cache_one_shot", False):
                return
            asset = await cdn.ensure_ready(media, quality_tier=quality_tier)
            if asset and asset.local_path:
                media.local_path = asset.local_path
                logger.info(
                    "Branch B CDN READY media_id=%s path=%s",
                    getattr(media, "id", None),
                    asset.local_path,
                )
        except Exception as ex:
            logger.debug("Branch B CDN publish failed: %s", ex)

    async def _try_direct_local_failover(
        self, chat_id: int, *, startup_failure: bool = False
    ) -> bool:
        """Acquire local and restart after a failed/ended remote direct stream.

        ``startup_failure=True`` is used when detached ``client.play()`` itself
        failed before an async proof monitor could be installed.  In that case
        this function must own the fallback immediately instead of deferring to
        a proof task that does not exist.
        """
        # Never start fallback work when the VC is already gone.
        if not await self._verify_chat_still_playable(chat_id):
            logger.info(
                "direct failover: VC absent or chat stopped — "
                "performing full cleanup chat_id=%s", chat_id,
            )
            await self.stop(chat_id)
            return False
        media = queue.get_current(chat_id)
        if not media:
            return False
        # Signal gate if still in startup window.  A normal StreamEnded is
        # delegated to the async proof monitor.  A synchronous/detached play
        # exception has no proof task yet, so it must continue into local
        # fallback here or the current track would be skipped.
        if startup_gate.in_gate_window(chat_id):
            startup_gate.signal_fatal(
                chat_id,
                "direct_play_failed_in_gate"
                if startup_failure
                else "stream_ended_in_gate",
            )
            if not startup_failure:
                return False
            startup_gate.end(chat_id)

        if not startup_failure and not direct_watchdog.should_failover_on_stream_end(
            chat_id, media
        ):
            direct_watchdog.disarm(chat_id)
            return False

        watch = direct_watchdog.consume_failover(chat_id)
        local = None
        if watch and watch.local_path:
            local = watch.local_path
        local = local or getattr(media, "local_path", None) or getattr(
            media, "file_path", None
        )
        if local and str(local).startswith("http"):
            local = None
        if not local or not yt.is_complete_media_file(
            local, min_bytes=(512 * 1024 if getattr(media, "video", False) else 8 * 1024)
        ):
            # The local-cache task itself is the readiness event.
            task = self.prefetch_manager.current_task(chat_id, media)
            if task is not None:
                try:
                    await asyncio.shield(task)
                except Exception:
                    pass
            local = self._pick_ready_local(
                media, getattr(media, "stream_tier", None)
            )
        if not local and (
            getattr(media, "source", None) == "youtube_remote"
            or bool(getattr(media, "stream_url", None))
        ):
            if getattr(config, "YOUTUBE_DIRECT_STREAM_ONLY", False) and not bool(
                getattr(media, "video", False)
            ):
                logger.info(
                    "direct failover: local disabled by direct-only mode "
                    "chat_id=%s media_id=%s",
                    chat_id,
                    getattr(media, "id", None),
                )
                return False
            try:
                quality_tier = (
                    getattr(media, "_cache_quality_tier", None)
                    if bool(getattr(media, "video", False))
                    else None
                )
                logger.info(
                    "direct failover: starting deferred local fallback "
                    "chat_id=%s media_id=%s video=%s",
                    chat_id,
                    getattr(media, "id", None),
                    int(bool(getattr(media, "video", False))),
                )
                local = await self._await_parallel_local(
                    chat_id,
                    media,
                    quality_tier=quality_tier,
                )
            except Exception as ex:
                logger.warning(
                    "direct failover: deferred local fallback failed "
                    "chat_id=%s media_id=%s: %s",
                    chat_id,
                    getattr(media, "id", None),
                    ex,
                )
                local = None
        if not local:
            logger.info(
                "direct failover: no local READY chat_id=%s media_id=%s",
                chat_id,
                getattr(media, "id", None),
            )
            return False

        seek = int(getattr(media, "time", 0) or 0)
        if seek < 2:
            seek = 0
        media.file_path = local
        media.local_path = local
        media.stream_url = None
        media.source = "youtube_local"
        try:
            mark_local_failover()
        except Exception:
            pass
        logger.info(
            "direct→local failover chat_id=%s media_id=%s path=%s seek=%s",
            chat_id,
            getattr(media, "id", None),
            local,
            seek,
        )
        try:
            # Reuse the card the user is already looking at.  This is a
            # failover of the *current* track, so the queue-advance card both
            # misreports what is happening and leaves the download progress
            # card orphaned at its final 100% frame.
            msg = None
            seen: set[int] = set()
            progress = getattr(media, "download_progress_message", None)
            for raw in (
                getattr(media, "message_id", 0),
                getattr(progress, "id", 0),
                getattr(media, "status_message_id", 0),
            ):
                try:
                    mid = int(raw or 0)
                except (TypeError, ValueError):
                    continue
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                try:
                    candidate = await app.get_messages(chat_id, mid)
                except Exception:
                    continue
                # A deleted card comes back as an empty Message, not None.
                if candidate is None or getattr(candidate, "empty", False):
                    continue
                msg = candidate
                break
            if msg is None:
                _lang = await lang.get_lang(chat_id)
                msg = await app.send_message(chat_id, _lang.get("play_starting", "…"))
            media.message_id = msg.id
            await self.play_media(
                chat_id, msg, media, seek_time=seek, force_now_playing=True
            )
            return True
        except Exception as ex:
            logger.warning("direct→local failover play failed: %s", ex)
            return False

    async def decorators(self, client: PyTgCalls) -> None:
        @client.on_update()
        async def update_handler(_, update: types.Update) -> None:
            if isinstance(update, types.StreamEnded):
                # AnonX baseline: StreamEnded → play_next → stop/leave when empty.
                # Keep only light guards (debounce, residual switch, startup gate).
                st = getattr(update, "stream_type", None)
                if st not in (
                    types.StreamEnded.Type.AUDIO,
                    types.StreamEnded.Type.VIDEO,
                ):
                    return
                chat_id = update.chat_id
                now = time.time()
                current = queue.get_current(chat_id)
                cur_id = str(getattr(current, "id", "") or "") if current else ""

                # Already advancing this chat (long prefetch) — drop duplicate ends.
                if self._play_next_lock_held(chat_id):
                    logger.debug(
                        "StreamEnded ignored (play_next busy) chat_id=%s type=%s",
                        chat_id,
                        st,
                    )
                    return

                # Already finishing this exact media (AUDIO+VIDEO dual end).
                if cur_id and self._ending_media_id.get(chat_id) == cur_id:
                    logger.debug(
                        "StreamEnded ignored (already ending) chat_id=%s media=%s",
                        chat_id,
                        cur_id,
                    )
                    return

                # Residual end from previous track after next already started.
                switch_until = float(self._stream_switch_until.get(chat_id, 0.0) or 0.0)
                if now < switch_until:
                    if startup_gate.in_gate_window(chat_id):
                        # Real early death of the NEW stream inside direct gate only
                        startup_gate.signal_fatal(chat_id, "stream_ended_in_gate")
                        logger.info(
                            "StreamEnded during switch+gate chat_id=%s type=%s → fatal",
                            chat_id,
                            getattr(st, "name", st),
                        )
                        return
                    logger.debug(
                        "StreamEnded ignored (post-switch residual) chat_id=%s type=%s",
                        chat_id,
                        st,
                    )
                    return

                # Debounce dual AUDIO+VIDEO for same track
                last = float(self._stream_end_at.get(chat_id, 0.0) or 0.0)
                if now - last < 2.5:
                    logger.debug(
                        "StreamEnded debounced chat_id=%s type=%s",
                        chat_id,
                        st,
                    )
                    return
                self._stream_end_at[chat_id] = now
                logger.info(
                    "StreamEnded chat_id=%s type=%s media_id=%s",
                    chat_id,
                    getattr(st, "name", st),
                    cur_id or "?",
                )

                # Direct startup gate only: wake the asynchronous proof monitor.
                # Must not swallow natural song end (local path used to get stuck).
                if startup_gate.in_gate_window(chat_id):
                    startup_gate.signal_fatal(chat_id, "stream_ended_in_gate")
                    logger.info(
                        "StreamEnded during direct gate chat_id=%s type=%s → fatal",
                        chat_id,
                        getattr(st, "name", st),
                    )
                    return

                if not current:
                    # No queue item but stream ended → leave VC cleanly
                    logger.info(
                        "StreamEnded with empty queue → stop/leave chat_id=%s", chat_id
                    )
                    try:
                        await self.stop(chat_id)
                    except Exception:
                        pass
                    return

                # Early remote death only (not natural completion) → one local retry
                try:
                    if await self._try_direct_local_failover(chat_id):
                        return
                except Exception as ex:
                    logger.debug("failover handler error: %s", ex)
                direct_watchdog.disarm(chat_id)
                if cur_id:
                    self._ending_media_id[chat_id] = cur_id
                # Same as AnonX: advance queue or stop() → leave_call
                try:
                    await self.play_next(chat_id)
                except Exception as ex:
                    logger.warning(
                        "play_next after StreamEnded failed chat_id=%s: %s",
                        chat_id,
                        ex,
                    )
                    try:
                        await self.stop(chat_id)
                    except Exception:
                        pass
            elif isinstance(update, types.ChatUpdate):
                if update.status in [
                    types.ChatUpdate.Status.KICKED,
                    types.ChatUpdate.Status.LEFT_GROUP,
                    types.ChatUpdate.Status.CLOSED_VOICE_CHAT,
                ]:
                    direct_watchdog.disarm(update.chat_id)
                    await self.stop(update.chat_id)

    @staticmethod
    def _flatten_owned_tasks(roots: list[asyncio.Task]) -> list[asyncio.Task]:
        """Flatten only call-owned task trees, avoiding asyncio.all_tasks()."""
        current = asyncio.current_task()
        seen = {id(current)} if current is not None else set()
        collected: list[asyncio.Task] = []
        stack = list(roots)
        while stack:
            task = stack.pop()
            identity = id(task)
            if identity in seen:
                continue
            seen.add(identity)
            collected.append(task)
            for child in getattr(task, "_children", ()):
                if id(child) not in seen:
                    stack.append(child)
        return collected

    def _detach_prefetch_tasks(self) -> list[asyncio.Task]:
        """Detach every task registry owned by PrefetchManager."""
        detached: list[asyncio.Task] = []
        for name in ("prefetch", "secondary", "current_cache"):
            registry = getattr(self.prefetch_manager, name, None)
            if not isinstance(registry, dict):
                continue
            for entry in list(registry.values()):
                if (
                    isinstance(entry, tuple)
                    and len(entry) > 1
                    and isinstance(entry[1], asyncio.Task)
                ):
                    detached.append(entry[1])
            registry.clear()
        terminal = getattr(self.prefetch_manager, "_terminal_outcomes", None)
        if isinstance(terminal, dict):
            terminal.clear()
        return detached

    @staticmethod
    async def _stop_call_client(client) -> None:
        """Stop a PyTgCalls client across old and new library APIs."""
        failures: list[Exception] = []
        try:
            stopped = False
            for method_name in ("shutdown", "stop", "close"):
                method = getattr(client, method_name, None)
                if not callable(method):
                    continue
                try:
                    result = method()
                    if inspect.isawaitable(result):
                        await result
                    stopped = True
                    break
                except Exception as ex:
                    failures.append(ex)

            # py-tgcalls 2.x has no process-level stop method.  Leave each
            # owned call explicitly before retiring its binding/executor.
            if not stopped:
                active = getattr(client, "calls", {})
                if inspect.isawaitable(active):
                    active = await active
                elif callable(active):
                    active = active()
                    if inspect.isawaitable(active):
                        active = await active
                leave_call = getattr(client, "leave_call", None)
                if callable(leave_call):
                    for chat_id in list(getattr(active, "keys", lambda: ())()):
                        try:
                            result = leave_call(chat_id)
                            if inspect.isawaitable(result):
                                await result
                        except Exception as ex:
                            failures.append(ex)
        except Exception as ex:
            failures.append(ex)
        finally:
            try:
                client._is_running = False
            except Exception:
                pass
            executor = getattr(client, "executor", None)
            if executor is not None:
                try:
                    await asyncio.to_thread(
                        executor.shutdown,
                        wait=True,
                        cancel_futures=True,
                    )
                except TypeError:
                    try:
                        await asyncio.to_thread(executor.shutdown, wait=True)
                    except Exception as ex:
                        failures.append(ex)
                except Exception as ex:
                    failures.append(ex)
        if failures:
            raise ExceptionGroup("PyTgCalls client shutdown failed", failures)

    async def shutdown(self) -> None:
        """Quiesce all tasks and PyTgCalls clients owned by this service."""
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return

            self._shutting_down = True
            clients = list(self.clients)
            self.clients.clear()

            roots = [*self._owned_tasks, *self._detach_prefetch_tasks()]
            self._owned_tasks.clear()
            self._vc_unmute_tasks.clear()
            self._direct_cache_tasks.clear()
            self._thumbnail_tasks.clear()
            self._post_start_tasks.clear()
            self._post_start_by_chat.clear()
            self._startup_proof_tasks.clear()
            watches = getattr(direct_watchdog, "_watches", None)
            if isinstance(watches, dict):
                watches.clear()

            pending = [
                task
                for task in self._flatten_owned_tasks(roots)
                if not task.done()
            ]
            for task in pending:
                children = getattr(task, "_children", None)
                if children is not None:
                    try:
                        children.clear()
                    except Exception:
                        pass
            for task in pending:
                task.cancel()

            shutdown_failures: list[Exception] = []
            try:
                if pending:
                    done, lingering = await asyncio.wait(pending, timeout=5.0)
                    if done:
                        task_results = await asyncio.gather(
                            *done,
                            return_exceptions=True,
                        )
                        shutdown_failures.extend(
                            result
                            for result in task_results
                            if isinstance(result, Exception)
                        )
                    if lingering:
                        shutdown_failures.append(
                            RuntimeError(
                                f"{len(lingering)} PyTgCalls-owned task(s) "
                                "ignored cancellation"
                            )
                        )

                results = await asyncio.gather(
                    *(self._stop_call_client(client) for client in reversed(clients)),
                    return_exceptions=True,
                )
                for ex in results:
                    if isinstance(ex, Exception):
                        shutdown_failures.append(ex)
            finally:
                self._startup_error = None
                self._ready.set()
                self._shutdown_complete = True

            if shutdown_failures:
                for ex in shutdown_failures:
                    logger.warning("PyTgCalls shutdown step failed: %s", ex)
                raise ExceptionGroup(
                    "PyTgCalls shutdown failed",
                    shutdown_failures,
                )
            logger.info("PyTgCalls client(s) stopped.")

    async def boot(self) -> None:
        if self._shutting_down:
            raise RuntimeError("PyTgCalls service is shutting down")
        PyTgCallsSession.notice_displayed = True
        self._ready.clear()
        self._startup_error = None
        try:
            for ub in userbot.clients:
                client = PyTgCalls(ub, cache_duration=100)
                # Own partial startup immediately.  If start() fails, shutdown
                # still sees and cleans the client's binding and executor.
                self.clients.append(client)
                await client.start()
                await self.decorators(client)
            if not self.clients:
                raise RuntimeError("No PyTgCalls clients could be started.")
            if bool(getattr(config, "DIRECT_PREVALIDATED_RAW_AUDIO", True)):
                try:
                    await asyncio.to_thread(self._prepare_raw_ffmpeg_launcher)
                except Exception as ex:
                    self._raw_direct_disabled_reason = (
                        f"launcher_preflight:{type(ex).__name__}:{str(ex)[:120]}"
                    )
                    logger.warning(
                        "Raw direct launcher disabled at boot; MediaStream fallback "
                        "will be used. reason=%s",
                        self._raw_direct_disabled_reason,
                    )
            logger.info("PyTgCalls client(s) started.")
        except Exception as ex:
            self._startup_error = ex
            raise
        finally:
            self._ready.set()

    async def wait_until_ready(self, timeout: float | None = None) -> None:
        """Wait until voice-call clients finish booting."""
        if self.clients:
            return
        try:
            await asyncio.wait_for(
                self._ready.wait(),
                timeout=self.READY_TIMEOUT_SEC if timeout is None else timeout,
            )
        except asyncio.TimeoutError as ex:
            raise RuntimeError(
                "Assistant call clients are still starting; retry shortly."
            ) from ex
        if not self.clients:
            raise RuntimeError(
                "No assistant call clients are available. "
                "Check the preceding PyTgCalls startup error."
            ) from self._startup_error
