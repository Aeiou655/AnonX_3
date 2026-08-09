# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
"""Production tail recovery for the <=3s cold YouTube path.

Two independent production failures are covered here:

* a warm persistent mweb worker can still spend ~12s inside yt-dlp even with
  retries disabled; an isolated short-lived CLI lane races it and can return a
  progressive, 206-proven source without waiting for the stuck worker; and
* NTgCalls can reject the immediate post-reset speculative EXTERNAL reconnect.
  The existing startup fastpath retries at zero settle delay, which can fail in
  a few milliseconds and close the reusable EXTERNAL session. A bounded native
  settle retry revives only a decoder-less provisional session and retries while
  YouTube resolution is still running.

This module is named ``zz_*`` so plugin discovery loads it after the existing
startup/youtube fastpaths. All paths are guarded and preserve the stock fallback.
"""

from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlparse

from ntgcalls import ConnectionError, ConnectionNotFound

from AnonX_3 import config, logger
from AnonX_3.core.calls import TgCall
from AnonX_3.core.youtube import DirectStreamSource, YouTube


_RESOLVER_SENTINEL = "_anonx_sub3_cli_tail_escape_v1"
_PRECONNECT_SENTINEL = "_anonx_sub3_native_settle_retry_v1"


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _float(name: str, default: float, low: float, high: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None and raw.strip() else float(default)
    except (TypeError, ValueError):
        value = float(default)
    return max(low, min(high, value))


def _sanitize_field(value: str, default: str = "") -> str:
    text = str(value or "").strip()
    return default if text in {"", "NA", "None", "none"} else text


def _strip_cli_pair(args: list[str], key: str) -> list[str]:
    out: list[str] = []
    idx = 0
    while idx < len(args):
        if args[idx] == key:
            idx += 2
            continue
        out.append(args[idx])
        idx += 1
    return out


async def _kill_process(process) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=0.25)
    except Exception:
        pass


