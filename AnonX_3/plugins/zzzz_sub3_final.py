# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
"""Final cold-start critical-path patch for the <=3s YouTube target.

Production evidence:
- foreground audio/video selects progressive itag 18 and validates HTTP 206
  without a visible POT token;
- the bgutil sidecar can answer /ping while actual BotGuard/POT generation fails;
- the isolated CLI hedge times out at 1.35s on every observed request and only
  competes with the proven persistent resolver; and
- the existing private player-response path is substantially cheaper than full
  extract_info, but previous micro clients were incompatible with authenticated
  cookies. mweb supports the configured cookies and can expose POT-exempt itag18.

This patch therefore:
1) keeps the provider configured as a fallback contract, but strips it from
   non-140 foreground/direct and download extraction so BotGuard failures cannot
   sit on the startup path;
2) disables exact-140 background promotion while the <=3s mode is active;
3) disables the repeatedly-timing-out subprocess hedge;
4) races one authenticated mweb player-response request against the full
   resolver; and
5) skips py_yt metadata lookup completely for explicit 11-char YouTube URLs,
   starting direct prewarm immediately.

All changes are runtime-guarded and preserve the full resolver fallback.
"""

from __future__ import annotations

import asyncio
import os

from AnonX_3 import config, logger
from AnonX_3.core.youtube import YouTube
from AnonX_3.core.bot_api import BotAPI
from AnonX_3.core.provider.po_token import PoTokenProvider
from AnonX_3.helpers import Track

_SENTINEL = "_anonx_sub3_final_critical_path_v1"
_PROVIDER_KEYS = ("youtubepot-bgutilhttp", "youtubepot-bgutilscript")
_DOWNLOAD_NO_POT_MARKER = "_anonx_disable_pot_for_download"


def _enabled() -> bool:
    raw = os.getenv("DIRECT_SUB3_FINAL_MODE")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _strip_provider_opts(opts: dict) -> dict:
    out = dict(opts)
    extractor_args = dict(out.get("extractor_args") or {})
    changed = False
    for key in _PROVIDER_KEYS:
        if key in extractor_args:
            extractor_args.pop(key, None)
            changed = True

    youtube_args = dict(extractor_args.get("youtube") or {})
    if "po_token" in youtube_args:
        youtube_args.pop("po_token", None)
        changed = True
    if youtube_args:
        extractor_args["youtube"] = youtube_args
    else:
        extractor_args.pop("youtube", None)

    if extractor_args:
        out["extractor_args"] = extractor_args
    else:
        out.pop("extractor_args", None)
    if changed:
        out["_anonx_pot_bypassed"] = True
    return out


def _strip_provider_cli(args: list[str]) -> list[str]:
    cleaned: list[str] = []
    idx = 0
    while idx < len(args):
        item = args[idx]
        if item == "--extractor-args" and idx + 1 < len(args):
            parts = [
                part
                for part in str(args[idx + 1]).split(";")
                if part
                and not any(part.startswith(f"{key}:") for key in _PROVIDER_KEYS)
            ]
            if parts:
                cleaned.extend([item, ";".join(parts)])
            idx += 2
            continue
        cleaned.append(item)
        idx += 1
    return cleaned


def _apply_runtime_defaults() -> None:
    # One cookie-compatible player-response request is the cheap hedge. The old
    # three anonymous/incompatible clients were proven misses in production.
    setattr(config, "DIRECT_RESOLVER_PARALLEL_MICRO", True)
    setattr(config, "DIRECT_MWEB_MICRO_PLAYER", True)
    setattr(config, "DIRECT_MICRO_PLAYER_CLIENTS", ("mweb",))
    setattr(config, "DIRECT_MICRO_TOTAL_BUDGET_SEC", 1.20)
    setattr(config, "DIRECT_MICRO_LANE_TIMEOUT_SEC", 0.95)
    setattr(config, "DIRECT_MICRO_PROBE_TIMEOUT_SEC", 0.20)

    # Exact 140 is quality promotion only, never a startup prerequisite. The
    # current bgutil sidecar is reachable but can fail BotGuard token minting,
    # so do not generate background POT traffic in sub-3 mode.
    setattr(config, "DIRECT_BACKGROUND_140_ENABLED", False)

    # The observed isolated CLI lane timed out at 1350ms for every audio/video
    # request and consumes an extra yt-dlp process. The already-installed tail
    # wrapper reads this env flag at request time, so disabling it here is safe
    # even though that plugin loaded earlier.
    os.environ["DIRECT_SUB3_CLI_ESCAPE"] = "0"

    # Bound GVS proof without changing the success criterion (still 200/206).
    current_probe = float(
        getattr(config, "DIRECT_AUTHORITATIVE_POT_PREFLIGHT_TIMEOUT_SEC", 1.5)
        or 1.5
    )
    setattr(
        config,
        "DIRECT_AUTHORITATIVE_POT_PREFLIGHT_TIMEOUT_SEC",
        min(current_probe, 0.45),
    )


