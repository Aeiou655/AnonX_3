# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
"""Latency-critical VC startup fastpaths.

The production <=3s path needs two properties that the stock cold path does not
provide reliably:

* a failed speculative EXTERNAL preconnect must reconnect while YouTube is still
  resolving, not after the resolver has already consumed ~3s; and
* once the VC connects, EXTERNAL silence must keep the native RTP clock warm
  until the first real PCM frame arrives, otherwise NTgCalls pays another ~1s
  clock-start tail after source readiness.

The existing reconnect-free vplay EXTERNAL-audio/raw-video handoff remains in
place.  All changes are guarded and the normal raw/local fallbacks remain.
"""

from __future__ import annotations

import asyncio
import time

from ntgcalls import (
    ConnectionError,
    ConnectionNotFound,
    FrameData,
    MediaSource,
    StreamDevice,
)
from pytgcalls import types

from AnonX_3 import config, logger
from AnonX_3.core.calls import TgCall
from AnonX_3.core.resource_manager import resource_manager

_PATCH_SENTINEL = "_anonx_vplay_cold_start_deep_fix_v3412_sub3"
_PRECONNECT_RETRY_GUARD = "_sub3_preconnect_retry_guard"


def _enabled(name: str, default: bool = True) -> bool:
    return bool(getattr(config, name, default))


async def _hard_reset_failed_binding(
    self: TgCall, client, chat_id: int, reason: str
) -> None:
    """Remove a half-created NTgCalls binding before an overlapping retry."""

    try:
        await client.leave_call(int(chat_id), close=False)
    except Exception:
        pass

    binding = getattr(client, "_binding", None)
    stop = getattr(binding, "stop", None)
    if callable(stop):
        try:
            await stop(int(chat_id))
        except Exception:
            pass

    try:
        resource_manager.unregister_stream(int(chat_id))
    except Exception:
        pass

    logger.info(
        "direct_failed_binding_reset chat_id=%s reason=%s reconnect_clean=1",
        chat_id,
        reason,
    )