def _install_resolver_escape() -> None:
    if getattr(YouTube, _RESOLVER_SENTINEL, False):
        return

    original_uncached = YouTube._resolve_direct_stream_source_uncached

    async def _cli_escape_source(
        self: YouTube,
        video_id: str,
        *,
        video: bool,
        quality_tier: str | None,
    ) -> DirectStreamSource | None:
        if not _flag("DIRECT_SUB3_CLI_ESCAPE", True):
            return None

        delay = _float("DIRECT_SUB3_CLI_ESCAPE_DELAY_SEC", 0.08, 0.0, 0.50)
        if delay:
            await asyncio.sleep(delay)

        process = None
        started = time.monotonic()
        try:
            socket_timeout = max(
                1,
                int(_float("DIRECT_SUB3_CLI_SOCKET_TIMEOUT_SEC", 1.0, 1.0, 2.0)),
            )
            args = list(
                self.build_ytdlp_cli_args(
                    action="direct",
                    video_id=video_id,
                    socket_timeout=socket_timeout,
                    skip_download=True,
                    include_proxy=True,
                    validate_cookie=True,
                )
            )
            fast_opts = self.build_ytdlp_api_opts(
                action="direct",
                video_id=video_id,
                socket_timeout=socket_timeout,
                skip_download=True,
                include_proxy=True,
                validate_cookie=True,
            )
            if video:
                fmt = (
                    "18/best[ext=mp4][acodec!=none][vcodec!=none][height<=?360]/"
                    "best[acodec!=none][vcodec!=none][height<=?360]"
                )
            else:
                fmt = "18/bestaudio[ext=m4a]/bestaudio/best"
            fast_opts["format"] = fmt
            fast_opts = self._authoritative_pot_opts(
                fast_opts,
                lightweight=True,
                fast_progressive=True,
            )

            args = _strip_cli_pair(args, "--extractor-args")
            extractor_cli = self._extractor_args_to_cli(
                fast_opts.get("extractor_args") or {}
            )
            if extractor_cli:
                args.extend(["--extractor-args", extractor_cli])

            template = (
                "%(url)s\x1f%(format_id)s\x1f%(ext)s\x1f%(acodec)s\x1f"
                "%(vcodec)s\x1f%(protocol)s\x1f%(abr)s"
            )
            args.extend(
                [
                    "--no-warnings",
                    "--no-playlist",
                    "--extractor-retries",
                    "0",
                    "--retries",
                    "0",
                    "-f",
                    fmt,
                    "--print",
                    template,
                    self.base + video_id,
                ]
            )

            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            extract_timeout = _float(
                "DIRECT_SUB3_CLI_ESCAPE_EXTRACT_TIMEOUT_SEC",
                1.35,
                0.70,
                2.20,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=extract_timeout
                )
            except asyncio.TimeoutError:
                await _kill_process(process)
                logger.info(
                    "direct_sub3_cli_escape timeout video_id=%s video=%s "
                    "timeout_ms=%s action=authoritative_continues",
                    video_id,
                    int(bool(video)),
                    int(extract_timeout * 1000),
                )
                return None

            if process.returncode != 0:
                detail = (stderr or b"").decode("utf-8", "replace").strip()
                logger.info(
                    "direct_sub3_cli_escape miss video_id=%s video=%s rc=%s "
                    "elapsed_ms=%s detail=%s",
                    video_id,
                    int(bool(video)),
                    process.returncode,
                    int((time.monotonic() - started) * 1000),
                    detail[-140:].replace("\n", " "),
                )
                return None

            lines = [
                line.strip()
                for line in (stdout or b"").decode("utf-8", "replace").splitlines()
                if line.strip()
            ]
            if not lines:
                return None
            fields = lines[-1].split("\x1f")
            if len(fields) < 7:
                logger.info(
                    "direct_sub3_cli_escape miss video_id=%s video=%s "
                    "reason=unparseable_output fields=%s",
                    video_id,
                    int(bool(video)),
                    len(fields),
                )
                return None

            stream_url = fields[0].strip()
            if not self._is_direct_media_url(stream_url):
                return None
            format_id = _sanitize_field(fields[1], "?")
            ext = _sanitize_field(fields[2], "mp4")
            acodec = _sanitize_field(fields[3], "mp4a.40.2")
            vcodec = _sanitize_field(fields[4], "none")
            protocol = _sanitize_field(fields[5], urlparse(stream_url).scheme or "https")
            abr_raw = _sanitize_field(fields[6], "")
            try:
                abr = float(abr_raw) if abr_raw else ""
            except (TypeError, ValueError):
                abr = abr_raw

            if not acodec or acodec == "none":
                return None
            if video and (not vcodec or vcodec == "none"):
                return None

            source = DirectStreamSource(
                url=stream_url,
                local_path=self.get_download_filename(
                    video_id,
                    video=video,
                    quality_tier=quality_tier,
                ),
                headers=self._direct_stream_headers({}, None),
                proxy="",
                format_id=format_id,
                ext=ext,
                acodec=acodec,
                vcodec=vcodec,
                protocol=protocol,
                abr=abr,
                audio_format="/".join(part for part in (ext, acodec) if part),
                host=self._direct_stream_host(stream_url),
                video=bool(video),
                reason="sub3_cli_escape",
                client=str(getattr(config, "PO_TOKEN_CLIENT", "mweb") or "mweb"),
                preflight_status=0,
                pot_bound=False,
                pot_provenance="cli_pending_probe",
            )

            probe_timeout = _float(
                "DIRECT_SUB3_CLI_ESCAPE_PROBE_TIMEOUT_SEC",
                0.28,
                0.12,
                0.60,
            )
            try:
                status = await asyncio.wait_for(
                    self._probe_direct_source_status(
                        source,
                        timeout=probe_timeout,
                    ),
                    timeout=probe_timeout + 0.08,
                )
            except asyncio.TimeoutError:
                status = 0
            if status not in (200, 206):
                logger.info(
                    "direct_sub3_cli_escape miss video_id=%s video=%s "
                    "reason=preflight status=%s elapsed_ms=%s",
                    video_id,
                    int(bool(video)),
                    status,
                    int((time.monotonic() - started) * 1000),
                )
                return None

            source = DirectStreamSource(
                **{
                    **source.__dict__,
                    "preflight_status": int(status),
                    "pot_provenance": "cli_gvs_206",
                }
            )
            logger.info(
                "direct_sub3_cli_escape ready video_id=%s video=%s format_id=%s "
                "status=%s elapsed_ms=%s host=%s",
                video_id,
                int(bool(video)),
                source.format_id,
                status,
                int((time.monotonic() - started) * 1000),
                source.host,
            )
            return source
        except asyncio.CancelledError:
            await _kill_process(process)
            raise
        except Exception as ex:
            await _kill_process(process)
            logger.info(
                "direct_sub3_cli_escape miss video_id=%s video=%s reason=%s "
                "elapsed_ms=%s action=authoritative_continues",
                video_id,
                int(bool(video)),
                type(ex).__name__,
                int((time.monotonic() - started) * 1000),
            )
            return None

    async def _resolve_with_cli_tail_escape(
        self: YouTube,
        video_id: str,
        video: bool = False,
        quality_tier: str | None = None,
        allow_local_ready: bool = True,
        allow_authoritative_retry: bool = True,
        exact_audio140: bool = False,
    ) -> DirectStreamSource:
        if (
            not _flag("DIRECT_SUB3_MODE", True)
            or not _flag("DIRECT_SUB3_CLI_ESCAPE", True)
            or exact_audio140
        ):
            return await original_uncached(
                self,
                video_id,
                video=video,
                quality_tier=quality_tier,
                allow_local_ready=allow_local_ready,
                allow_authoritative_retry=allow_authoritative_retry,
                exact_audio140=exact_audio140,
            )

        primary = asyncio.create_task(
            original_uncached(
                self,
                video_id,
                video=video,
                quality_tier=quality_tier,
                allow_local_ready=allow_local_ready,
                allow_authoritative_retry=allow_authoritative_retry,
                exact_audio140=exact_audio140,
            ),
            name=f"sub3-primary-resolver:{video_id}:{int(bool(video))}",
        )
        escape = asyncio.create_task(
            _cli_escape_source(
                self,
                video_id,
                video=bool(video),
                quality_tier=quality_tier,
            ),
            name=f"sub3-cli-escape:{video_id}:{int(bool(video))}",
        )
        labels = {primary: "authoritative", escape: "cli_escape"}
        pending = set(labels)
        hard_deadline = time.monotonic() + _float(
            "DIRECT_SUB3_RESOLVER_HEDGE_WINDOW_SEC",
            1.75,
            1.10,
            2.40,
        )

        try:
            while pending:
                remaining = hard_deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                for task in done:
                    try:
                        source = task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as ex:
                        logger.info(
                            "direct_sub3_resolver_lane_failed video_id=%s video=%s "
                            "lane=%s reason=%s",
                            video_id,
                            int(bool(video)),
                            labels[task],
                            type(ex).__name__,
                        )
                        continue
                    if not isinstance(source, DirectStreamSource) or not source.url:
                        continue

                    lane = labels[task]
                    for loser in pending:
                        loser.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    logger.info(
                        "direct_sub3_resolver_winner video_id=%s video=%s lane=%s "
                        "status=%s tail_hedge=1",
                        video_id,
                        int(bool(video)),
                        lane,
                        int(getattr(source, "preflight_status", 0) or 0),
                    )
                    return source
        except asyncio.CancelledError:
            for task in (primary, escape):
                if not task.done():
                    task.cancel()
            await asyncio.gather(primary, escape, return_exceptions=True)
            raise

        if not escape.done():
            escape.cancel()
            await asyncio.gather(escape, return_exceptions=True)
        logger.warning(
            "direct_sub3_resolver_target_miss video_id=%s video=%s "
            "hedge_window_ms=%s action=await_authoritative",
            video_id,
            int(bool(video)),
            int(
                _float(
                    "DIRECT_SUB3_RESOLVER_HEDGE_WINDOW_SEC",
                    1.75,
                    1.10,
                    2.40,
                )
                * 1000
            ),
        )
        return await primary

    YouTube._resolve_direct_stream_source_uncached = _resolve_with_cli_tail_escape
    setattr(YouTube, _RESOLVER_SENTINEL, True)
    logger.info(
        "sub3_cli_tail_escape_patch enabled=1 lane=isolated_ytdlp_cli "
        "progressive=1 validated_206=1 hedge_window_ms=%s",
        int(
            _float(
                "DIRECT_SUB3_RESOLVER_HEDGE_WINDOW_SEC",
                1.75,
                1.10,
                2.40,
            )
            * 1000
        ),
    )


