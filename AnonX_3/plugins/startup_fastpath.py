# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
"""Targeted startup hotfixes for VC startup and YouTube transport compatibility."""

from __future__ import annotations

import asyncio
import os
import time

from ntgcalls import ConnectionError, ConnectionNotFound, MediaSource
from pytgcalls import types

from AnonX_3 import config, logger
from AnonX_3.core.calls import TgCall
from AnonX_3.core.resource_manager import resource_manager
from AnonX_3.core.youtube import YouTube

_PATCH_SENTINEL = "_anonx_vplay_cold_start_deep_fix_v3411"
_YTDLP_PROXY_SENTINEL = "_anonx_ytdlp_auto_proxy_compat_bypass_v1"


def _enabled(name: str, default: bool = True) -> bool:
    return bool(getattr(config, name, default))


def _ytdlp_auto_proxy_bypass_enabled() -> bool:
    raw = os.getenv("YTDLP_AUTO_PROXY_COMPAT_BYPASS", "true").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _install_ytdlp_transport_patch() -> None:
    """Keep auto proxy for search, but force yt-dlp itself to direct transport.

    config.py intentionally exports the detected local proxy through PROXY_URL /
    ALL_PROXY. Omitting yt-dlp's proxy option is therefore not enough: yt-dlp
    can still inherit the environment proxy. Its documented explicit empty
    proxy value means direct connection, so set ``proxy=''`` for Python API
    options and ``--proxy ''`` for CLI invocations when the configured mode is
    auto. Explicit operator proxies and YOUTUBE_PROXY=off remain untouched.
    """

    if getattr(YouTube, _YTDLP_PROXY_SENTINEL, False):
        return

    original_base_opts = YouTube._build_ytdlp_base_api_opts
    original_cli_args = YouTube.build_ytdlp_cli_args

    def _auto_mode() -> bool:
        return (
            _ytdlp_auto_proxy_bypass_enabled()
            and str(getattr(config, "YOUTUBE_PROXY_MODE", "") or "").strip().lower()
            == "auto"
        )

    def build_ytdlp_base_api_opts_direct_on_auto(self, *args, **kwargs):
        result = original_base_opts(self, *args, **kwargs)
        if not _auto_mode():
            return result

        try:
            opts, js_runtime_cli, pot_provider_url = result
            if not isinstance(opts, dict):
                return result

            detected = str(
                opts.get("proxy")
                or getattr(self, "_youtube_proxy", "")
                or getattr(config, "YOUTUBE_PROXY", "")
                or ""
            ).strip()

            # Explicit empty proxy is yt-dlp's direct-connection override. Do
            # not merely remove the key: config.py may have exported ALL_PROXY.
            opts["proxy"] = ""

            if not getattr(self, "_ytdlp_auto_proxy_bypass_logged", False):
                logger.warning(
                    "youtube_ytdlp_auto_proxy_bypass proxy=%s transport=direct "
                    "override=explicit_empty search_proxy_retained=1",
                    detected.split("@")[-1] if detected else "auto",
                )
                self._ytdlp_auto_proxy_bypass_logged = True
            return opts, js_runtime_cli, pot_provider_url
        except Exception as ex:
            logger.debug(
                "youtube_ytdlp_auto_proxy_bypass skipped err=%s",
                type(ex).__name__,
            )
            return result

    def build_ytdlp_cli_args_direct_on_auto(self, *args, **kwargs):
        cli = list(original_cli_args(self, *args, **kwargs))
        if not _auto_mode() or kwargs.get("include_proxy", True) is False:
            return cli

        # The patched base options already suppress the normal truthy --proxy
        # append. Still normalize defensively in case call signatures change.
        cleaned: list[str] = []
        idx = 0
        while idx < len(cli):
            if cli[idx] == "--proxy":
                idx += 2
                continue
            cleaned.append(cli[idx])
            idx += 1
        cleaned.extend(["--proxy", ""])
        return cleaned

    YouTube._build_ytdlp_base_api_opts = build_ytdlp_base_api_opts_direct_on_auto
    YouTube.build_ytdlp_cli_args = build_ytdlp_cli_args_direct_on_auto
    setattr(YouTube, _YTDLP_PROXY_SENTINEL, True)
    logger.info(
        "youtube_transport_fastpath_patch enabled auto_proxy_ytdlp_bypass=%s "
        "explicit_direct_override=1",
        int(_ytdlp_auto_proxy_bypass_enabled()),
    )


async def _hard_reset_failed_binding(
    self: TgCall, client, chat_id: int, reason: str
) -> None:
    """Remove a half-created NTgCalls call before a fresh direct retry."""

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
            speculative_external_failure = external_audio_session is not None
            direct_retry = bool(
                external_audio_session is None
                and startup_media_id
                and isinstance(stream, types.raw.Stream)
                and _enabled("DIRECT_COLD_BINDING_RETRY", True)
            )

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

    TgCall._build_initial_direct_raw_stream = build_initial_direct_raw_stream
    TgCall._wait_direct_external_packet_clock = wait_direct_external_packet_clock
    TgCall._close_direct_external_audio = close_direct_external_audio
    TgCall._play_with_startup_slot = play_with_startup_slot
    setattr(TgCall, _PATCH_SENTINEL, True)

    logger.info(
        "startup_fastpath_patch enabled vplay_hybrid_audio=1 cold_binding_retry=1"
    )


_install_ytdlp_transport_patch()
_install_patch()