def _install_quiet_stale_delete() -> None:
    """Treat already-gone cleanup messages as an idempotent delete success.

    Bot API returns application 400 for an already deleted status card. Cleanup
    is intentionally multi-owner, so this state is expected and should not be
    emitted at ERROR severity. Other Bot API failures retain the stock path.
    """
    sentinel = "_anonx_quiet_stale_delete_v1"
    if getattr(BotAPI, sentinel, False):
        return
    original_request = BotAPI._request

    async def _request(self: BotAPI, method: str, payload: dict | None = None):
        if method != "deleteMessage":
            return await original_request(self, method, payload)

        session = await self._get_session()
        url = f"{self.base_url}/{method}"
        try:
            async with session.post(url, json=payload or {}) as resp:
                data = await resp.json(content_type=None)
        except self._NETWORK_ERRORS:
            return await original_request(self, method, payload)
        except Exception:
            return await original_request(self, method, payload)

        if data.get("ok"):
            return data.get("result")

        desc = str(data.get("description") or "")
        normalized = desc.casefold()
        if int(data.get("error_code") or 0) == 400 and (
            "message to delete not found" in normalized
            or "message_id_invalid" in normalized
            or "message identifier is not specified" in normalized
        ):
            logger.debug(
                "Bot API deleteMessage stale cleanup ignored chat_id=%s message_id=%s",
                (payload or {}).get("chat_id"),
                (payload or {}).get("message_id"),
            )
            return False

        # Preserve stock classification/retries for every non-idempotent error.
        return await original_request(self, method, payload)

    BotAPI._request = _request
    setattr(BotAPI, sentinel, True)


def _install_provider_download_bypass() -> None:
    """Stop download retry code from re-injecting the broken POT sidecar."""
    sentinel = "_anonx_download_pot_bypass_v1"
    if getattr(PoTokenProvider, sentinel, False):
        return

    original_sync = PoTokenProvider.apply_to_ydl_opts_sync
    original_async = PoTokenProvider.apply_to_ydl_opts

    @staticmethod
    def _consume_marker(opts: dict) -> tuple[dict, bool]:
        out = dict(opts)
        bypass = bool(out.pop(_DOWNLOAD_NO_POT_MARKER, False))
        if bypass:
            out = _strip_provider_opts(out)
            out.pop("_anonx_pot_bypassed", None)
        return out, bypass

    def _sync(self: PoTokenProvider, opts: dict, *, video_id: str | None = None):
        cleaned, bypass = _consume_marker(opts)
        if bypass:
            return cleaned
        return original_sync(self, cleaned, video_id=video_id)

    async def _async(
        self: PoTokenProvider,
        opts: dict,
        *,
        video_id: str | None = None,
    ):
        cleaned, bypass = _consume_marker(opts)
        if bypass:
            logger.debug(
                "po_token download bypass video_id=%s reason=sub3_progressive_cache",
                video_id or "",
            )
            return cleaned
        return await original_async(self, cleaned, video_id=video_id)

    PoTokenProvider.apply_to_ydl_opts_sync = _sync
    PoTokenProvider.apply_to_ydl_opts = _async
    setattr(PoTokenProvider, sentinel, True)