def _revive_provisional_session(self: TgCall, session: dict, chat_id: int) -> bool:
    if not session or session.get("process") is not None or session.get("activated"):
        return False
    try:
        session["closed"] = False
        session["error"] = ""
        session["connected_ns"] = 0
        session["first_frame_accepted_ns"] = 0
        session["jit_kick_accepted"] = False
        session["activated"] = False
        for key in ("connected", "first_frame_accepted"):
            event = session.get(key)
            clear = getattr(event, "clear", None)
            if callable(clear):
                clear()
        self._direct_external_audio_sessions[int(chat_id)] = session
        return True
    except Exception:
        return False


def _install_preconnect_settle_retry() -> None:
    if getattr(TgCall, _PRECONNECT_SENTINEL, False):
        return

    original_play = TgCall._play_with_startup_slot

    async def _play_with_native_settle_retry(
        self: TgCall,
        client,
        *,
        chat_id: int,
        stream,
        unmute_mode: str = "background",
        reserved_slot=None,
        startup_media_id: str | None = None,
        external_audio_session: dict | None = None,
    ) -> None:
        try:
            return await original_play(
                self,
                client,
                chat_id=chat_id,
                stream=stream,
                unmute_mode=unmute_mode,
                reserved_slot=reserved_slot,
                startup_media_id=startup_media_id,
                external_audio_session=external_audio_session,
            )
        except (ConnectionNotFound, ConnectionError) as first_ex:
            if (
                external_audio_session is None
                or not _flag("DIRECT_SUB3_NATIVE_SETTLE_RETRY", True)
            ):
                raise

            last_ex: BaseException = first_ex
            settles = (
                _float("DIRECT_SUB3_NATIVE_SETTLE_FIRST_SEC", 0.12, 0.05, 0.30),
                _float("DIRECT_SUB3_NATIVE_SETTLE_SECOND_SEC", 0.28, 0.10, 0.55),
            )
            for attempt, settle in enumerate(settles, 1):
                if not _revive_provisional_session(
                    self,
                    external_audio_session,
                    int(chat_id),
                ):
                    break
                logger.info(
                    "direct_preconnect_native_settle_retry chat_id=%s media_id=%s "
                    "attempt=%s settle_ms=%s resolver_overlap=1 session_revived=1",
                    chat_id,
                    startup_media_id,
                    attempt,
                    int(settle * 1000),
                )
                await asyncio.sleep(settle)
                try:
                    await original_play(
                        self,
                        client,
                        chat_id=chat_id,
                        stream=stream,
                        unmute_mode=unmute_mode,
                        reserved_slot=None,
                        startup_media_id=startup_media_id,
                        external_audio_session=external_audio_session,
                    )
                    logger.info(
                        "direct_preconnect_native_settle_connected chat_id=%s "
                        "media_id=%s attempt=%s reconnect_on_critical_path=0",
                        chat_id,
                        startup_media_id,
                        attempt,
                    )
                    return
                except (ConnectionNotFound, ConnectionError) as ex:
                    last_ex = ex
                    continue

            raise last_ex

    TgCall._play_with_startup_slot = _play_with_native_settle_retry
    setattr(TgCall, _PRECONNECT_SENTINEL, True)
    logger.info(
        "sub3_native_settle_patch enabled=1 retries=2 "
        "settle_ms=%s,%s session_revive=decoderless_only",
        int(_float("DIRECT_SUB3_NATIVE_SETTLE_FIRST_SEC", 0.12, 0.05, 0.30) * 1000),
        int(_float("DIRECT_SUB3_NATIVE_SETTLE_SECOND_SEC", 0.28, 0.10, 0.55) * 1000),
    )


_install_resolver_escape()
_install_preconnect_settle_retry()
