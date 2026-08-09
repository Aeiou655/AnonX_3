# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
"""YouTube transport + <=3s resolver critical-path tuning.

Production evidence after the proxy fix showed:

* the auto search proxy is useful, but yt-dlp must stay direct;
* all three player-response micro lanes miss (authenticated=unplayable or
  anonymous=login_required), so they only add concurrent YouTube load;
* two full extractor hedges finish at roughly the same 3.2-3.4s, while the
  video-lightweight loser can remain alive for minutes; and
* startup warm constructs the audio profile on a different sticky worker than
  the foreground race, so the live winner still reports ydl_warm=0.

This plugin keeps the safe authenticated mweb/POT fallback but makes the cold
critical path one deterministic fast extractor per media mode, prewarmed on the
exact worker that the race will use. Robust profiles remain available if the
fast source fails validation.
"""

from __future__ import annotations

import asyncio
import os
import time

from AnonX_3 import config, logger
from AnonX_3.core.youtube import YouTube


_PROXY_SENTINEL = "_anonx_ytdlp_auto_proxy_compat_bypass_v1"
_SUB3_SENTINEL = "_anonx_sub3_resolver_critical_path_v1"


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _proxy_enabled() -> bool:
    return _flag("YTDLP_AUTO_PROXY_COMPAT_BYPASS", True)


def _sub3_enabled() -> bool:
    return _flag("DIRECT_SUB3_MODE", True)


def _youtube_player_skip(opts: dict) -> set[str]:
    try:
        extractor_args = opts.get("extractor_args") or {}
        youtube_args = extractor_args.get("youtube") or {}
        return {str(item) for item in (youtube_args.get("player_skip") or [])}
    except Exception:
        return set()


def _install_proxy_patch() -> None:
    if getattr(YouTube, _PROXY_SENTINEL, False):
        return

    original_base = YouTube._build_ytdlp_base_api_opts
    original_cli = YouTube.build_ytdlp_cli_args

    def _auto_mode() -> bool:
        return (
            _proxy_enabled()
            and str(getattr(config, "YOUTUBE_PROXY_MODE", "") or "")
            .strip()
            .lower()
            == "auto"
        )

    def _build_ytdlp_base_api_opts_direct_on_auto(self, *args, **kwargs):
        result = original_base(self, *args, **kwargs)
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

            # Empty is yt-dlp's explicit direct-connection override. Removing
            # the key is insufficient because auto discovery also exports
            # ALL_PROXY/PROXY_URL into the process environment.
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

    def _build_ytdlp_cli_args_direct_on_auto(self, *args, **kwargs):
        cli = list(original_cli(self, *args, **kwargs))
        if not _auto_mode() or kwargs.get("include_proxy", True) is False:
            return cli

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

    YouTube._build_ytdlp_base_api_opts = _build_ytdlp_base_api_opts_direct_on_auto
    YouTube.build_ytdlp_cli_args = _build_ytdlp_cli_args_direct_on_auto
    setattr(YouTube, _PROXY_SENTINEL, True)
    logger.info(
        "youtube_transport_fastpath_patch enabled auto_proxy_ytdlp_bypass=%s "
        "explicit_direct_override=1",
        int(_proxy_enabled()),
    )


def _apply_sub3_runtime_defaults() -> None:
    if not _sub3_enabled():
        return

    # Production showed every micro candidate missing while consuming three
    # additional player requests beside the authoritative resolver. Stop
    # launching them on the visible path; the authenticated full resolver is the
    # proven source of valid 206 URLs.
    setattr(config, "DIRECT_RESOLVER_PARALLEL_MICRO", False)

    # The audio escape profile is almost identical to foreground_fast and in the
    # live trace finished at the same 3.398s. One request gives the winner the
    # whole connection/CPU budget; robust audio remains a fallback.
    setattr(config, "DIRECT_AUDIO_ESCAPE_RACE", False)

    # Two 10ms frames are enough to start the EXTERNAL pump. Four-frame priming
    # measured ~0.3s on the VPS and added avoidable source-ready latency.
    current_frames = int(getattr(config, "DIRECT_EXTERNAL_PREBUFFER_FRAMES", 4) or 4)
    setattr(config, "DIRECT_EXTERNAL_PREBUFFER_FRAMES", min(current_frames, 2))