def _install_patch() -> None:
    if getattr(TgCall, _PATCH_SENTINEL, False):
        return

    original_build_raw = TgCall._build_initial_direct_raw_stream
    original_wait_clock = TgCall._wait_direct_external_packet_clock
    original_close_external = TgCall._close_direct_external_audio
    original_play_with_slot = TgCall._play_with_startup_slot
    original_jit_prime = TgCall._jit_prime_external_capture

    async def jit_prime_external_capture(self, client, session: dict) -> None:
        """Keep EXTERNAL silence flowing after connect until real PCM is ready.

        The stock JIT helper stops the moment ``connected`` is set. Production
        traces then show roughly a one-second outgoing-clock tail after the real
        source is attached. Continue the same 10ms silence feed for a bounded
        window so NTgCalls/TG keep the RTP sender hot while resolver/FFmpeg work
        finishes. ``send_lock`` makes the handoff to the first real frame
        atomic: the keepalive exits as soon as ``first_frame_accepted``/activated
        becomes true.
        """

        await original_jit_prime(self, client, session)
        if not _enabled("DIRECT_EXTERNAL_POSTCONNECT_RTP_KEEPALIVE", True):
            return
        if not session or session.get("closed"):
            return

        connected = session.get("connected")
        accepted = session.get("first_frame_accepted")
        if (
            connected is None
            or not connected.is_set()
            or accepted is None
            or accepted.is_set()
            or session.get("activated")
        ):
            return

        binding = getattr(client, "_binding", None)
        send = getattr(binding, "send_external_frame", None)
        if not callable(send):
            return

        interval = max(
            0.005,
            min(
                0.030,
                float(
                    getattr(config, "DIRECT_EXTERNAL_RTP_KEEPALIVE_MS", 10) or 10
                )
                / 1000.0,
            ),
        )
        max_sec = max(
            0.50,
            min(
                6.0,
                float(
                    getattr(
                        config,
                        "DIRECT_EXTERNAL_POSTCONNECT_KEEPALIVE_SEC",
                        4.5,
                    )
                    or 4.5
                ),
            ),
        )
        silence = bytes(max(1, int(session.get("frame_bytes") or 720)))
        started = time.perf_counter()
        deadline = started + max_sec
        sent = 0
        logger.info(
            "direct_external_rtp_keepalive_started chat_id=%s media_id=%s "
            "interval_ms=%s max_ms=%s",
            session.get("chat_id"),
            session.get("media_id"),
            int(interval * 1000),
            int(max_sec * 1000),
        )
        try:
            while (
                not self._shutting_down
                and not session.get("closed")
                and connected.is_set()
                and not accepted.is_set()
                and not session.get("activated")
                and time.perf_counter() < deadline
            ):
                try:
                    async with session["send_lock"]:
                        if accepted.is_set() or session.get("activated"):
                            break
                        await send(
                            int(session["chat_id"]),
                            StreamDevice.MICROPHONE,
                            silence,
                            FrameData(int(time.time() * 1000), 0, 0, 0),
                        )
                    sent += 1
                except asyncio.CancelledError:
                    raise
                except (ConnectionNotFound, ConnectionError):
                    break
                except Exception:
                    pass
                await asyncio.sleep(interval)
        finally:
            logger.info(
                "direct_external_rtp_keepalive_stopped chat_id=%s media_id=%s "
                "sent=%s elapsed_ms=%s real_audio=%s",
                session.get("chat_id"),
                session.get("media_id"),
                sent,
                int((time.perf_counter() - started) * 1000),
                int(bool(accepted.is_set() or session.get("activated"))),
            )

    def build_initial_direct_raw_stream(self, source, profile, media, *, chat_id: int):
        stream, event_path = original_build_raw(
            self,
            source,
            profile,
            media,
            chat_id=chat_id,
        )
        if not (
            _enabled("DIRECT_VPLAY_HYBRID_AUDIO_HANDOFF", True)
            and bool(getattr(media, "video", False))
        ):
            return stream, event_path

        session = self._direct_external_audio_sessions.get(int(chat_id))
        camera = getattr(stream, "camera", None)
        if (
            session is None
            or session.get("closed")
            or session.get("process") is None
            or camera is None
        ):
            return stream, event_path

        audio = self._raw_audio_parameters(profile.audio_parameters)
        microphone = types.raw.AudioStream(MediaSource.EXTERNAL, "", audio)
        hybrid = types.raw.Stream(microphone=microphone, camera=camera)

        session["vplay_handoff_pending"] = True
        session["vplay_handoff_complete"] = False
        session["vplay_close_deferred"] = False
        session["vplay_hybrid_stream_id"] = id(hybrid)
        session["vplay_fallback_raw_stream"] = stream
        session["vplay_handoff_started"] = time.perf_counter()

        logger.info(
            "vplay_hybrid_external_audio_prepared chat_id=%s media_id=%s "
            "audio=external video=raw reconnect=0",
            chat_id,
            str(getattr(media, "id", "") or ""),
        )
        return hybrid, event_path

    async def wait_direct_external_packet_clock(
        self,
        client,
        *,
        chat_id: int,
        media_id: str,
        timeout: float = 0.40,
    ) -> bool:
        session = self._direct_external_audio_sessions.get(int(chat_id))
        if (
            _enabled("DIRECT_VPLAY_HYBRID_AUDIO_HANDOFF", True)
            and session is not None
            and not session.get("closed")
            and session.get("vplay_handoff_pending")
        ):
            accepted = session.get("first_frame_accepted")
            if accepted is not None and not accepted.is_set():
                try:
                    await asyncio.wait_for(accepted.wait(), timeout=0.08)
                except asyncio.TimeoutError:
                    pass
            logger.info(
                "vplay_audio_lead_handoff_ready chat_id=%s media_id=%s "
                "clock_wait_bypassed=1 first_frame_accepted=%s reconnect=0",
                chat_id,
                media_id,
                int(bool(accepted is not None and accepted.is_set())),
            )
            return True

        return await original_wait_clock(
            self,
            client,
            chat_id=chat_id,
            media_id=media_id,
            timeout=timeout,
        )

    async def close_direct_external_audio(self, session: dict | None) -> None:
        if (
            session
            and not session.get("closed")
            and session.get(_PRECONNECT_RETRY_GUARD)
        ):
            session["sub3_preconnect_close_deferred"] = True
            logger.info(
                "direct_preconnect_session_close_deferred chat_id=%s media_id=%s "
                "until=overlap_retry",
                session.get("chat_id"),
                session.get("media_id"),
            )
            return

        if (
            session
            and not session.get("closed")
            and session.get("vplay_handoff_pending")
            and not session.get("vplay_handoff_complete")
        ):
            session["vplay_close_deferred"] = True
            if session.get("vplay_close_watchdog") is None:

                async def _expire_unfinished_handoff() -> None:
                    await asyncio.sleep(1.5)
                    if (
                        not session.get("closed")
                        and session.get("vplay_handoff_pending")
                        and not session.get("vplay_handoff_complete")
                    ):
                        session["vplay_handoff_pending"] = False
                        await original_close_external(self, session)
                        logger.info(
                            "vplay_hybrid_handoff_expired chat_id=%s media_id=%s",
                            session.get("chat_id"),
                            session.get("media_id"),
                        )

                task = asyncio.create_task(
                    _expire_unfinished_handoff(),
                    name=(
                        f"vplay-handoff-expire:{session.get('chat_id')}:"
                        f"{session.get('media_id')}"
                    ),
                )
                session["vplay_close_watchdog"] = task
                try:
                    self._track_owned_task(task, self._direct_external_audio_tasks)
                except Exception:
                    pass
            logger.info(
                "vplay_external_audio_close_deferred chat_id=%s media_id=%s "
                "until=source_swap",
                session.get("chat_id"),
                session.get("media_id"),
            )
            return

        await original_close_external(self, session)

    async def _cancel_jit_task(session: dict | None) -> None:
        if not session:
            return
        task = session.get("jit_task")
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def play_with_startup_slot(
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
        session = self._direct_external_audio_sessions.get(int(chat_id))
        hybrid = bool(
            session is not None
            and not session.get("closed")
            and session.get("vplay_handoff_pending")
            and session.get("vplay_hybrid_stream_id") == id(stream)
        )
        speculative_external = external_audio_session is not None
        overlap_retry = bool(
            speculative_external
            and _enabled("DIRECT_PRECONNECT_OVERLAP_RETRY", True)
        )
        if overlap_retry and external_audio_session is not None:
            external_audio_session[_PRECONNECT_RETRY_GUARD] = True

        try:
            await original_play_with_slot(
                self,
                client,
                chat_id=chat_id,
                stream=stream,
                unmute_mode=unmute_mode,
                reserved_slot=reserved_slot,
                startup_media_id=startup_media_id,
                external_audio_session=external_audio_session,
            )
        except (ConnectionNotFound, ConnectionError) as ex:
            if bool(getattr(config, "DIRECT_STARTUP_V4", True)):
                raise
            speculative_external_failure = external_audio_session is not None
            direct_retry = bool(
                external_audio_session is None
                and startup_media_id
                and isinstance(stream, types.raw.Stream)
                and _enabled("DIRECT_COLD_BINDING_RETRY", True)
            )

            if speculative_external_failure and overlap_retry:
                external_audio_session[_PRECONNECT_RETRY_GUARD] = False
                await _cancel_jit_task(external_audio_session)
                await _hard_reset_failed_binding(
                    self,
                    client,
                    int(chat_id),
                    f"{type(ex).__name__}:preconnect-overlap",
                )
                logger.info(
                    "direct_preconnect_overlap_retry chat_id=%s media_id=%s "
                    "reason=%s resolver_overlap=1 external_session_reused=1",
                    chat_id,
                    startup_media_id,
                    type(ex).__name__,
                )
                try:
                    await original_play_with_slot(
                        self,
                        client,
                        chat_id=chat_id,
                        stream=stream,
                        unmute_mode=unmute_mode,
                        reserved_slot=None,
                        startup_media_id=startup_media_id,
                        external_audio_session=external_audio_session,
                    )
                except BaseException:
                    if not external_audio_session.get("closed"):
                        await original_close_external(self, external_audio_session)
                    raise
                logger.info(
                    "direct_preconnect_overlap_retry_connected chat_id=%s media_id=%s "
                    "reconnect_on_critical_path=0",
                    chat_id,
                    startup_media_id,
                )
                return

            if overlap_retry and external_audio_session is not None:
                external_audio_session[_PRECONNECT_RETRY_GUARD] = False
                if not external_audio_session.get("closed"):
                    await original_close_external(self, external_audio_session)

            if not (speculative_external_failure or direct_retry):
                raise

            if hybrid and session is not None:
                session["vplay_handoff_pending"] = False
                fallback_stream = session.get("vplay_fallback_raw_stream")
                await original_close_external(self, session)
            else:
                fallback_stream = stream

            await _hard_reset_failed_binding(
                self,
                client,
                int(chat_id),
                f"{type(ex).__name__}:"
                f"{'preconnect' if speculative_external_failure else 'direct'}",
            )

            if speculative_external_failure:
                raise
            if fallback_stream is None:
                raise

            logger.info(
                "direct_cold_binding_retry chat_id=%s media_id=%s hybrid=%s "
                "reason=%s",
                chat_id,
                startup_media_id,
                int(hybrid),
                type(ex).__name__,
            )
            await original_play_with_slot(
                self,
                client,
                chat_id=chat_id,
                stream=fallback_stream,
                unmute_mode="required" if hybrid else unmute_mode,
                reserved_slot=None,
                startup_media_id=startup_media_id,
                external_audio_session=None,
            )
            return
        except BaseException:
            if overlap_retry and external_audio_session is not None:
                external_audio_session[_PRECONNECT_RETRY_GUARD] = False
                if not external_audio_session.get("closed"):
                    await original_close_external(self, external_audio_session)
            raise
        else:
            if overlap_retry and external_audio_session is not None:
                external_audio_session[_PRECONNECT_RETRY_GUARD] = False

        if hybrid and session is not None and not session.get("closed"):
            session["vplay_handoff_complete"] = True
            session["vplay_handoff_pending"] = False
            started = float(
                session.get("vplay_handoff_started") or time.perf_counter()
            )
            logger.info(
                "vplay_hybrid_external_audio_attached chat_id=%s media_id=%s "
                "handoff_ms=%s reconnect=0",
                chat_id,
                session.get("media_id"),
                int((time.perf_counter() - started) * 1000),
            )

    TgCall._jit_prime_external_capture = jit_prime_external_capture
    TgCall._build_initial_direct_raw_stream = build_initial_direct_raw_stream
    TgCall._wait_direct_external_packet_clock = wait_direct_external_packet_clock
    TgCall._close_direct_external_audio = close_direct_external_audio
    TgCall._play_with_startup_slot = play_with_startup_slot
    setattr(TgCall, _PATCH_SENTINEL, True)

    logger.info(
        "startup_fastpath_patch enabled vplay_hybrid_audio=1 cold_binding_retry=1 "
        "preconnect_overlap_retry=1 rtp_keepalive=1"
    )


_install_patch()