def _install() -> None:
    if not _enabled() or getattr(YouTube, _SENTINEL, False):
        return

    _apply_runtime_defaults()
    _install_quiet_stale_delete()
    _install_provider_download_bypass()

    original_pot_opts = YouTube._authoritative_pot_opts
    original_build_api = YouTube.build_ytdlp_api_opts
    original_build_cli = YouTube.build_ytdlp_cli_args
    original_search_uncached = YouTube._search_uncached

    def _pot_off_for_progressive(
        self: YouTube,
        base_opts: dict,
        *,
        lightweight: bool = False,
        fast_progressive: bool = False,
    ) -> dict:
        opts = original_pot_opts(
            self,
            base_opts,
            lightweight=lightweight,
            fast_progressive=fast_progressive,
        )
        fmt = str((opts or {}).get("format") or "").strip()

        # Keep POT only for an explicit exact-140 request. All foreground paths
        # have a progressive fallback and production already proves itag18 with
        # HTTP 206 and pot_bound=0.
        if fmt == "140":
            return opts

        stripped = _strip_provider_opts(opts)
        if stripped.get("_anonx_pot_bypassed"):
            stripped.pop("_anonx_pot_bypassed", None)
            if not getattr(self, "_sub3_pot_bypass_logged", False):
                logger.warning(
                    "sub3_pot_critical_path_bypass enabled=1 "
                    "foreground_provider=off exact140_provider=retained "
                    "reason=progressive_206_proven"
                )
                self._sub3_pot_bypass_logged = True
        return stripped

    def _download_without_pot(self: YouTube, *args, **kwargs) -> dict:
        opts = original_build_api(self, *args, **kwargs)
        action = str(kwargs.get("action") or (args[0] if args else "")).lower()
        if action == "download":
            stripped = _strip_provider_opts(opts)
            stripped.pop("_anonx_pot_bypassed", None)
            # download() may call po_token_provider.apply_to_ydl_opts() again on
            # each retry. Carry a private marker until that hook consumes it.
            stripped[_DOWNLOAD_NO_POT_MARKER] = True
            return stripped
        return opts

    def _download_cli_without_pot(self: YouTube, *args, **kwargs) -> list[str]:
        cli = list(original_build_cli(self, *args, **kwargs))
        action = str(kwargs.get("action") or (args[0] if args else "")).lower()
        return _strip_provider_cli(cli) if action == "download" else cli

    async def _zero_lookup_direct_url(
        self: YouTube,
        query: str,
        m_id: int,
        video: bool,
    ):
        clean_query = str(query or "").strip()
        match = self.regex.match(clean_query)
        direct_id = match.group(5) if match else None
        if (
            direct_id
            and len(direct_id) == 11
            and all(ch.isalnum() or ch in "_-" for ch in direct_id)
        ):
            # Use already-cached authoritative metadata if available, but never
            # spend 350ms on py_yt before starting the resolver for an explicit ID.
            cached = self._track_from_direct_metadata(
                direct_id,
                m_id,
                video=bool(video),
            )
            self.warm_direct_stream_source(
                direct_id,
                video=bool(video),
                quality_tier=None,
            )
            if cached is not None:
                logger.info(
                    "youtube_path=direct_id_zero_search video_id=%s "
                    "metadata=cached resolver_prewarm=immediate",
                    direct_id,
                )
                return cached
            logger.info(
                "youtube_path=direct_id_zero_search video_id=%s "
                "metadata=deferred resolver_prewarm=immediate",
                direct_id,
            )
            return Track(
                id=direct_id,
                channel_name="YouTube",
                duration="",
                duration_sec=0,
                message_id=m_id,
                title="YouTube Video",
                thumbnail=f"https://i.ytimg.com/vi/{direct_id}/hqdefault.jpg",
                url=f"{self.base}{direct_id}",
                view_count="",
                video=bool(video),
            )
        return await original_search_uncached(self, query, m_id, video)

    YouTube._authoritative_pot_opts = _pot_off_for_progressive
    YouTube.build_ytdlp_api_opts = _download_without_pot
    YouTube.build_ytdlp_cli_args = _download_cli_without_pot
    YouTube._search_uncached = _zero_lookup_direct_url

    setattr(YouTube, _SENTINEL, True)
    logger.info(
        "sub3_final_critical_path_patch enabled=1 "
        "direct_url_zero_search=1 authenticated_mweb_micro=1 "
        "cli_escape=0 background_140=0 foreground_pot=0 "
        "download_pot=0 stale_delete_error=quiet preflight_timeout_ms=%s",
        int(
            float(
                getattr(
                    config,
                    "DIRECT_AUTHORITATIVE_POT_PREFLIGHT_TIMEOUT_SEC",
                    0.45,
                )
                or 0.45
            )
            * 1000
        ),
    )


_install()
