# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
"""Truthful <=3s startup proof for EXTERNAL/JIT playback.

The sub-3s VC fastpath intentionally sends silence after connect to keep the
native RTP sender warm while YouTube/FFmpeg finishes.  The stock observer treats
any positive NTgCalls outgoing clock as "first audio packet", which can count a
keepalive silence packet as audible media.

For live EXTERNAL sessions, prove playback only after:
1. the first real PCM frame has been accepted by NTgCalls,
2. the outgoing clock advances after the pre-submit baseline, and
3. the assistant unmute request is confirmed.

That keeps RTP prewarming without allowing a false <=3s result. Non-EXTERNAL
paths retain the stock observer unchanged.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from AnonX_3 import config, logger, queue
from AnonX_3.core.calls import TgCall

_SENTINEL = "_anonx_sub3_real_pcm_proof_v1"


def _enabled() -> bool:
    return bool(getattr(config, "DIRECT_SUB3_REAL_PCM_PROOF", True))


def _install() -> None:
    if getattr(TgCall, _SENTINEL, False):
        return

    original = TgCall._observe_initial_direct_media

    async def observe_initial_direct_media(
        self,
        *,
        chat_id: int,
        message,
        media,
        client,
        direct_source,
        quality_tier: str | None,
        event_path: str,
        attached_event: asyncio.Event,
        play_state: dict,
        trace=None,
    ) -> None:
        if not _enabled():
            return await original(
                self,
                chat_id=chat_id,
                message=message,
                media=media,
                client=client,
                direct_source=direct_source,
                quality_tier=quality_tier,
                event_path=event_path,
                attached_event=attached_event,
                play_state=play_state,
                trace=trace,
            )

        media_id = str(getattr(media, "id", "") or "")
        session = self._direct_external_audio_sessions.get(int(chat_id))
        accepted = session.get("first_frame_accepted") if session else None
        session_media_id = str(session.get("media_id") or "") if session else ""
        if (
            session is None
            or session.get("closed")
            or accepted is None
            or (session_media_id and session_media_id != media_id)
        ):
            return await original(
                self,
                chat_id=chat_id,
                message=message,
                media=media,
                client=client,
                direct_source=direct_source,
                quality_tier=quality_tier,
                event_path=event_path,
                attached_event=attached_event,
                play_state=play_state,
                trace=trace,
            )

        started = time.perf_counter()
        baseline = session.get("real_pcm_clock_baseline")
        packet_proved = False
        missed_target_logged = False
        event_file = Path(event_path) if event_path else None

        try:
            while time.perf_counter() - started < 30.0:
                if play_state.get("observer_done"):
                    return
                if play_state.get("status") == "failed":
                    if trace:
                        trace.finish("direct_failed")
                    return

                current_session = self._direct_external_audio_sessions.get(int(chat_id))
                if current_session is not session or session.get("closed"):
                    logger.info(
                        "direct_sub3_real_pcm_proof_aborted chat_id=%s media_id=%s "
                        "reason=session_closed_or_replaced",
                        chat_id,
                        media_id,
                    )
                    return

                # The baseline is captured immediately before the first real PCM
                # submission. Camera/source attachment is deliberately irrelevant
                # to audio proof, so /vplay cannot serialize RTP behind a swap.
                if accepted.is_set():
                    try:
                        outgoing_time = int(await client.time(chat_id))
                    except Exception:
                        outgoing_time = 0

                    if baseline is None:
                        baseline = session.get("real_pcm_clock_baseline")
                        if baseline is None:
                            baseline = max(0, outgoing_time)
                        logger.info(
                            "direct_sub3_real_pcm_clock_armed chat_id=%s media_id=%s "
                            "baseline=%s real_pcm=1 video_attach_required=0",
                            chat_id,
                            media_id,
                            baseline,
                        )
                    else:
                        # A raw A/V source swap may reset the native clock. Adopt
                        # the reset value, then require a subsequent advance.
                        if outgoing_time < baseline:
                            baseline = max(0, outgoing_time)
                        elif outgoing_time > baseline and not packet_proved:
                            packet_proved = True
                            self._log_direct_startup_event(
                                "first_telegram_audio_packet_sent",
                                chat_id=chat_id,
                                media_id=media_id,
                                evidence="ntgcalls_outgoing_clock_advanced_after_real_pcm",
                                outgoing_clock=outgoing_time,
                                status="observed",
                                detail=f"baseline={baseline};real_pcm=1",
                            )
                            if trace:
                                trace.mark("first_telegram_audio_packet")

                if packet_proved and session["unmute_confirmed"].is_set():
                    if trace:
                        trace.mark("audible")
                        trace.mark("voice_started")

                    current = queue.get_current(chat_id)
                    if str(getattr(current, "id", "") or "") == media_id:
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
                    return

                if session["unmute_failed"].is_set():
                    if trace:
                        trace.finish("assistant_unmute_failed")
                    return

                elapsed = time.perf_counter() - started
                if elapsed >= 12.0 and not missed_target_logged:
                    missed_target_logged = True
                    self._log_direct_startup_event(
                        "first_packet_target_missed",
                        chat_id=chat_id,
                        media_id=media_id,
                        evidence="real_pcm_clock_or_unmute_12s_deadline",
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
                evidence="real_pcm_clock_or_unmute_30s_deadline",
                status=play_state.get("status", "pending"),
            )
            if trace:
                trace.finish("startup_observer_timeout")
        finally:
            if event_file is not None:
                try:
                    event_file.unlink(missing_ok=True)
                except OSError:
                    pass

    TgCall._observe_initial_direct_media = observe_initial_direct_media
    setattr(TgCall, _SENTINEL, True)
    logger.info(
        "sub3_real_pcm_proof_patch enabled=1 "
        "proof=real_pcm+post_submit_clock_advance+confirmed_unmute "
        "video_attach_required=0"
    )


_install()
