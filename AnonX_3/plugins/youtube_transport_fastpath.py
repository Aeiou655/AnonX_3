# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""YouTube transport and resolver startup compatibility fastpaths.

Two narrow fixes live here:
- keep auto proxying for search/API traffic while forcing yt-dlp itself direct;
- run only the tiny player-response micro lanes without authenticated cookies.

The full authoritative mweb/POT resolver remains authenticated and unchanged as
fallback.  The micro lane is isolated by a different YoutubeDL fingerprint, so
cookie-free experiments cannot mutate or evict the authenticated resolver jar.
"""

from __future__ import annotations

import os

from AnonX_3 import config, logger
from AnonX_3.core.youtube import YouTube


_PROXY_SENTINEL = "_anonx_ytdlp_auto_proxy_compat_bypass_v1"
_MICRO_SENTINEL = "_anonx_micro_cookie_free_resolver_v1"


def _proxy_enabled() -> bool:
    raw = os.getenv("YTDLP_AUTO_PROXY_COMPAT_BYPASS", "true").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _micro_enabled() -> bool:
    raw = os.getenv("DIRECT_MICRO_COOKIE_FREE", "true").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _cookie_free_micro_opts(opts: dict) -> dict:
    """Return a shallow-isolated yt-dlp profile with no authentication cookies."""

    clean = dict(opts or {})
    clean.pop("cookiefile", None)
    clean.pop("cookiesfrombrowser", None)

    headers = dict(clean.get("http_headers") or {})
    for key in list(headers):
        if str(key).lower() == "cookie":
            headers.pop(key, None)
    if headers:
        clean["http_headers"] = headers
    else:
        clean.pop("http_headers", None)
    return clean


def _install_proxy_patch() -> None:
    if getattr(YouTube, _PROXY_SENTINEL, False):
        return

    original_base = YouTube._build_ytdlp_base_api_opts
    original_cli = YouTube.build_ytdlp_cli_args

    def _auto_mode() -> bool:
        return (
            _proxy_enabled()
            and str(getattr(config, "YOUTUBE_PROXY_MODE", "") or "").strip().lower()
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

            # An empty proxy is yt-dlp's explicit direct-connection override.
            # Merely deleting the option is insufficient when ALL_PROXY was
            # exported by automatic local-proxy discovery.
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


def _install_micro_cookie_free_patch() -> None:
    """Use a cookie-free YoutubeDL identity only for bounded player API lanes.

    Production traces show the 130-700ms micro calls returning
    ``authenticated=1`` + ``status=unplayable`` while the 2.8-3.2s full mweb
    extractor eventually succeeds.  Removing cookies only from micro workers
    lets clients such as tv_downgraded/web_safari/android_vr use their normal
    anonymous player context.  If every micro lane still misses, the existing
    authenticated full-resolver race remains the fallback.
    """

    if getattr(YouTube, _MICRO_SENTINEL, False):
        return

    original_candidate = YouTube._run_persistent_direct_player_candidate
    original_prepare = YouTube._run_persistent_direct_micro_prepare

    async def _run_cookie_free_candidate(
        self,
        video_id: str,
        opts: dict,
        *,
        client: str,
        video: bool,
        quality_tier: str | None,
        slot_hint: int,
    ):
        selected = _cookie_free_micro_opts(opts) if _micro_enabled() else opts
        if _micro_enabled() and not getattr(
            self, "_micro_cookie_free_profile_logged", False
        ):
            logger.info(
                "direct_micro_cookie_free_profile enabled=1 isolated_ydl=1 "
                "authenticated_fallback_retained=1"
            )
            self._micro_cookie_free_profile_logged = True
        return await original_candidate(
            self,
            video_id,
            selected,
            client=client,
            video=video,
            quality_tier=quality_tier,
            slot_hint=slot_hint,
        )

    async def _prepare_cookie_free_micro(
        self,
        opts: dict,
        *,
        slot_hint: int | None = None,
    ):
        selected = _cookie_free_micro_opts(opts) if _micro_enabled() else opts
        return await original_prepare(self, selected, slot_hint=slot_hint)

    YouTube._run_persistent_direct_player_candidate = _run_cookie_free_candidate
    YouTube._run_persistent_direct_micro_prepare = _prepare_cookie_free_micro
    setattr(YouTube, _MICRO_SENTINEL, True)
    logger.info(
        "youtube_micro_fastpath_patch enabled cookie_free=%s isolated_profile=1",
        int(_micro_enabled()),
    )


_install_proxy_patch()
_install_micro_cookie_free_patch()