def _install_sub3_resolver_patch() -> None:
    if getattr(YouTube, _SUB3_SENTINEL, False):
        return

    _apply_sub3_runtime_defaults()
    original_extract = YouTube._run_persistent_direct_extract
    original_warm = YouTube.warm_direct_resolver_runtime

    async def _run_sub3_persistent_direct_extract(
        self,
        url: str,
        opts: dict,
        *,
        background140: bool = False,
        resolver_slot_hint: int | None = None,
    ):
        if _sub3_enabled() and not background140:
            fmt = str((opts or {}).get("format") or "")
            player_skip = _youtube_player_skip(opts or {})
            lightweight = "webpage" in player_skip

            # video_lightweight was observed continuing for ~350s after the
            # 3.2s video_escape winner had already played. Do not start that
            # known slow duplicate on the foreground critical path. The robust
            # profile has the same format but not the lightweight webpage-skip
            # marker, so it remains available if video_escape fails.
            if lightweight and fmt.startswith("best[ext=mp4]"):
                executors = getattr(self, "_direct_resolver_executors", ())
                count = max(1, len(executors))
                slot = int(resolver_slot_hint or 0) % count
                if not getattr(self, "_sub3_video_light_suppressed_logged", False):
                    logger.info(
                        "direct_sub3_lane_suppressed lane=video_lightweight "
                        "reason=known_slow_duplicate robust_fallback_retained=1"
                    )
                    self._sub3_video_light_suppressed_logged = True
                return None, True, 0, slot

        return await original_extract(
            self,
            url,
            opts,
            background140=background140,
            resolver_slot_hint=resolver_slot_hint,
        )

    async def _prepare_on_exact_slot(self, opts: dict, slot: int):
        loop = asyncio.get_running_loop()
        executors = getattr(self, "_direct_resolver_executors", ())
        if not executors:
            return False, -1, 0
        selected = int(slot) % len(executors)
        started = time.monotonic()
        warm, inner_ms = await loop.run_in_executor(
            executors[selected], self._persistent_direct_prepare, opts
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return bool(warm), max(int(inner_ms), elapsed_ms), selected

    async def _warm_sub3_resolver_runtime(self) -> None:
        await original_warm(self)
        if not _sub3_enabled():
            return

        profiles: list[tuple[str, dict, int]] = []
        try:
            audio_base = self.build_ytdlp_api_opts(
                action="direct", video_id=None, socket_timeout=4
            )
            audio_base["format"] = "18/bestaudio[ext=m4a]/bestaudio/best"
            audio_fast = self._authoritative_pot_opts(
                audio_base, lightweight=True, fast_progressive=True
            )
            # foreground_fast is prestarted with resolver_slot_hint=0.
            profiles.append(("audio-foreground-fast", audio_fast, 0))
        except Exception as ex:
            logger.warning(
                "direct_sub3_pinned_warm skipped profile=audio err=%s",
                type(ex).__name__,
            )

        try:
            video_base = self.build_ytdlp_api_opts(
                action="direct", video_id=None, socket_timeout=4
            )
            video_base["format"] = (
                "18/best[ext=mp4][acodec!=none][vcodec!=none][height<=?360]/"
                "best[acodec!=none][vcodec!=none][height<=?360]"
            )
            video_escape = self._authoritative_pot_opts(
                video_base, lightweight=True, fast_progressive=True
            )
            # video_escape_fast is profile index 1 and therefore slot_hint=1.
            profiles.append(("video-escape-fast", video_escape, 1))
        except Exception as ex:
            logger.warning(
                "direct_sub3_pinned_warm skipped profile=video err=%s",
                type(ex).__name__,
            )

        if not profiles:
            return
        results = await asyncio.gather(
            *(
                _prepare_on_exact_slot(self, opts, slot)
                for _label, opts, slot in profiles
            ),
            return_exceptions=True,
        )
        ready: list[str] = []
        for (label, _opts, _slot), result in zip(profiles, results):
            if isinstance(result, Exception):
                ready.append(f"{label}:error={type(result).__name__}")
                continue
            warm, elapsed_ms, slot = result
            ready.append(
                f"{label}:slot{slot}:preexisting={int(bool(warm))}:ms={elapsed_ms}"
            )
        logger.info(
            "direct_sub3_pinned_warm ready=%s single_fast_lane=1 micro_runtime=0",
            ",".join(ready),
        )

    YouTube._run_persistent_direct_extract = _run_sub3_persistent_direct_extract
    YouTube.warm_direct_resolver_runtime = _warm_sub3_resolver_runtime
    setattr(YouTube, _SUB3_SENTINEL, True)
    logger.info(
        "youtube_sub3_resolver_patch enabled=%s micro_runtime=0 "
        "audio_escape=0 video_lightweight=0 pinned_warm=1 prebuffer_frames=%s",
        int(_sub3_enabled()),
        int(getattr(config, "DIRECT_EXTERNAL_PREBUFFER_FRAMES", 2) or 2),
    )


_install_proxy_patch()
_install_sub3_resolver_patch()
