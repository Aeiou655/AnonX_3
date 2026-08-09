# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Keep automatic YouTube search proxying while forcing yt-dlp direct transport.

The VPS auto-proxy probe can legitimately find a local HTTP proxy that works for
small YouTube/search requests but fails yt-dlp's player/API/CDN traffic.  In that
case the old behaviour poisoned direct resolution *and* the local-download
fallback with the same proxy.

This patch is intentionally narrow:
- explicit operator proxies are untouched;
- YOUTUBE_PROXY=off is untouched;
- auto proxy remains available to py_yt/API/search paths;
- only yt-dlp API/CLI option construction drops an auto-selected proxy.

Because DirectStreamSource.proxy is derived from the yt-dlp extraction options,
successfully minted media URLs are also probed/fed directly instead of being
sent back through the incompatible local proxy.
"""

from __future__ import annotations

import os

from AnonX_3 import config, logger
from AnonX_3.core.youtube import YouTube


_PATCH_SENTINEL = "_anonx_ytdlp_auto_proxy_compat_bypass_v1"


def _enabled() -> bool:
    raw = os.getenv("YTDLP_AUTO_PROXY_COMPAT_BYPASS", "true").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _install() -> None:
    if getattr(YouTube, _PATCH_SENTINEL, False):
        return

    original = YouTube._build_ytdlp_base_api_opts

    def _build_ytdlp_base_api_opts_direct_on_auto(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not _enabled():
            return result

        try:
            opts, js_runtime_cli, pot_provider_url = result
            mode = str(getattr(config, "YOUTUBE_PROXY_MODE", "") or "").strip().lower()
            proxy = str(opts.get("proxy") or "").strip() if isinstance(opts, dict) else ""
            if mode != "auto" or not proxy:
                return result

            # Drop only the auto-selected transport from yt-dlp.  Do not mutate
            # config/self._youtube_proxy: search/API paths may still benefit from
            # that local proxy and explicit operator intent must remain intact.
            opts.pop("proxy", None)

            if not getattr(self, "_ytdlp_auto_proxy_bypass_logged", False):
                logger.warning(
                    "youtube_ytdlp_auto_proxy_bypass proxy=%s transport=direct "
                    "search_proxy_retained=1",
                    proxy.split("@")[-1],
                )
                self._ytdlp_auto_proxy_bypass_logged = True
            return opts, js_runtime_cli, pot_provider_url
        except Exception as ex:
            # A compatibility guard must never make runtime option construction
            # less reliable than the unpatched path.
            logger.debug(
                "youtube_ytdlp_auto_proxy_bypass skipped err=%s",
                type(ex).__name__,
            )
            return result

    YouTube._build_ytdlp_base_api_opts = _build_ytdlp_base_api_opts_direct_on_auto
    setattr(YouTube, _PATCH_SENTINEL, True)
    logger.info(
        "youtube_transport_fastpath_patch enabled auto_proxy_ytdlp_bypass=%s",
        int(_enabled()),
    )


_install()
